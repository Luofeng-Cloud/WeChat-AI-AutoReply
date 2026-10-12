"""RPC service: every method the Electron UI can call.

One place where the protocol surface is defined, so the TypeScript client and the Python
server cannot drift without a test noticing (see tests/test_protocol.py).
"""

from __future__ import annotations

import os
import platform
import threading
import time
from typing import Any, Optional

from . import (
    __version__,
    ai,
    config as config_mod,
    logutil,
    naming,
    ocr,
    paths,
    protocol,
    statemachine,
    vision,
    win32,
    win32ops,
)

LOG_RING_LIMIT = 2000


class Service:
    """Owns the engine instance and exposes it over JSON-RPC."""

    def __init__(self, store=None, dispatcher=None, headless: bool = False, emit_events: bool = True):
        self.store = store or config_mod.get_store()
        self.dispatcher = dispatcher
        self.headless = headless
        # Without a transport there is nowhere to send events, and constructing a default
        # Dispatcher here would write notifications to stdout. That corrupts the JSON that
        # --call prints, which is exactly how the one-shot CLI silently returned garbage.
        self.emit_events = emit_events and dispatcher is not None
        self.engine = statemachine.Engine(store=self.store, on_event=self._on_engine_event)
        self._started_at = time.time()
        self._logs = []
        self._logs_lock = threading.Lock()
        self._log_offset = 0
        self._unsubscribe = logutil.get_bus().subscribe(self._on_log)

    # -- event plumbing ---------------------------------------------------
    def _on_log(self, level: str, line: str) -> None:
        with self._logs_lock:
            self._logs.append({"level": level, "line": line, "at": time.time()})
            if len(self._logs) > LOG_RING_LIMIT:
                del self._logs[:-LOG_RING_LIMIT]
        if self.emit_events and self.dispatcher is not None:
            self.dispatcher.notify("engine.log", {"level": level, "line": line})

    def _on_engine_event(self, event: str, payload: dict) -> None:
        if self.emit_events and self.dispatcher is not None:
            self.dispatcher.notify(event, payload)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self.engine.stop()

    # -- meta -------------------------------------------------------------
    def ping(self) -> dict:
        return {"pong": True, "at": time.time()}

    def hello(self) -> dict:
        cfg = self.store.snapshot()
        return {
            "name": "wechat-ai-autoreply-engine",
            "version": __version__,
            "protocol": 1,
            "platform": platform.platform(),
            "isWindows": win32.IS_WINDOWS,
            "pid": os.getpid(),
            "headless": self.headless,
            "methods": self.dispatcher.methods() if self.dispatcher else [],
            "paths": {
                "dataDir": str(paths.data_dir()),
                "configFile": str(paths.config_file()),
                "logFile": str(paths.log_file()),
                "modelsDir": str(paths.models_dir()),
                "iconFile": str(paths.icon_file() or ""),
            },
            "config": cfg,
        }

    def diagnostics(self) -> dict:
        bus = logutil.get_bus()
        return {
            "version": __version__,
            "uptimeSeconds": round(time.time() - self._started_at, 1),
            "host": win32ops.engine_host_info(),
            "ocr": ocr.get_engine().stats,
            "ai": dict(ai.get_client().stats),
            "logFile": str(bus.path),
            "logLevel": bus.level,
            "configDegraded": self.store.degraded,
            "sitePackages": _site_package_report(),
        }

    # -- config -----------------------------------------------------------
    def config_get(self) -> dict:
        return self.store.snapshot()

    def config_masked(self) -> dict:
        return self.store.masked()

    def config_update(self, patch: Optional[dict] = None) -> dict:
        if not isinstance(patch, dict):
            raise protocol.RpcError(protocol.INVALID_PARAMS, "patch must be an object")
        before = self.store.snapshot()
        after = self.store.update(patch, source="ui")
        self.engine.nudge()
        if before.get("check_interval_seconds") != after.get("check_interval_seconds"):
            logutil.log("[配置] 巡检间隔调整为 %ss" % after.get("check_interval_seconds"))
        if before.get("log_level") != after.get("log_level"):
            logutil.get_bus().level = logutil._LEVELS.get(after.get("log_level"), 20)
        return after

    def config_reload(self) -> dict:
        self.store.load()
        return self.store.snapshot()

    def prompt_presets(self) -> dict:
        return {
            "order": list(config_mod.PROMPT_PRESET_ORDER),
            "prompts": dict(config_mod.DEFAULT_PROMPTS),
            "default": config_mod.DEFAULT_PRESET_KEY,
            "customKey": config_mod.CUSTOM_PRESET_KEY,
            "blacklistDefaults": list(config_mod.DEFAULT_BLACKLIST),
            "ignoredKeywordsDefaults": list(config_mod.DEFAULT_IGNORED_KEYWORDS),
        }

    def apply_preset(self, key: str) -> dict:
        key = str(key or "").strip()
        if key not in config_mod.DEFAULT_PROMPTS:
            raise protocol.RpcError(protocol.INVALID_PARAMS, "unknown preset: %s" % key)
        patch = {"prompt_preset": key}
        if key == config_mod.CUSTOM_PRESET_KEY:
            # Never clobber the user's own prompt with the generic template.
            current = self.store.snapshot()
            custom = str(current.get("custom_prompt") or "").strip()
            active = str(current.get("system_prompt") or "").strip()
            is_stock = active in config_mod.DEFAULT_PROMPTS.values()
            patch["system_prompt"] = custom or (active if not is_stock and active else config_mod.DEFAULT_PROMPTS[key])
        else:
            current = str(self.store.get("system_prompt") or "").strip()
            if current and current not in config_mod.DEFAULT_PROMPTS.values():
                patch["custom_prompt"] = current
            patch["system_prompt"] = config_mod.DEFAULT_PROMPTS[key]
        return self.store.update(patch, source="preset")

    def save_custom_prompt(self, text: str) -> dict:
        return self.store.update(
            {"custom_prompt": str(text or ""), "prompt_preset": config_mod.CUSTOM_PRESET_KEY},
            source="custom-prompt",
        )

    # -- engine control ---------------------------------------------------
    def engine_status(self) -> dict:
        status = self.engine.status()
        status["wechat"] = win32ops.wechat_status()
        status["configDegraded"] = self.store.degraded
        return status

    def engine_start(self) -> dict:
        if not win32.IS_WINDOWS:
            raise protocol.RpcError(protocol.INTERNAL_ERROR, "engine requires Windows")
        self.engine.start()
        return self.engine.status()

    def engine_stop(self) -> dict:
        self.engine.stop()
        return self.engine.status()

    def engine_pause(self) -> dict:
        self.engine.pause(True)
        return self.engine.status()

    def engine_resume(self) -> dict:
        self.engine.pause(False)
        self.engine.nudge()
        return self.engine.status()

    def engine_restart(self) -> dict:
        self.engine.stop()
        time.sleep(0.2)
        self.engine.start()
        return self.engine.status()

    def engine_set_dry_run(self, enabled: bool = True) -> dict:
        self.engine.set_dry_run(bool(enabled))
        return self.engine.status()

    def engine_tick(self, force: bool = True, cfg: Optional[dict] = None) -> dict:
        """Run exactly one scan cycle on demand (the UI "test detection" button)."""
        return self.engine.scan_once(cfg=cfg or self.store.snapshot(), force=bool(force))

    def engine_preview(self) -> dict:
        """Capture + analyse without making any decision or sending anything."""
        captured = self.engine.capture_and_analyze(self.store.snapshot())
        if not captured.get("ok"):
            return {"ok": False, "reason": captured.get("reason")}
        return {"ok": True, "detection": captured["detection"], "wechat": self.engine.status().get("wechat")}

    def engine_reset_memory(self, contact: Optional[str] = None) -> dict:
        self.engine.memory.clear(contact)
        return {"ok": True, "cleared": contact or "*"}

    # -- window operations ------------------------------------------------
    def wechat_status(self) -> dict:
        return win32ops.wechat_status()

    def wechat_offscreen(self, hidden: Optional[bool] = None) -> dict:
        return win32ops.set_offscreen(hidden)

    def wechat_toggle_offscreen(self) -> dict:
        return win32ops.toggle_offscreen()

    def wechat_show(self) -> dict:
        return win32ops.show_wechat()

    def wechat_minimize(self) -> dict:
        return win32ops.minimize_wechat()

    def wechat_open(self) -> dict:
        return win32ops.open_wechat()

    def capture_screenshot(self, max_width: int = 0, include_data_url: bool = True, save_path: str = "") -> dict:
        return win32ops.capture_window(max_width, include_data_url, save_path)

    def capture_data_url(self, max_width: int = 900) -> dict:
        result = win32ops.capture_window(max_width, True, "")
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "dataUrl": result.get("dataUrl"),
            "width": result["width"],
            "height": result["height"],
            "at": time.time(),
        }

    # -- logs -------------------------------------------------------------
    def log_tail(self, lines: int = 400) -> dict:
        try:
            count = max(1, min(5000, int(lines)))
        except (TypeError, ValueError):
            count = 400
        with self._logs_lock:
            tail = list(self._logs[-count:])
        offset = 0
        try:
            offset = os.path.getsize(logutil.get_bus().path)
        except OSError:
            offset = 0
        return {"lines": tail, "offset": offset, "path": str(logutil.get_bus().path)}

    def log_read(self, offset: int = 0) -> dict:
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        text, new_offset = logutil.read_new(logutil.get_bus().path, start)
        return {"text": text, "offset": new_offset}

    def log_clear(self) -> dict:
        logutil.truncate(logutil.get_bus().path)
        with self._logs_lock:
            self._logs.clear()
        return {"ok": True}

    def log_open(self) -> dict:
        target = str(logutil.get_bus().path)
        if not os.path.isfile(target):
            return {"ok": False, "reason": "missing"}
        try:
            os.startfile(target)  # noqa: S606 - Windows shell open
            return {"ok": True, "path": target}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "path": target}

    # -- sandbox ----------------------------------------------------------
    def sandbox_test(self, sender: str = "测试好友", message: str = "", use_history: bool = False) -> dict:
        text = str(message or "").strip()
        if not text:
            raise protocol.RpcError(protocol.INVALID_PARAMS, "message is required")
        cfg = self.store.snapshot()
        history = self.engine.memory.get(sender) if use_history else []
        started = time.time()
        try:
            result = ai.get_client().complete(cfg, sender, text, history)
        except ai.AiError as exc:
            return {"ok": False, "reason": "config", "message": str(exc), "configRequired": True}
        except Exception as exc:
            return {"ok": False, "reason": "request", "message": str(exc), "elapsed": round(time.time() - started, 2)}
        return {
            "ok": True,
            "reply": result.reply,
            "raw": result.raw,
            "elapsed": round(result.elapsed, 2),
            "model": result.model,
            "usage": result.usage,
            "attempts": result.attempts,
            "systemPrompt": ai.build_system_prompt(cfg, sender),
        }

    def sandbox_preview_prompt(self, sender: str = "测试好友") -> dict:
        cfg = self.store.snapshot()
        return {
            "systemPrompt": ai.build_system_prompt(cfg, sender),
            "persona": (cfg.get("friend_personas") or {}).get(sender, ""),
            "hasPersona": bool((cfg.get("friend_personas") or {}).get(sender)),
        }

    def test_api(self) -> dict:
        """Validate the endpoint, key and model without sending a chat message."""
        cfg = self.store.snapshot()
        api = cfg.get("openai_api") or {}
        key = str(api.get("api_key") or "").strip()
        url = ai.build_chat_url(api.get("api_base") or "")
        model = str(api.get("model") or "").strip()
        problems = []
        if not key:
            problems.append("API Key 未填写")
        if not url:
            problems.append("API Base URL 未填写")
        if not model:
            problems.append("模型 ID 未填写")
        if problems:
            return {"ok": False, "reason": "config", "problems": problems, "endpoint": url, "model": model}
        return {"ok": True, "endpoint": url, "model": model, "keyPreview": (key[:6] + "…" + key[-4:]) if len(key) > 10 else "…"}

    # -- naming helpers (used by the whitelist editor to preview matching) --
    def naming_check(self, contact: str = "", whitelist: Optional[list] = None) -> dict:
        entries = whitelist if isinstance(whitelist, list) else (self.store.get("whitelist") or [])
        matched, canonical, tier = naming.find_whitelist_match(contact, entries)
        return {
            "contact": contact,
            "matched": matched,
            "canonical": canonical,
            "tier": tier,
            "isGroup": naming.looks_like_group(contact),
            "isSystem": naming.is_system_sender(contact),
            "normalized": naming.normalize_basic(contact),
        }

    def ocr_warmup(self) -> dict:
        """Load the OCR models now so the first scan is not delayed."""
        try:
            ocr.get_engine().load()
        except ocr.OcrUnavailable as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "stats": ocr.get_engine().stats}

    def shutdown(self) -> dict:
        """Stop the engine and ask the dispatcher to exit."""
        self.engine.stop()
        if self.dispatcher is not None:
            threading.Thread(target=self.dispatcher.request_stop, daemon=True).start()
        return {"ok": True}


def _site_package_report() -> dict:
    """Which optional dependencies resolved, for the diagnostics panel."""
    report = {}
    for name in ("numpy", "PIL", "requests", "psutil", "pyperclip", "rapidocr_onnxruntime", "onnxruntime"):
        try:
            module = __import__(name)
            report[name] = getattr(module, "__version__", "ok")
        except Exception as exc:
            report[name] = "missing (%s)" % type(exc).__name__
    return report


def build_dispatcher(service: Service, stream_in=None, stream_out=None) -> protocol.Dispatcher:
    dispatcher = protocol.Dispatcher(stream_in=stream_in, stream_out=stream_out)
    service.dispatcher = dispatcher
    service.emit_events = True
    for name in dir(service):
        if name.startswith("_"):
            continue
        attribute = getattr(service, name)
        if callable(attribute) and getattr(attribute, "__module__", "") == __name__:
            dispatcher.register(name, attribute)
    return dispatcher
