"""Reply state machine: duplicate suppression, retry policy, and send safety."""

from __future__ import annotations

import time

import pytest

from wechat_engine import statemachine


class FakeAnalysis:
    """Minimal stand-in for vision.FrameAnalysis."""

    def __init__(self, pending_count=1, text="你好", title="张三"):
        self.width = 1100
        self.height = 750
        self.layout = {}
        self.title = title
        self.bubbles = [object()]
        self.pending = [object()] * pending_count
        self.pending_text = text
        self.rows = []
        self.active_sidebar_name = None
        self.active_target = title
        self.active_source = "header"


class FakeResult:
    def __init__(self, reply="好呀"):
        self.reply = reply
        self.raw = reply
        self.elapsed = 0.01
        self.model = "test-model"
        self.usage = {}
        self.status = 200
        self.attempts = 1


@pytest.fixture()
def engine(store, quiet_log, monkeypatch):
    """An engine whose AI call and send path are controllable."""
    instance = statemachine.Engine(store=store)
    state = {"replies": [], "send_ok": True, "fail_calls": 0}

    def fake_complete(cfg, sender, message, history=None):
        state["replies"].append((sender, message))
        return FakeResult("测试回复")

    def fake_send(hwnd, layout, target, text, cfg):
        state["fail_calls"] += 1
        return (state["send_ok"], "test")

    fake_client = type("FakeClient", (), {})
    fake_client.complete = staticmethod(fake_complete)
    fake_client.stats = {"requests": 0, "failures": 0, "retries": 0, "totalSeconds": 0.0}
    monkeypatch.setattr(statemachine.ai, "get_client", lambda: fake_client())
    monkeypatch.setattr(instance, "send_reply", fake_send)
    instance.test_state = state
    return instance


class TestDuplicateSuppression:
    def test_same_message_is_answered_once(self, engine):
        """The 1.x signature included the bubble count and last y, both of which drift,
        so the same message could be answered repeatedly on a long conversation."""
        first = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert first["reason"] == "sent"
        second = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert second["reason"] == "duplicate"
        assert len(engine.test_state["replies"]) == 1

    def test_whitespace_differences_do_not_re_arm(self, engine):
        engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        again = engine._reply_to(None, {}, {}, "张三", "在 吗 ", FakeAnalysis())
        assert again["reason"] == "duplicate"

    def test_a_genuinely_new_message_is_answered(self, engine):
        engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        follow_up = engine._reply_to(None, {}, {}, "张三", "晚上吃饭不", FakeAnalysis())
        assert follow_up["reason"] == "sent"
        assert len(engine.test_state["replies"]) == 2

    def test_contacts_are_tracked_independently(self, engine):
        engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        other = engine._reply_to(None, {}, {}, "李四", "在吗", FakeAnalysis(title="李四"))
        assert other["reason"] == "sent"

    def test_history_memory_is_bounded(self, engine):
        for index in range(statemachine.RECENT_MESSAGE_MEMORY + 4):
            engine._reply_to(None, {}, {}, "张三", "消息%d" % index, FakeAnalysis())
        contact = engine._contact("张三")
        assert len(contact.recent_hashes) == statemachine.RECENT_MESSAGE_MEMORY


class TestRetryPolicy:
    def test_failed_send_retries_once_then_gives_up(self, engine):
        engine.test_state["send_ok"] = False
        first = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert first["reason"] == "send-failed"
        # Within the cooldown a second attempt is suppressed, so the API is not billed twice.
        second = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert second["reason"] in ("cooldown", "duplicate", "send-failed")

    def test_circuit_breaker_stops_after_max_attempts(self, engine):
        engine.test_state["send_ok"] = False
        contact = engine._contact("张三")
        digest = statemachine.message_hash("在吗")
        # note_failure counts consecutive failures for one message.
        first = contact.note_failure(digest)
        second = contact.note_failure(digest)
        assert (first, second) == (1, 2)
        assert second >= statemachine.MAX_SEND_ATTEMPTS

        # A different message starts a fresh count.
        assert contact.note_failure(statemachine.message_hash("新消息")) == 1

    def test_cooldown_expires(self, engine):
        contact = engine._contact("张三")
        digest = statemachine.message_hash("在吗")
        contact.note_failure(digest)
        assert contact.in_cooldown(digest) is True
        contact.last_fail_at = time.time() - statemachine.FAILED_SEND_COOLDOWN - 1
        assert contact.in_cooldown(digest) is False


class TestAiFailureHandling:
    def test_ai_error_does_not_send(self, engine, monkeypatch):
        def boom(cfg, sender, message, history=None):
            raise RuntimeError("API down")

        monkeypatch.setattr(statemachine.ai, "get_client", lambda: type("C", (), {"complete": staticmethod(boom)})())
        result = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert result["reason"] == "ai-failed"
        assert engine.test_state["fail_calls"] == 0

    def test_empty_reply_is_not_sent_and_is_remembered(self, engine, monkeypatch):
        def empty(cfg, sender, message, history=None):
            return FakeResult("")

        monkeypatch.setattr(statemachine.ai, "get_client", lambda: type("C", (), {"complete": staticmethod(empty)})())
        result = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert result["reason"] == "empty-reply"
        assert engine.test_state["fail_calls"] == 0
        assert engine._contact("张三").seen(statemachine.message_hash("在吗"))


class TestDryRun:
    def test_dry_run_generates_but_never_sends(self, engine):
        engine.set_dry_run(True)
        result = engine._reply_to(None, {}, {}, "张三", "在吗", FakeAnalysis())
        assert result["reason"] == "dry-run"
        assert engine.test_state["fail_calls"] == 0
        assert engine.test_state["replies"] == [("张三", "在吗")]
        assert engine.status()["replies"] == 0


class TestTypingDelay:
    def test_delay_is_clamped(self, engine):
        assert engine._typing_delay({"reply_delay_seconds": -5}) == 0.0
        assert engine._typing_delay({"reply_delay_seconds": 999}) == 120.0

    def test_jitter_is_additive_and_bounded(self, engine):
        for _ in range(20):
            delay = engine._typing_delay({"reply_delay_seconds": 1.0, "reply_delay_jitter_seconds": 0.5})
            assert 1.0 <= delay <= 1.5

    def test_malformed_delay_falls_back_to_zero(self, engine):
        assert engine._typing_delay({"reply_delay_seconds": "abc"}) == 0.0


class TestCleanReply:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("好呀", "好呀"),
            ("<think>hmm</think>好呀", "好呀"),
            ("（想了想）好呀", "好呀"),
            ("**好呀**", "好呀"),
            ("“好呀”", "好呀"),
            ("好呀\n\n再说", "好呀 再说"),
            ("好呀\n再说", "好呀 再说"),
        ],
    )
    def test_reply_sanitising(self, raw, expected):
        assert statemachine.ai.clean_ai_reply(raw) == expected

    def test_newlines_can_be_kept_when_asked(self, engine):
        assert statemachine.ai.clean_ai_reply("一\n二", keep_newlines=True) == "一\n二"

    def test_length_is_capped(self):
        long_reply = "字" * 900
        cleaned = statemachine.ai.clean_ai_reply(long_reply, max_chars=100)
        assert len(cleaned) == 101  # 100 characters plus the ellipsis
        assert cleaned.endswith("…")


class TestPromptAssembly:
    def test_persona_is_appended_when_present(self, store):
        store.update({"friend_personas": {"张三": "死党互损模式"}})
        prompt = statemachine.ai.build_system_prompt(store.snapshot(), "张三")
        assert "死党互损模式" in prompt
        assert "张三" in prompt

    def test_generic_fallback_when_no_persona(self, store):
        prompt = statemachine.ai.build_system_prompt(store.snapshot(), "张三")
        assert "张三" in prompt
        assert "严禁透露AI身份" in prompt


class TestChatUrl:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
            ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/chat/completions"),
            ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1/chat/completions"),
            ("api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
            ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/chat/completions"),
            ("", ""),
        ],
    )
    def test_url_normalisation(self, given, expected):
        assert statemachine.ai.build_chat_url(given) == expected


class TestResponseParsing:
    def test_openai_envelope(self):
        content, usage = statemachine.ai.extract_content({
            "choices": [{"message": {"content": "你好"}}],
            "usage": {"total_tokens": 12},
        })
        assert content == "你好"
        assert usage["total_tokens"] == 12

    def test_content_parts_are_joined(self):
        content, _usage = statemachine.ai.extract_content({
            "choices": [{"message": {"content": [{"text": "你"}, {"text": "好"}]}}],
        })
        assert content == "你好"

    def test_reasoning_only_response_is_used(self):
        content, _usage = statemachine.ai.extract_content({
            "choices": [{"message": {"content": "", "reasoning_content": "思考结果"}}],
        })
        assert content == "思考结果"

    def test_provider_error_is_reported_verbatim(self):
        with pytest.raises(ValueError, match="provider error: quota"):
            statemachine.ai.extract_content({"error": {"message": "quota"}})

    def test_missing_choices_reports_the_problem(self):
        with pytest.raises(ValueError, match="no choices"):
            statemachine.ai.extract_content({"choices": []})

    def test_empty_content_is_an_error_not_an_empty_reply(self):
        with pytest.raises(ValueError, match="empty assistant content"):
            statemachine.ai.extract_content({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})


class TestConversationMemory:
    def test_history_is_capped_per_contact(self):
        memory = statemachine.ai.ConversationMemory(max_turns=2)
        for index in range(5):
            memory.append("张三", "问%d" % index, "答%d" % index)
        assert len(memory.get("张三")) == 4

    def test_contacts_are_evicted_when_the_limit_is_reached(self):
        memory = statemachine.ai.ConversationMemory(max_turns=2, max_contacts=3)
        for index in range(6):
            memory.append("好友%d" % index, "问", "答")
        assert len(memory.snapshot()) == 3

    def test_clear_single_contact_keeps_others(self):
        memory = statemachine.ai.ConversationMemory()
        memory.append("张三", "问", "答")
        memory.append("李四", "问", "答")
        memory.clear("张三")
        assert memory.get("张三") == []
        assert len(memory.get("李四")) == 2
