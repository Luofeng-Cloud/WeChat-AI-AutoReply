"""Transport and service-surface tests.

These pin the RPC contract that the TypeScript client depends on: framing, error codes,
notifications, and the exact method list.
"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from wechat_engine import protocol, service


def run_requests(payloads, service_instance=None, store=None):
    """Feed newline-delimited requests through a dispatcher and collect the replies.

    Requests within one call are handled concurrently, so callers must not assume the
    replies share the submission order. Depend on a reply before issuing the next call.
    """
    inbound = io.StringIO("\n".join(json.dumps(item, ensure_ascii=False) for item in payloads) + "\n")
    outbound = io.StringIO()
    app = service_instance or service.Service(store=store)
    dispatcher = service.build_dispatcher(app, stream_in=inbound, stream_out=outbound)
    dispatcher.serve_forever()
    replies = [json.loads(line) for line in outbound.getvalue().splitlines() if line.strip()]
    app.close()
    return replies


class TestFraming:
    def test_response_is_one_json_object_per_line(self):
        replies = run_requests([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        assert len(replies) == 1
        assert replies[0]["id"] == 1
        assert replies[0]["result"]["pong"] is True

    def test_malformed_json_is_reported(self):
        inbound = io.StringIO("{not json}\n")
        outbound = io.StringIO()
        app = service.Service()
        dispatcher = service.build_dispatcher(app, stream_in=inbound, stream_out=outbound)
        dispatcher.serve_forever()
        reply = json.loads(outbound.getvalue().strip())
        assert reply["error"]["code"] == protocol.PARSE_ERROR
        app.close()

    def test_unknown_method_is_reported(self):
        replies = run_requests([{"jsonrpc": "2.0", "id": 3, "method": "does_not_exist"}])
        errors = [r for r in replies if r.get("id") == 3]
        assert errors and errors[0]["error"]["code"] == protocol.METHOD_NOT_FOUND

    def test_bad_params_are_reported_without_crashing(self):
        replies = run_requests([{"jsonrpc": "2.0", "id": 4, "method": "config_update", "params": {"patch": 5}}])
        errors = [r for r in replies if r.get("id") == 4]
        assert errors and errors[0]["error"]["code"] == protocol.INVALID_PARAMS

    def test_non_object_params_are_rejected(self):
        replies = run_requests([{"jsonrpc": "2.0", "id": 5, "method": "ping", "params": [1, 2]}])
        errors = [r for r in replies if r.get("id") == 5]
        assert errors and errors[0]["error"]["code"] == protocol.INVALID_PARAMS

    def test_handler_exception_becomes_an_error_response(self):
        class Boom:
            def explode(self):
                raise RuntimeError("kaboom")

        inbound = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "explode"}) + "\n")
        outbound = io.StringIO()
        dispatcher = protocol.Dispatcher(stream_in=inbound, stream_out=outbound)
        dispatcher.register("explode", Boom().explode)
        dispatcher.serve_forever()
        replies = [json.loads(line) for line in outbound.getvalue().splitlines()]
        errors = [r for r in replies if r.get("id") == 6]
        assert errors and errors[0]["error"]["code"] == protocol.INTERNAL_ERROR
        assert "kaboom" in errors[0]["error"]["message"]

    def test_eof_terminates_the_loop(self):
        dispatcher = protocol.Dispatcher(stream_in=io.StringIO(""), stream_out=io.StringIO())
        dispatcher.serve_forever()
        assert dispatcher.closed is True


class TestServiceSurface:
    def test_every_documented_method_exists(self, quiet_log, data_dir, store):
        """The renderer calls these by name; a rename must break this test, not the UI."""
        required = [
            "ping", "hello", "diagnostics",
            "config_get", "config_masked", "config_update", "config_reload",
            "prompt_presets", "apply_preset", "save_custom_prompt",
            "engine_status", "engine_start", "engine_stop", "engine_pause",
            "engine_resume", "engine_restart", "engine_set_dry_run", "engine_tick",
            "engine_preview", "engine_reset_memory",
            "wechat_status", "wechat_offscreen", "wechat_toggle_offscreen",
            "wechat_show", "wechat_minimize", "wechat_open",
            "capture_screenshot", "capture_data_url",
            "log_tail", "log_read", "log_clear", "log_open",
            "sandbox_test", "sandbox_preview_prompt", "test_api",
            "naming_check", "ocr_warmup", "shutdown",
        ]
        app = service.Service(store=store)
        try:
            for name in required:
                assert callable(getattr(app, name, None)), "missing method: %s" % name
        finally:
            app.close()

    def test_registered_methods_match_the_service(self, quiet_log, data_dir, store):
        inbound = io.StringIO("")
        app = service.Service(store=store)
        dispatcher = service.build_dispatcher(app, stream_in=inbound, stream_out=io.StringIO())
        registered = set(dispatcher.methods())
        assert "engine_status" in registered
        assert "config_update" in registered
        # Private helpers must not be reachable over the wire.
        assert not any(name.startswith("_") for name in registered)
        app.close()

    def test_config_round_trip_through_rpc(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "config_update", "params": {"patch": {"whitelist": ["张三"]}}},
            {"jsonrpc": "2.0", "id": 2, "method": "config_get"},
        ])
        by_id = {r.get("id"): r for r in replies if "id" in r}
        assert by_id[1]["result"]["whitelist"] == ["张三"]
        assert by_id[2]["result"]["whitelist"] == ["张三"]

    def test_presets_are_served(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[{"jsonrpc": "2.0", "id": 1, "method": "prompt_presets"}])
        result = [r for r in replies if r.get("id") == 1][0]["result"]
        assert len(result["prompts"]) == 12
        assert result["default"] in result["prompts"]

    def test_apply_preset_switches_the_active_prompt(self, quiet_log, data_dir, store):
        config_mod = __import__("wechat_engine.config", fromlist=["x"])
        replies = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "apply_preset", "params": {"key": "🤣 随和幽默 / 接梗达人"}},
        ])
        result = [r for r in replies if r.get("id") == 1][0]["result"]
        assert result["system_prompt"] == config_mod.DEFAULT_PROMPTS["🤣 随和幽默 / 接梗达人"]
        assert result["prompt_preset"] == "🤣 随和幽默 / 接梗达人"

    def test_apply_preset_restores_the_custom_prompt(self, quiet_log, data_dir, store):
        """Selecting 自由定制 must bring back the user text, not the generic template."""
        custom = "恢复我的自定义提示词"
        config_mod = __import__("wechat_engine.config", fromlist=["x"])
        replies = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "config_update", "params": {"patch": {
                "system_prompt": custom, "custom_prompt": custom,
                "prompt_preset": "🤣 随和幽默 / 接梗达人",
            }}},
            {"jsonrpc": "2.0", "id": 2, "method": "apply_preset", "params": {"key": config_mod.CUSTOM_PRESET_KEY}},
        ])
        by_id = {r.get("id"): r for r in replies if "id" in r}
        assert by_id[2]["result"]["system_prompt"] == custom

    def test_switching_away_from_custom_backs_it_up(self, quiet_log, data_dir, store):
        """The 1.x GUI overwrote an edited prompt without saving it first.

        Requests are dispatched on a thread pool, so a batch has no ordering guarantee.
        State-dependent steps therefore go in separate round trips; that is a property of
        the transport, not a workaround.
        """
        config_mod = __import__("wechat_engine.config", fromlist=["x"])
        custom = "只在自由定制里存在的文字"

        run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "apply_preset", "params": {"key": config_mod.CUSTOM_PRESET_KEY}},
        ])
        after_edit = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 2, "method": "config_update", "params": {"patch": {"system_prompt": custom}}},
        ])
        assert after_edit[0]["result"]["system_prompt"] == custom

        run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 3, "method": "apply_preset", "params": {"key": "💼 职场专业 / 严谨得体"}},
        ])
        final = run_requests(store=store, payloads=[{"jsonrpc": "2.0", "id": 4, "method": "config_get"}])
        assert final[0]["result"]["custom_prompt"] == custom

    def test_unknown_preset_is_rejected(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[{"jsonrpc": "2.0", "id": 1, "method": "apply_preset", "params": {"key": "nope"}}])
        errors = [r for r in replies if r.get("id") == 1 and "error" in r]
        assert errors and errors[0]["error"]["code"] == protocol.INVALID_PARAMS

    def test_naming_check_previews_matching(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[{
            "jsonrpc": "2.0", "id": 1, "method": "naming_check",
            "params": {"contact": "3张三", "whitelist": ["张三"]},
        }])
        result = [r for r in replies if r.get("id") == 1][0]["result"]
        assert result["matched"] is True
        assert result["canonical"] == "张三"

    def test_sandbox_requires_a_message(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[{"jsonrpc": "2.0", "id": 1, "method": "sandbox_test", "params": {"message": "  "}}])
        errors = [r for r in replies if r.get("id") == 1 and "error" in r]
        assert errors and errors[0]["error"]["code"] == protocol.INVALID_PARAMS

    def test_sandbox_reports_a_missing_key_as_configuration(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[{
            "jsonrpc": "2.0", "id": 1, "method": "sandbox_test",
            "params": {"sender": "测试好友", "message": "在吗"},
        }])
        result = [r for r in replies if r.get("id") == 1][0]["result"]
        assert result["ok"] is False
        assert result["configRequired"] is True

    def test_test_api_reports_the_missing_key(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "config_update",
             "params": {"patch": {"openai_api": {"api_key": ""}}}},
            {"jsonrpc": "2.0", "id": 2, "method": "test_api"},
        ])
        result = [r for r in replies if r.get("id") == 2][0]["result"]
        assert result["ok"] is False
        assert any("API Key" in problem for problem in result["problems"])
        # base URL and model keep usable defaults, so they are not reported
        assert result["endpoint"].endswith("/chat/completions")

    def test_test_api_accepts_a_complete_configuration(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "config_update", "params": {"patch": {
                "openai_api": {"api_key": "sk-abcdefghijklmnop", "api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
            }}},
            {"jsonrpc": "2.0", "id": 2, "method": "test_api"},
        ])
        result = [r for r in replies if r.get("id") == 2][0]["result"]
        assert result["ok"] is True
        assert result["endpoint"] == "https://api.deepseek.com/v1/chat/completions"
        # the key must never be echoed in full
        assert "abcdefghijklmnop" not in result["keyPreview"]
    

    def test_log_notifications_are_pushed(self, quiet_log, data_dir, store):
        inbound = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        outbound = io.StringIO()
        app = service.Service(store=store)
        dispatcher = service.build_dispatcher(app, stream_in=inbound, stream_out=outbound)
        from wechat_engine import logutil

        logutil.configure(path=data_dir / "n.log", level="info", stream=io.StringIO())
        # Re-subscribe against the freshly configured bus.
        app._unsubscribe()
        app._unsubscribe = logutil.get_bus().subscribe(app._on_log)
        logutil.log("hello from the engine")
        dispatcher.serve_forever()
        payloads = [json.loads(line) for line in outbound.getvalue().splitlines()]
        log_events = [p for p in payloads if p.get("method") == "engine.log"]
        assert any("hello from the engine" in event["params"]["line"] for event in log_events)
        app.close()

    def test_log_tail_and_clear(self, quiet_log, data_dir, store):
        replies = run_requests(store=store, payloads=[
            {"jsonrpc": "2.0", "id": 1, "method": "log_tail", "params": {"lines": 10}},
            {"jsonrpc": "2.0", "id": 2, "method": "log_clear"},
        ])
        tail = [r for r in replies if r.get("id") == 1][0]["result"]
        cleared = [r for r in replies if r.get("id") == 2][0]["result"]
        assert "lines" in tail and "offset" in tail
        assert cleared["ok"] is True
