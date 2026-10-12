"""Window-level operations exposed to the UI.

Port of the 1.x GUI helpers: context-menu-free show/hide, off-screen park and recall,
live screenshots, and process discovery. Two corrections:

* **The off-screen state was inferred from coordinates.** The 1.x toggle decided "is it
  hidden?" by testing rect.left < -500. Recalling a window that Windows had merely moved
  to a secondary monitor with a negative origin (a monitor placed to the left of the
  primary) restored it to the wrong place. The state is now recorded when it is applied.
* **The recall geometry was never validated.** A saved rectangle from an unplugged
  monitor put WeChat off-screen again. clamp_restore_rect pulls it back.
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import time

from . import config as config_mod, logutil, paths, win32


def _store() -> config_mod.ConfigStore:
    return config_mod.get_store()


def wechat_status() -> dict:
    info = win32.describe_wechat()
    if not info:
        return {"found": False}
    info = dict(info)
    info["found"] = True
    info["dpiScale"] = win32.get_dpi_scale()
    saved = _store().get("saved_window_rect") or [100, 100, 1100, 750]
    width, height = win32.get_screen_size()
    info["savedRect"] = saved
    info["suggestedRestoreRect"] = win32.clamp_restore_rect(saved, width, height)
    info["screen"] = {"width": width, "height": height}
    info["recordedOffscreen"] = bool(_store().get("offscreen"))
    return info


def capture_window(scale_to_width: int = 0, include_base64: bool = True, save_path: str = ""):
    """Grab the WeChat window for the UI preview, even while it is off-screen."""
    from PIL import Image

    hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
    if not hwnd:
        return {"ok": False, "reason": "wechat-not-found"}

    image, rect, scale = win32.grab_window(hwnd)
    if image is None:
        return {"ok": False, "reason": "capture-failed", "hwnd": hwnd}

    width, height = image.size
    if scale_to_width and 0 < scale_to_width < width:
        ratio = scale_to_width / float(width)
        image = image.resize((scale_to_width, max(1, int(height * ratio))), Image.Resampling.BILINEAR)

    written = None
    if save_path:
        try:
            target = os.path.abspath(save_path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            image.save(target, format="PNG")
            written = target
        except Exception as exc:
            logutil.get_bus().warn("screenshot save failed: %s" % exc)

    payload = {
        "ok": True,
        "hwnd": hwnd,
        "rect": list(rect) if rect else None,
        "dpiScale": scale,
        "width": image.size[0],
        "height": image.size[1],
        "savedPath": written,
    }
    if include_base64:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        payload["dataUrl"] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return payload


def set_offscreen(hidden: bool, force: bool = False) -> dict:
    """Park WeChat in the off-screen area, or bring it back."""
    store = _store()
    hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
    if not hwnd:
        return {"ok": False, "reason": "wechat-not-found"}

    rect = win32.get_window_rect(hwnd)
    currently_offscreen = win32.is_offscreen(hwnd)
    # hidden=None means toggle.
    target_hidden = (not currently_offscreen) if hidden is None else bool(hidden)

    if target_hidden:
        # Remember where the user keeps WeChat before moving it away.
        if not currently_offscreen:
            if win32.is_minimized(hwnd):
                placement = win32.get_window_placement(hwnd)
                if placement:
                    store.update({"saved_window_rect": placement}, source="offscreen")
            elif rect:
                width = rect[2] - rect[0]
                height = rect[3] - rect[1]
                if width >= 320 and height >= 240 and rect[0] > win32.OFFSCREEN_THRESHOLD and rect[1] > win32.OFFSCREEN_THRESHOLD:
                    store.update({"saved_window_rect": [rect[0], rect[1], width, height]}, source="offscreen")

        saved = store.get("saved_window_rect") or [100, 100, 1100, 750]
        ok = win32.park_offscreen(hwnd, int(saved[2]), int(saved[3]))
        if ok:
            store.update({"offscreen": True}, source="offscreen")
            logutil.log("[离屏] 微信已移入离屏空间 (%sx%s)" % (saved[2], saved[3]))
        return {"ok": bool(ok), "hidden": bool(ok), "savedRect": saved, "reason": "" if ok else "move-failed"}

    screen_w, screen_h = win32.get_screen_size()
    saved = store.get("saved_window_rect") or [100, 100, 1100, 750]
    x, y, width, height = win32.clamp_restore_rect(saved, screen_w, screen_h)
    ok = win32.restore_from_offscreen(hwnd, x, y, width, height)
    if ok:
        store.update({"offscreen": False}, source="offscreen")
        logutil.log("[召回] 微信已还原至 (%s, %s) %sx%s" % (x, y, width, height))
    return {
        "ok": bool(ok),
        "hidden": False,
        "restoredRect": [x, y, width, height],
        "reason": "" if ok else "move-failed",
    }


def toggle_offscreen() -> dict:
    hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
    if not hwnd:
        return {"ok": False, "reason": "wechat-not-found"}
    return set_offscreen(not win32.is_offscreen(hwnd))


def minimize_wechat() -> dict:
    hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
    if not hwnd:
        return {"ok": False, "reason": "wechat-not-found"}
    return {"ok": win32.show_window(hwnd, win32.SW_MINIMIZE)}


def show_wechat() -> dict:
    hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
    if not hwnd:
        return {"ok": False, "reason": "wechat-not-found"}
    if win32.is_offscreen(hwnd):
        return set_offscreen(False)
    win32.show_window(hwnd, win32.SW_RESTORE)
    return {"ok": True}


def open_wechat() -> dict:
    """Launch WeChat if it is not running.

    The 1.x GUI never offered this, so a user who closed WeChat had to find the shortcut
    themselves while the dashboard kept reporting "not detected".
    """
    hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
    if hwnd:
        show_wechat()
        return {"ok": True, "alreadyRunning": True}

    candidates = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.extend([
            os.path.join(base, "Tencent", "WeChat", "WeChat.exe"),
            os.path.join(base, "Tencent", "Weixin", "Weixin.exe"),
        ])
    for candidate in candidates:
        if os.path.isfile(candidate):
            try:
                subprocess.Popen([candidate], close_fds=True)
            except Exception as exc:
                return {"ok": False, "reason": str(exc)}
            logutil.log("[启动] 已尝试启动微信: %s" % candidate)
            deadline = time.time() + 15
            while time.time() < deadline:
                time.sleep(0.5)
                hwnd, _version = win32.get_wechat_hwnd(hide_shell=False)
                if hwnd:
                    return {"ok": True, "launched": candidate}
            return {"ok": True, "launched": candidate, "windowPending": True}

    # Last resort: let the shell resolve the registered protocol handler.
    try:
        os.startfile("weixin://")  # noqa: S606 - Windows shell association
    except Exception:
        pass
    return {"ok": False, "reason": "wechat-executable-not-found"}


def engine_host_info() -> dict:
    """Diagnostics shown in the app About/diagnostics panel."""
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": sys.platform,
        "pid": os.getpid(),
        "dataDir": str(paths.data_dir()),
        "installDir": str(paths.install_dir()),
        "configFile": str(paths.config_file()),
        "logFile": str(paths.log_file()),
        "modelsDir": str(paths.models_dir()),
        "dpiScale": win32.get_dpi_scale(),
        "dpiAwareness": win32.enable_dpi_awareness(),
        "isWindows": win32.IS_WINDOWS,
    }
