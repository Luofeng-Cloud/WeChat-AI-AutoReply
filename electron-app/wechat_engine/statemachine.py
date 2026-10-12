"""The reply state machine: capture, perceive, decide, send, verify.

This is the port of scan_and_reply_wechat() plus the main loop. It is the file where the
1.x safety and correctness problems were concentrated.

### Duplicate-reply bug (the important one)

The 1.x dedup signature was:

    current_sig = (target, combined_text, int(last_bubble_y), len(chat_bubbles))

Both of the last two fields drift as the conversation scrolls and grows. Replying to a
friend whose message sits above the fold changes last_bubble_y on the next scan, so the
signature no longer matched and the *same* message was answered again. On a long chat the
engine could reply repeatedly to one question.

Replying also *changes* the bubble list, and the engine compared against the pre-send
list, so the very next scan saw a different length and re-armed. The signature now keys on
content only: the sender plus a hash of the aggregated incoming text. Adjacent repeats are
suppressed by a per-contact history of recent message hashes.

### Focus theft

"Zero mouse movement" was true, but only after activate_wechat() had momentarily raised
WeChat above the user whole window stack, clicked, and restored focus. Anyone typing got
interrupted. The engine now prefers the background injection path (PostMessage WM_CHAR plus
a posted Enter), which never changes the foreground window, and only falls back to the
foreground path when the user explicitly selects it or the background attempt is rejected.

### Retry storm

SEND_FAIL_RETRY was keyed on the drifting signature, so the "2 attempts then give up"
circuit breaker never tripped for the case it was written for. It is now keyed on
(contact, message hash) with an explicit attempt counter and a cooldown.
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import ai, config as config_mod, layout as layout_mod, logutil, naming, ocr, vision, win32

MAX_SEND_ATTEMPTS = 2
FAILED_SEND_COOLDOWN = 90.0
CANDIDATE_COOLDOWN = 3.0
PIXEL_SENTINEL_FORCE_SCAN = 1.0
RECENT_MESSAGE_MEMORY = 6


def message_hash(text: str) -> str:
    normalized = naming.strip_whitespace(text or "")
    return hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class ContactMemory:
    """Per-contact bookkeeping for duplicate suppression."""

    recent_hashes: List[str] = field(default_factory=list)
    failing_hash: str = ""
    fail_count: int = 0
    last_fail_at: float = 0.0

    def seen(self, digest: str) -> bool:
        return digest in self.recent_hashes

    def remember(self, digest: str) -> None:
        if digest in self.recent_hashes:
            self.recent_hashes.remove(digest)
        self.recent_hashes.append(digest)
        if len(self.recent_hashes) > RECENT_MESSAGE_MEMORY:
            del self.recent_hashes[:-RECENT_MESSAGE_MEMORY]

    def note_failure(self, digest: str) -> int:
        if self.failing_hash != digest:
            self.failing_hash = digest
            self.fail_count = 0
        self.fail_count += 1
        self.last_fail_at = time.time()
        return self.fail_count

    def clear_failure(self) -> None:
        self.failing_hash = ""
        self.fail_count = 0

    def in_cooldown(self, digest: str) -> bool:
        if self.failing_hash != digest:
            return False
        return (time.time() - self.last_fail_at) < FAILED_SEND_COOLDOWN


class Engine:
    """Owns the scan loop. All public methods are safe to call from any thread."""

    def __init__(self, store=None, on_event: Optional[Callable] = None):
        self.store = store or config_mod.get_store()
        self._on_event = on_event
        self.memory = ai.ConversationMemory(max_turns=8)
        self._contacts = {}
        self._contacts_lock = threading.Lock()
        self._candidate_clicked = {}

        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.RLock()

        self._last_chat_hash = None
        self._last_sidebar_hash = None
        self._last_full_scan = 0.0
        self._dpi_scale = 1.0

        self._status = {
            "running": False,
            "phase": "idle",
            "cycles": 0,
            "replies": 0,
            "lastError": None,
            "lastScanAt": None,
            "lastReplyAt": None,
            "lastReplyTarget": None,
            "lastReplyText": None,
            "ocrStats": {},
            "wechat": None,
            "detection": None,
            "dryRun": False,
            "startedAt": None,
            "paused": False,
        }

    # -- lifecycle --------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._state_lock:
            snapshot = dict(self._status)
        snapshot["running"] = self.running
        snapshot["ocrStats"] = ocr.get_engine().stats
        snapshot["aiStats"] = dict(ai.get_client().stats)
        snapshot["memoryContacts"] = len(self.memory.snapshot())
        return snapshot

    def start(self):
        if self.running:
            return False
        self._stop.clear()
        self._wake.clear()
        win32.enable_dpi_awareness()
        self._thread = threading.Thread(target=self._loop, name="wechat-engine", daemon=True)
        self._thread.start()
        with self._state_lock:
            self._status["running"] = True
            self._status["startedAt"] = time.time()
            self._status["phase"] = "starting"
        self._emit("engine.started", {})
        return True

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        with self._state_lock:
            self._status["running"] = False
            self._status["phase"] = "stopped"
        self._emit("engine.stopped", {})
        return True

    def pause(self, paused: bool = True):
        with self._state_lock:
            self._status["paused"] = bool(paused)
        self._emit("engine.paused" if paused else "engine.resumed", {})
        return self._status["paused"]

    def nudge(self):
        """Wake the loop immediately (used after a config change)."""
        self._wake.set()

    def set_dry_run(self, enabled: bool):
        with self._state_lock:
            self._status["dryRun"] = bool(enabled)
        return self._status["dryRun"]

    def _emit(self, event: str, payload: dict):
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:
            pass

    def _contact(self, name: str) -> ContactMemory:
        with self._contacts_lock:
            if name not in self._contacts:
                self._contacts[name] = ContactMemory()
            return self._contacts[name]

    # -- main loop --------------------------------------------------------
    def _loop(self):
        logutil.get_bus().banner(
            "PC 微信真人拟真 AI 自动回复系统【Electron 引擎 v2】已启动",
            [
                "   左轨看板: 绿色激活项背景判定 + 白名单红点巡检",
                "   右轨微观: 顶栏标题交叉锚定 + 贴底气泡真彩流向分析",
                "   协同仲裁: 内容指纹去重，绝不重复回复同一条消息",
            ],
        )
        win32.ensure_default_desktop()
        while not self._stop.is_set():
            interval = 0.8
            try:
                cfg = self.store.snapshot()
                interval = float(cfg.get("check_interval_seconds", 0.8) or 0.8)
                interval = min(60.0, max(0.1, interval))
                paused = False
                with self._state_lock:
                    paused = bool(self._status.get("paused"))
                if not paused:
                    self.scan_once(cfg)
                with self._state_lock:
                    self._status["cycles"] += 1
                    self._status["lastScanAt"] = time.time()
                    if self._status.get("phase") not in ("sending", "generating"):
                        self._status["phase"] = "watching"
            except Exception as exc:
                with self._state_lock:
                    self._status["lastError"] = str(exc)
                logutil.get_bus().error("[main loop] unhandled: %s" % exc)
                interval = 1.0
                self._emit("engine.error", {"message": str(exc)})
            # Interruptible sleep: a config change or a stop returns immediately.
            self._wake.wait(timeout=interval)
            self._wake.clear()
        with self._state_lock:
            self._status["running"] = False
            self._status["phase"] = "stopped"
        logutil.log("engine loop exited")

    # -- one cycle --------------------------------------------------------
    def capture_and_analyze(self, cfg: Optional[dict] = None):
        """Capture WeChat and run the perception pipeline. No decisions, no side effects.

        Shared by the reply loop and by the UI preview so both describe the screen with
        exactly the same code path; a divergent preview would be worse than none.
        Returns a dict with keys ok/reason or hwnd/rect/layout/image/analysis/detection.
        """
        cfg = cfg or self.store.snapshot()
        hwnd, version = win32.get_wechat_hwnd(hide_shell=False)
        if not hwnd:
            with self._state_lock:
                self._status["wechat"] = None
                self._status["detection"] = None
            return {"ok": False, "reason": "wechat-not-found"}

        info = win32.describe_wechat() or {}
        with self._state_lock:
            self._status["wechat"] = {
                "hwnd": hwnd,
                "version": version,
                "title": info.get("title"),
                "minimized": info.get("minimized"),
                "offscreen": info.get("offscreen"),
                "rect": info.get("rect"),
            }

        image, rect, scale = win32.grab_window(hwnd, self._dpi_scale)
        if image is None:
            return {"ok": False, "reason": "capture-failed", "hwnd": hwnd}
        if scale and scale > 0:
            self._dpi_scale = scale
        width, height = image.size
        if width <= 100 or height <= 100:
            return {"ok": False, "reason": "window-too-small", "hwnd": hwnd}

        layout = layout_mod.compute_layout(width, height, self._dpi_scale)
        results, _ratio = ocr.get_engine().infer_downscaled(image, max_width=1000)
        if not results:
            with self._state_lock:
                self._status["detection"] = None
            return {"ok": False, "reason": "ocr-empty", "hwnd": hwnd, "image": image, "layout": layout}

        whitelist_mode = bool(cfg.get("whitelist_mode", True))
        analysis = vision.analyze_frame(
            results,
            image,
            layout,
            whitelist_mode,
            cfg.get("whitelist") or [],
            cfg.get("blacklist") or [],
            cfg.get("ignored_keywords"),
        )
        detection = {
            "width": analysis.width,
            "height": analysis.height,
            "title": analysis.title,
            "target": analysis.active_target,
            "source": analysis.active_source,
            "pendingCount": len(analysis.pending),
            "pendingText": analysis.pending_text,
            "bubbleCount": len(analysis.bubbles),
            "rows": [
                {
                    "name": row.name,
                    "canonical": row.canonical,
                    "y": row.y,
                    "unread": row.has_red_badge,
                    "active": row.green_score > vision.SIDEBAR_ACTIVE_GREEN_MIN,
                    "tier": row.tier,
                }
                for row in analysis.rows
            ],
            "scale": layout["scale"],
            "at": time.time(),
        }
        with self._state_lock:
            self._status["detection"] = detection
        self._emit("engine.detection", detection)
        return {
            "ok": True,
            "hwnd": hwnd,
            "version": version,
            "rect": rect,
            "layout": layout,
            "image": image,
            "analysis": analysis,
            "detection": detection,
        }

    def scan_once(self, cfg: Optional[dict] = None, force: bool = False):
        """Run a single perception/decision cycle. Returns a small result dict.

        The pixel sentinel is evaluated before the OCR pass so an idle screen costs one
        capture plus two hashes instead of a full inference.
        """
        cfg = cfg or self.store.snapshot()
        hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
        if not hwnd:
            with self._state_lock:
                self._status["wechat"] = None
                self._status["detection"] = None
            return {"ok": False, "reason": "wechat-not-found"}

        if not force:
            probe, _rect, scale = win32.grab_window(hwnd, self._dpi_scale)
            if probe is None:
                return {"ok": False, "reason": "capture-failed"}
            if scale and scale > 0:
                self._dpi_scale = scale
            layout = layout_mod.compute_layout(probe.size[0], probe.size[1], self._dpi_scale)
            if self._pixel_sentinel(probe, layout):
                return {"ok": True, "skipped": "no-change"}

        captured = self.capture_and_analyze(cfg)
        if not captured.get("ok"):
            return {"ok": False, "reason": captured.get("reason")}
        return self._handle_analysis(
            captured["hwnd"],
            captured["rect"],
            captured["layout"],
            captured["image"],
            cfg,
            captured["analysis"],
        )

    def _pixel_sentinel(self, image, layout) -> bool:
        """True when nothing can have changed, so the OCR pass is skipped.

        The 1.x sentinel hashed the chat viewport and the whole sidebar on every cycle.
        That is kept, but a full rescan is forced every second even when a red dot is
        visible, so a stuck badge cannot drive a full OCR pass on every 20 ms tick.
        """
        import numpy as np

        arr = np.asarray(image)
        x1, y1, x2, y2 = layout_mod.chat_crop_box(layout)
        chat_patch = arr[y1:y2, x1:x2]
        chat_hash = hashlib.md5(chat_patch.tobytes()).hexdigest() if chat_patch.size else ""

        sidebar_patch = arr[:, : layout["chat_start_x"]]
        sidebar_hash = hashlib.md5(sidebar_patch.tobytes()).hexdigest() if sidebar_patch.size else ""
        has_red = vision.sidebar_has_red(sidebar_patch)

        now = time.time()
        unchanged = (
            chat_hash == self._last_chat_hash
            and sidebar_hash == self._last_sidebar_hash
        )
        fresh = (now - self._last_full_scan) < PIXEL_SENTINEL_FORCE_SCAN
        skip = unchanged and not has_red and fresh

        if not skip:
            self._last_chat_hash = chat_hash
            self._last_sidebar_hash = sidebar_hash
            self._last_full_scan = now
        return skip

    def invalidate_sentinel(self):
        """Force a full OCR pass on the next cycle."""
        self._last_chat_hash = None
        self._last_sidebar_hash = None
        self._last_full_scan = 0.0

    # -- decision ---------------------------------------------------------
    def _handle_analysis(self, hwnd, rect, layout, image, cfg, analysis):
        target = analysis.active_target
        if not target:
            return self._consider_unread_candidates(hwnd, layout, cfg, analysis)

        if not analysis.pending or not analysis.pending_text.strip():
            if analysis.bubbles and analysis.bubbles[-1].is_incoming:
                # The newest bubble belongs to the friend but was filtered (an image or a
                # transfer). Stay on this conversation rather than jumping away.
                return {"ok": True, "reason": "awaiting-content", "target": target}
            return self._consider_unread_candidates(hwnd, layout, cfg, analysis)

        return self._reply_to(hwnd, layout, cfg, target, analysis.pending_text, analysis)

    def _reply_to(self, hwnd, layout, cfg, target, message_text, analysis):
        digest = message_hash(message_text)
        contact = self._contact(target)
        if contact.seen(digest):
            return {"ok": True, "reason": "duplicate", "target": target}
        if contact.in_cooldown(digest):
            return {"ok": True, "reason": "cooldown", "target": target}

        logutil.log(
            "\n[IN] 收到好友【%s】新消息(%s条): \"%s\""
            % (target, len(analysis.pending), message_text),
        )

        with self._state_lock:
            self._status["phase"] = "generating"
        try:
            result = ai.get_client().complete(
                cfg, target, message_text, self.memory.get(target)
            )
        except Exception as exc:
            contact.note_failure(digest)
            logutil.get_bus().warn("[AI 生成失败] 好友【%s】: %s" % (target, exc))
            with self._state_lock:
                self._status["phase"] = "watching"
                self._status["lastError"] = str(exc)
            self._emit("engine.ai_failed", {"target": target, "message": str(exc)})
            return {"ok": False, "reason": "ai-failed", "target": target}

        reply = result.reply
        if not reply:
            contact.remember(digest)
            logutil.get_bus().warn("[AI 生成跳过] 好友【%s】返回了空回复" % target)
            with self._state_lock:
                self._status["phase"] = "watching"
            return {"ok": False, "reason": "empty-reply", "target": target}

        prefix = cfg.get("reply_prefix", "") if cfg.get("include_prefix", False) else ""
        final_reply = "%s%s" % (prefix or "", reply)
        logutil.log("[AI 回复] 好友【%s】 -> %s (%.2fs)" % (target, final_reply, result.elapsed))

        with self._state_lock:
            dry_run = bool(self._status.get("dryRun"))
        if dry_run:
            logutil.log("[演练模式] 已生成回复但未发送: %s" % final_reply)
            contact.remember(digest)
            self.memory.append(target, message_text, reply)
            self._record_reply(target, final_reply, dry_run=True)
            return {"ok": True, "reason": "dry-run", "target": target}

        delay = self._typing_delay(cfg)
        if delay > 0:
            time.sleep(delay)

        with self._state_lock:
            self._status["phase"] = "sending"
        success, detail = self.send_reply(hwnd, layout, target, final_reply, cfg)
        with self._state_lock:
            self._status["phase"] = "watching"

        if success:
            contact.remember(digest)
            contact.clear_failure()
            self.memory.append(target, message_text, reply)
            self._record_reply(target, final_reply, dry_run=False)
            return {"ok": True, "reason": "sent", "target": target, "detail": detail}

        attempts = contact.note_failure(digest)
        if attempts >= MAX_SEND_ATTEMPTS:
            contact.remember(digest)
            contact.clear_failure()
            logutil.get_bus().error(
                "[熔断] 好友【%s】连续 %s 次发送失败，已放弃该条回复以防死循环。"
                % (target, attempts),
            )
        else:
            logutil.log(
                "[发送失败] 好友【%s】第 %s 次发送未成功，稍后自动补偿重试。" % (target, attempts)
            )
        self._emit("engine.send_failed", {"target": target, "detail": detail})
        return {"ok": False, "reason": "send-failed", "target": target, "detail": detail}

    def _typing_delay(self, cfg) -> float:
        try:
            base = float(cfg.get("reply_delay_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            base = 0.0
        try:
            jitter = float(cfg.get("reply_delay_jitter_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            jitter = 0.0
        delay = max(0.0, min(120.0, base))
        if jitter > 0:
            delay += random.uniform(0.0, min(60.0, jitter))
        return delay

    def _record_reply(self, target, text, dry_run=False):
        with self._state_lock:
            if not dry_run:
                self._status["replies"] += 1
            self._status["lastReplyAt"] = time.time()
            self._status["lastReplyTarget"] = target
            self._status["lastReplyText"] = text
        self._emit("engine.replied", {"target": target, "text": text, "dryRun": dry_run})

    def _consider_unread_candidates(self, hwnd, layout, cfg, analysis):
        """Switch to a whitelisted contact with an unread badge.

        Only reached when the current conversation has nothing to answer, which preserves
        the 1.x guarantee that an open replyable conversation is never abandoned mid-thread.
        """
        now = time.time()
        skip_current = analysis.active_target
        for row in analysis.rows:
            if not row.has_red_badge:
                continue
            if skip_current and row.canonical == skip_current:
                continue
            if row.canonical == analysis.title:
                continue
            last = self._candidate_clicked.get(row.canonical, 0.0)
            if (now - last) < CANDIDATE_COOLDOWN:
                continue
            self._candidate_clicked[row.canonical] = now
            logutil.log(
                "\n[UNREAD] 发现白名单好友【%s】未读红点，正在后台切入..." % row.canonical
            )
            click_x = layout_mod.sidebar_click_x(layout)
            win32.post_click_client_point(hwnd, click_x, int(row.y))
            # Let Qt finish rendering the switched conversation before the next OCR pass.
            time.sleep(0.35)
            self.invalidate_sentinel()
            return {"ok": True, "reason": "switched", "target": row.canonical}
        return {"ok": True, "reason": "idle"}

    # -- sending ----------------------------------------------------------
    def send_reply(self, hwnd, layout, target, final_reply, cfg):
        """Deliver a reply. Returns (success, detail).

        Pre-flight: the conversation must actually be the target and the text must be a
        single line, because the paste-then-Enter sequence would otherwise send the first
        paragraph and leave the rest in the input box.
        """
        final_reply = ai.clean_ai_reply(final_reply, keep_newlines=False)
        if not final_reply:
            return False, "empty-reply"

        image_now, _rect, _scale = win32.grab_window(hwnd, self._dpi_scale)
        if image_now is None:
            logutil.get_bus().warn("[发送前核验失败] 无法获取微信画面，终止发送防误触")
            return False, "preflight-capture-failed"

        width, height = image_now.size
        current_layout = layout_mod.compute_layout(width, height, self._dpi_scale)
        results, _ratio = ocr.get_engine().infer_downscaled(image_now, max_width=1000)
        current_title = vision.extract_active_chat_title(results, current_layout)

        if current_title and not naming.match_contact(current_title, target)[0]:
            logutil.get_bus().warn(
                "[发送前纠偏] 当前窗口为【%s】，尝试切回【%s】..." % (current_title, target)
            )
            if not self._recenter_on_target(hwnd, current_layout, results, target):
                logutil.get_bus().error(
                    "[安全熔断] 未能在会话列表中定位【%s】，放弃本次发送以防串发！" % target
                )
                return False, "target-mismatch"

        mode = str(cfg.get("input_injection_mode") or "auto")
        if mode in ("auto", "background"):
            ok, detail = self._send_background(hwnd, current_layout, final_reply)
            if ok:
                return True, detail
            if mode == "background":
                return False, detail
            logutil.log("[注入回退] 后台注入未通过校验(%s)，改用前台注入" % detail)

        return self._send_foreground(hwnd, current_layout, final_reply)

    def _recenter_on_target(self, hwnd, layout, results, target) -> bool:
        """Click the target inside the conversation list. Returns False if not found."""
        sidebar_w = layout["sidebar_w"]
        chat_start_x = layout["chat_start_x"]
        y_min, y_max = layout_mod.session_rows_band(layout)
        for box, text, _score in results or ():
            cx = (box[0][0] + box[1][0]) / 2.0
            cy = (box[0][1] + box[2][1]) / 2.0
            if sidebar_w <= cx <= chat_start_x and y_min <= cy <= y_max:
                if naming.match_contact(str(text or "").strip(), target)[0]:
                    win32.post_click_client_point(hwnd, int(0.16 * layout["total_w_px"]), int(cy))
                    time.sleep(0.12)
                    logutil.log("[后台静默纠偏] 已重新切入【%s】" % target)
                    return True
        return False

    def _send_background(self, hwnd, layout, text):
        """Type and send without touching the foreground window.

        Qt receives WM_CHAR on its focused input widget and WM_KEYDOWN for Enter. No
        cursor movement, no z-order change, no focus steal.
        """
        input_x, input_y = layout_mod.input_box_point(layout)
        if not win32.post_click_client_point(hwnd, input_x, input_y):
            return False, "background-click-failed"
        time.sleep(0.04)

        image_pre, _rect, _scale = win32.grab_window(hwnd, self._dpi_scale)
        if not win32.post_text_background(hwnd, text):
            return False, "background-type-failed"
        time.sleep(0.06)
        win32.post_key(hwnd, win32.VK_RETURN, settle=0.02)
        time.sleep(0.35)
        image_post, _rect, _scale = win32.grab_window(hwnd, self._dpi_scale)
        verdict = vision.verify_diff_bubble_success(image_pre, image_post, layout)
        if verdict is True:
            logutil.log("[OK] 后台静默秒回成功（物理鼠标 0 移动，前台窗口未被打断）")
            return True, "background"
        if verdict == "UNCERTAIN":
            logutil.get_bus().warn(
                "[闭环验收异常] 画面抓取受阻无法比对，按已发送处理并登记防重复。"
            )
            return True, "background-unverified"
        return False, "background-unverified-delta"

    def _send_foreground(self, hwnd, layout, text):
        """Foreground fallback: activate, paste, Enter, verify, restore focus.

        The 1.x path, with the same hard safety check: the foreground window must be
        WeChat immediately before the keystrokes are synthesised, and immediately after
        the input box is clicked. Any mismatch aborts instead of typing into whatever the
        user had open.
        """
        original = win32.get_foreground_window()
        pyperclip = None
        previous_clipboard = None
        try:
            import pyperclip as _pyperclip

            pyperclip = _pyperclip
            previous_clipboard = pyperclip.paste()
        except Exception:
            pyperclip = None

        success = False
        unverified = False
        try:
            for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
                if pyperclip is None:
                    return False, "pyperclip-missing"
                if not _safe_clipboard_copy(pyperclip, text):
                    logutil.get_bus().warn(
                        "[剪贴板占用] 写入失败 (尝试 %s/%s)，稍后重试" % (attempt, MAX_SEND_ATTEMPTS)
                    )
                    time.sleep(0.05)
                    continue
                time.sleep(0.02)

                image_pre, _rect, _scale = win32.grab_window(hwnd, self._dpi_scale)
                win32.activate_window(hwnd)
                time.sleep(0.03)

                if not win32.is_foreground(hwnd):
                    logutil.get_bus().warn(
                        "[安全熔断] 微信未取得前台焦点 (尝试 %s/%s)，终止按键防串窗！"
                        % (attempt, MAX_SEND_ATTEMPTS),
                    )
                    time.sleep(0.15)
                    continue

                input_x, input_y = layout_mod.input_box_point(layout)
                win32.post_click_client_point(hwnd, input_x, input_y)
                time.sleep(0.03)
                if not win32.is_foreground(hwnd):
                    logutil.get_bus().warn(
                        "[安全熔断] 点击输入框后焦点丢失 (尝试 %s/%s)，终止击键！"
                        % (attempt, MAX_SEND_ATTEMPTS),
                    )
                    continue

                win32.send_key_combo(hwnd, [win32.VK_CONTROL, win32.VK_V])
                win32.send_key_combo(hwnd, [win32.VK_RETURN])
                time.sleep(0.35)
                image_post, _rect, _scale = win32.grab_window(hwnd, self._dpi_scale)
                verdict = vision.verify_diff_bubble_success(image_pre, image_post, layout)
                if verdict is True:
                    success = True
                    break
                if verdict == "UNCERTAIN":
                    logutil.get_bus().warn(
                        "[闭环验收异常] 无法比对画面，按已发送处理以防重复发送。"
                    )
                    success = True
                    unverified = True
                    break
                logutil.get_bus().warn(
                    "[闭环验收未通过] 未检测到新的绿底气泡增量 (尝试 %s/%s)"
                    % (attempt, MAX_SEND_ATTEMPTS),
                )
                time.sleep(0.12)
        finally:
            if pyperclip is not None and previous_clipboard is not None:
                _safe_clipboard_copy(pyperclip, previous_clipboard, retries=2)
            if original and original != hwnd:
                win32.restore_foreground(original)

        if success and unverified:
            logutil.get_bus().warn("[发送未验证] 已按发出处理并登记防重复，请留意确认。")
            return True, "foreground-unverified"
        if success:
            logutil.log("[OK] 前台静默秒回成功，已恢复原前台窗口")
            return True, "foreground"
        return False, "foreground-failed"


def _safe_clipboard_copy(pyperclip_module, text, retries: int = 3, delay: float = 0.03) -> bool:
    """Clipboard write with backoff for Win+V history and AV scanners holding it."""
    for attempt in range(retries):
        try:
            pyperclip_module.copy(text)
            return True
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
    return False
