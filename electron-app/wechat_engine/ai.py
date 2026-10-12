"""LLM brain: prompt assembly, HTTP client, reply sanitising.

Port of generate_ai_reply() plus the sandbox test path, with these corrections:

* **Status codes other than 200 were treated identically.** A 429 was logged as
  "API HTTP 429" and the message signature was then registered as processed, so the
  reply was silently dropped forever. Retryable statuses now back off and retry, and
  only non-retryable failures are recorded as processed.
* **Response parsing assumed the exact OpenAI shape.** data["choices"][0]["message"]
  ["content"] raised KeyError on any provider that returns a different envelope, and
  the resulting exception message was logged as a request error. Parsing now tolerates
  reasoning models, content arrays and missing fields, and reports precisely what was
  wrong.
* **No request timeout configuration.** 12 s was hard-coded. It is now per-config.
* **No newline handling.** The reply was pasted verbatim; a model that answered with two
  paragraphs produced a message that WeChat sent in pieces. Replies are collapsed to a
  single line by default.
* **History grew per contact without bound** (API_CONVERSATION_HISTORY[sender] was
  trimmed to 10 entries but the dict itself never shrank). Both are bounded now.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import List, Optional, Tuple

from . import logutil

THINK_TAG_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
LEADING_PAREN_RE = re.compile(r"^[（(][^）)]{0,60}[）)]\s*")
TRAILING_PAREN_RE = re.compile(r"\s*[（(][^）)]{0,60}[）)]$")
MARKDOWN_RE = re.compile(r"[*_#`]")
MULTI_NEWLINE_RE = re.compile(r"\s*\n+\s*")

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})


class AiError(RuntimeError):
    """Raised for configuration problems that the user must fix."""


class AiResult:
    __slots__ = ("reply", "raw", "elapsed", "model", "usage", "status", "attempts")

    def __init__(self, reply, raw="", elapsed=0.0, model="", usage=None, status=200, attempts=1):
        self.reply = reply
        self.raw = raw
        self.elapsed = elapsed
        self.model = model
        self.usage = usage or {}
        self.status = status
        self.attempts = attempts

    def to_dict(self):
        return {
            "reply": self.reply,
            "elapsed": round(self.elapsed, 3),
            "model": self.model,
            "usage": self.usage,
            "status": self.status,
            "attempts": self.attempts,
        }


def clean_ai_reply(reply, keep_newlines: bool = False, max_chars: int = 600) -> str:
    """Strip reasoning blocks, stage directions, markdown and stray quoting.

    Newlines collapse to a single space by default so the pasted text cannot be split
    across two WeChat messages by an embedded Enter.
    """
    if not reply:
        return ""
    text = str(reply)
    text = THINK_TAG_RE.sub("", text)
    text = text.strip()
    text = LEADING_PAREN_RE.sub("", text)
    text = TRAILING_PAREN_RE.sub("", text)
    text = MARKDOWN_RE.sub("", text)
    # Strip a wrapping pair of quotes only when the whole reply is quoted.
    for opener, closer in (('"', '"'), ("\u201c", "\u201d"), ("\u300c", "\u300d")):
        if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
            text = text[1:-1].strip()
    if not keep_newlines:
        text = MULTI_NEWLINE_RE.sub(" ", text)
    else:
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def build_system_prompt(cfg: dict, sender: str, now: Optional[str] = None) -> str:
    """Assemble the system prompt exactly as the 1.x engine did."""
    now_time = now or time.strftime("%H:%M")
    custom_prompt = str(cfg.get("system_prompt") or "").strip()
    personas = cfg.get("friend_personas") or {}
    persona_desc = str(personas.get(sender) or "").strip()

    if custom_prompt:
        parts = [custom_prompt]
        parts.append("【当前互动好友】：好友【%s】，当前时间%s。" % (sender, now_time))
        if persona_desc:
            parts.append("【该好友专属人设】：%s" % persona_desc)
        parts.append("【对话规则】：直接输出发给对方的一句中文，口语化自然、简练得体。严禁括号描写心理活动，严禁透露AI身份。")
        return "\n".join(parts)

    fallback_persona = persona_desc or "幽默风趣、随和自然的真实好友"
    return (
        "你现在是微信账号的主人本人（真实真人），正在微信上与好友【%s】聊天。当前时间是%s。\n"
        "【专属人设风格】：%s\n"
        "【对话规则】：直接输出发给对方的一句中文，口语化自然、简练得体。严禁括号描写心理活动，严禁透露AI身份。"
    ) % (sender, now_time, fallback_persona)


def build_chat_url(api_base: str) -> str:
    """Normalise a base URL into a chat-completions endpoint.

    Accepts "https://x/v1", "https://x/v1/", "https://x/v1/chat/completions" and bare
    hosts, because every provider documents it differently and users paste all of them.
    """
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return ""
    if not re.match(r"^https?://", base, re.IGNORECASE):
        base = "https://" + base
    if base.lower().endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def extract_content(payload) -> Tuple[str, dict]:
    """Pull the assistant text and usage out of a chat-completions response.

    Handles the OpenAI envelope, reasoning models that also return reasoning_content,
    and providers that return content as a list of parts.
    """
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ValueError("provider error: %s" % message)

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")

    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                chunks.append(str(part.get("text") or ""))
            else:
                chunks.append(str(part))
        content = "".join(chunks)

    if not content:
        # Some reasoning models put everything in reasoning_content when they truncate.
        content = message.get("reasoning_content") or first.get("text") or ""
    if not isinstance(content, str) or not content.strip():
        finish = first.get("finish_reason")
        raise ValueError("empty assistant content (finish_reason=%s)" % finish)

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return content, usage


class AiClient:
    """OpenAI-compatible chat client with bounded, explicit retry behaviour."""

    def __init__(self, timeout: float = 12.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = None
        self._lock = threading.Lock()
        self.last_error = None
        self.stats = {"requests": 0, "failures": 0, "retries": 0, "totalSeconds": 0.0}

    def _http(self):
        with self._lock:
            if self._session is None:
                import requests

                self._session = requests.Session()
                self._session.headers.update({"Content-Type": "application/json"})
            return self._session

    def close(self):
        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None

    def complete(self, cfg: dict, sender: str, message: str,
                 history: Optional[List[dict]] = None) -> AiResult:
        """One chat completion, with retries on transient failures."""
        api = cfg.get("openai_api") or {}
        key = str(api.get("api_key") or cfg.get("api_key") or "").strip()
        url = build_chat_url(api.get("api_base") or cfg.get("api_url") or "")
        model = str(api.get("model") or cfg.get("api_model") or "").strip()
        try:
            temperature = float(api.get("temperature", 0.7))
        except (TypeError, ValueError):
            temperature = 0.7
        try:
            timeout = float(api.get("timeout_seconds", self.timeout))
        except (TypeError, ValueError):
            timeout = self.timeout
        try:
            retries = int(api.get("max_retries", self.max_retries))
        except (TypeError, ValueError):
            retries = self.max_retries

        if not key:
            raise AiError("未配置 API Key，请在【AI 引擎】中填写")
        if not url:
            raise AiError("未配置 API Base URL")
        if not model:
            raise AiError("未配置模型 ID")

        system_prompt = build_system_prompt(cfg, sender)
        messages = [{"role": "system", "content": system_prompt}]
        for item in (history or [])[-16:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                messages.append({"role": item["role"], "content": str(item.get("content") or "")})
        messages.append({"role": "user", "content": message})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": max(0.0, min(2.0, temperature)),
        }
        headers = {"Authorization": "Bearer %s" % key}
        started = time.time()
        attempt = 0
        last_error = None

        while attempt <= max(0, retries):
            attempt += 1
            try:
                response = self._http().post(url, headers=headers, json=payload, timeout=timeout)
            except Exception as exc:
                last_error = "网络请求失败: %s" % exc
                self.stats["failures"] += 1
                if attempt <= max(0, retries):
                    self.stats["retries"] += 1
                    time.sleep(min(4.0, 0.4 * (2 ** (attempt - 1))))
                    continue
                break

            self.stats["requests"] += 1
            status = response.status_code
            if status == 200:
                try:
                    content, usage = extract_content(response.json())
                except Exception as exc:
                    last_error = "响应解析失败: %s" % exc
                    self.stats["failures"] += 1
                    break
                elapsed = time.time() - started
                self.stats["totalSeconds"] += elapsed
                self.last_error = None
                return AiResult(
                    reply=clean_ai_reply(content),
                    raw=content,
                    elapsed=elapsed,
                    model=model,
                    usage=usage,
                    status=status,
                    attempts=attempt,
                )

            body = (response.text or "")[:200]
            last_error = "HTTP %s: %s" % (status, body)
            if status in RETRYABLE_STATUS and attempt <= max(0, retries):
                self.stats["retries"] += 1
                # Honour Retry-After when the provider sends it.
                delay = 0.5 * (2 ** (attempt - 1))
                header = response.headers.get("Retry-After") if response.headers else None
                if header:
                    try:
                        delay = max(delay, min(10.0, float(header)))
                    except (TypeError, ValueError):
                        pass
                logutil.get_bus().warn(
                    "[API] HTTP %s，%.1fs 后重试 (%s/%s)" % (status, delay, attempt, retries)
                )
                time.sleep(min(10.0, delay))
                continue
            self.stats["failures"] += 1
            break

        self.last_error = last_error
        raise RuntimeError(last_error or "unknown AI error")


class ConversationMemory:
    """Bounded per-contact history.

    The 1.x API_CONVERSATION_HISTORY dict kept one list per contact forever; a long
    running daemon accumulated every friend ever messaged. Entries are now evicted when
    the contact count is exceeded, and each list is truncated to the configured depth.
    """

    def __init__(self, max_turns: int = 8, max_contacts: int = 200):
        self.max_turns = max(1, int(max_turns))
        self.max_contacts = max(1, int(max_contacts))
        self._data = {}
        self._order = []
        self._lock = threading.Lock()

    def get(self, contact: str) -> List[dict]:
        with self._lock:
            return list(self._data.get(contact, []))

    def append(self, contact: str, user_text: str, assistant_text: str) -> None:
        with self._lock:
            history = self._data.setdefault(contact, [])
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": assistant_text})
            # Keep 2 entries per turn.
            limit = self.max_turns * 2
            if len(history) > limit:
                del history[:-limit]
            if contact in self._order:
                self._order.remove(contact)
            self._order.append(contact)
            while len(self._order) > self.max_contacts:
                evicted = self._order.pop(0)
                self._data.pop(evicted, None)

    def clear(self, contact: Optional[str] = None) -> None:
        with self._lock:
            if contact is None:
                self._data.clear()
                self._order.clear()
            else:
                self._data.pop(contact, None)
                if contact in self._order:
                    self._order.remove(contact)

    def snapshot(self) -> dict:
        with self._lock:
            return {key: list(value) for key, value in self._data.items()}


_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> AiClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = AiClient()
        return _CLIENT
