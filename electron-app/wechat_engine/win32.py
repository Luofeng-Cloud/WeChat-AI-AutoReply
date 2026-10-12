"""Win32 interop: window discovery, capture, message routing, geometry.

Everything here is a thin, individually testable wrapper. The 1.x engine inlined all
of it into one 1192-line module, which made the failure modes invisible; the fixes
below are each called out where they live.

Notable corrections versus version 1.x:

* make_lparam() is used for mouse coordinates. The old \`(y << 16) | (x & 0xFFFF)\`
  put the high bits of an out-of-range y into the x field, so a click at y >= 65536
  silently landed somewhere else. Coordinates are also clamped to the client area.
* \`grab_window\` never dereferences a None image. The 1.x version computed
  \`np.array(img).max()\` after PrintWindow had already failed, and because that call
  sat inside the same try block, the resulting exception was swallowed and the caller
  received \`(None, None, 1.0)\` with no diagnostic.
* \`get_dpi_scale\` is cached briefly. The 1.x code opened a screen DC on every capture
  and every geometry question, i.e. several times per poll cycle.
* The process is DPI aware before any geometry is measured, and the DPI awareness call
  is validated rather than assumed.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    try:
        shcore = ctypes.windll.shcore
    except Exception:  # pragma: no cover - Windows 7 / Server 2008
        shcore = None
else:  # pragma: no cover - allows importing on CI runners
    user32 = gdi32 = kernel32 = shcore = None

# --- constants -------------------------------------------------------------
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

HWND_TOP = 0
HWND_BOTTOM = 1
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

WPF_RESTORETOMAXIMIZED = 0x0002

DESKTOP_READOBJECTS = 0x0001
DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_ENUMERATE = 0x0040
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_ALL_ACCESS = 0x01FF

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
EM_SETSEL = 0x00B1
CB_SETEDITSEL = 0x0142
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

HORZRES = 8
DESKTOPHORZRES = 118

# WeChat's Qt window class names. 3.x ships WeChatMainWndForPC, 4.x uses a
# Qt-generated class; both are matched as substrings because the Qt suffix changes
# between builds.
WECHAT_WINDOW_CLASSES = ("Qt51514QWindowIcon", "WeChatMainWndForPC")
WECHAT_SHELL_TITLE = "Weixin"

# Off-screen parking spot. Kept far enough away that DWM still composites it.
OFFSCREEN_POS = (-3000, -3000)
OFFSCREEN_THRESHOLD = -500


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


if IS_WINDOWS:
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)
else:  # pragma: no cover
    _WNDENUMPROC = None


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------
_dpi_lock = threading.Lock()
_dpi_cache = {"value": 1.0, "at": 0.0}
_DPI_TTL_SECONDS = 1.0


def enable_dpi_awareness() -> str:
    """Make the process per-monitor DPI aware. Returns the mode actually applied."""
    if not IS_WINDOWS:
        return "unsupported"
    # PROCESS_PER_MONITOR_DPI_AWARE = 2
    if shcore is not None:
        try:
            if shcore.SetProcessDpiAwareness(2) == 0:
                return "per-monitor"
        except Exception:
            pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "none"


def ensure_default_desktop() -> bool:
    """Bind the calling thread to the interactive desktop.

    A thread that starts on a different window station (a service, a scheduled task)
    cannot enumerate or move WeChat's window. Re-binding is cheap and idempotent.
    """
    if not IS_WINDOWS:
        return False
    try:
        handle = user32.OpenDesktopW("Default", 0, False, DESKTOP_ALL_ACCESS)
        if not handle:
            return False
        try:
            user32.SetThreadDesktop(handle)
            return True
        finally:
            user32.CloseDesktop(handle)
    except Exception:
        return False


def get_dpi_scale(force: bool = False) -> float:
    """Physical pixels per logical pixel for the primary display."""
    if not IS_WINDOWS:
        return 1.0
    now = time.monotonic()
    with _dpi_lock:
        if not force and (now - _dpi_cache["at"]) < _DPI_TTL_SECONDS:
            return _dpi_cache["value"]
    scale = 1.0
    hdc = None
    try:
        hdc = user32.GetDC(0)
        if hdc:
            logical = gdi32.GetDeviceCaps(hdc, HORZRES)
            physical = gdi32.GetDeviceCaps(hdc, DESKTOPHORZRES)
            if logical and physical:
                candidate = physical / float(logical)
                if candidate > 0:
                    scale = candidate
    except Exception:
        scale = 1.0
    finally:
        if hdc:
            try:
                user32.ReleaseDC(0, hdc)
            except Exception:
                pass
    with _dpi_lock:
        _dpi_cache["value"] = scale
        _dpi_cache["at"] = now
    return scale


# ---------------------------------------------------------------------------
# Window discovery
# ---------------------------------------------------------------------------
def list_top_level_windows():
    """Enumerate every top-level window as (hwnd, class_name, title)."""
    if not IS_WINDOWS:
        return []
    found = []

    def _proc(hwnd, _lparam):
        try:
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            found.append((int(hwnd), class_buf.value, title_buf.value))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_proc), 0)
    except Exception:
        return found
    return found


class WeChatWindowInfo:
    __slots__ = ("hwnd", "version", "title", "is_shell", "visible")

    def __init__(self, hwnd, version, title, is_shell, visible=True):
        self.hwnd = hwnd
        self.version = version
        self.title = title
        self.is_shell = is_shell
        self.visible = visible

    def to_dict(self):
        return {
            "hwnd": self.hwnd,
            "version": self.version,
            "title": self.title,
            "isShell": self.is_shell,
            "visible": self.visible,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "WeChatWindowInfo(hwnd=%s, v%s, title=%r, shell=%s)" % (
            self.hwnd,
            self.version,
            self.title,
            self.is_shell,
        )


def find_wechat_windows(hide_shell: bool = True):
    """All matching WeChat top-level windows, real chat window first.

    WeChat 4.x spawns a companion shell window titled exactly "Weixin" that has no
    content. The 1.x code hid it as a side effect of *enumeration*, which meant any
    status poll (once per 1.5 s in the GUI, once per cycle in the daemon) could hide a
    window the user had deliberately opened. Hiding is now explicit and opt-in.
    """
    candidates = []
    for hwnd, class_name, title in list_top_level_windows():
        if not any(marker in class_name for marker in WECHAT_WINDOW_CLASSES):
            continue
        is_shell = title == WECHAT_SHELL_TITLE
        version = 4 if "Qt" in class_name else 3
        visible = True
        try:
            visible = bool(user32.IsWindowVisible(hwnd))
        except Exception:
            pass
        candidates.append(WeChatWindowInfo(hwnd, version, title, is_shell, visible))

    real = [c for c in candidates if not c.is_shell]
    ordered = real + [c for c in candidates if c.is_shell]

    if hide_shell:
        for info in ordered:
            if info.is_shell and info.visible:
                try:
                    user32.ShowWindow(info.hwnd, SW_HIDE)
                    info.visible = False
                except Exception:
                    pass
    return ordered


def get_wechat_hwnd(hide_shell: bool = False):
    """(hwnd, version) of the real chat window, or (None, None)."""
    ensure_default_desktop()
    windows = find_wechat_windows(hide_shell=hide_shell)
    if not windows:
        return None, None
    return windows[0].hwnd, windows[0].version


def describe_wechat() -> Optional[dict]:
    """Status payload for the UI, without touching shell windows."""
    ensure_default_desktop()
    windows = find_wechat_windows(hide_shell=False)
    if not windows:
        return None
    primary = windows[0]
    rect = get_window_rect(primary.hwnd)
    placement = get_window_placement(primary.hwnd)
    return {
        "hwnd": primary.hwnd,
        "version": primary.version,
        "title": primary.title,
        "visible": primary.visible,
        "minimized": is_minimized(primary.hwnd),
        "offscreen": is_offscreen(primary.hwnd),
        "rect": list(rect) if rect else None,
        "restore_rect": placement,
        "shellWindows": [w.to_dict() for w in windows if w.is_shell],
        "allWindows": [w.to_dict() for w in windows],
    }


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def get_window_rect(hwnd) -> Optional[Tuple[int, int, int, int]]:
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None


def get_window_size(hwnd) -> Tuple[int, int]:
    rect = get_window_rect(hwnd)
    if not rect:
        return 0, 0
    return rect[2] - rect[0], rect[3] - rect[1]


def get_window_placement(hwnd):
    """Normal (pre-minimise) position and size: [x, y, w, h] or None.

    Used as the authoritative restore geometry when WeChat is currently minimised or
    parked off-screen, because GetWindowRect then reports meaningless coordinates.
    """
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        placement = WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return None
        rect = placement.rcNormalPosition
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 320 or height < 240:
            return None
        if rect.left < -5000 or rect.top < -5000:
            return None
        return [int(rect.left), int(rect.top), int(width), int(height)]
    except Exception:
        return None


def is_minimized(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(user32.IsIconic(hwnd))
    except Exception:
        return False


def is_window_visible(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def is_offscreen(hwnd) -> bool:
    """True only when the window is parked *and* not merely minimised."""
    rect = get_window_rect(hwnd)
    if not rect:
        return False
    if is_minimized(hwnd):
        return False
    return rect[0] < OFFSCREEN_THRESHOLD or rect[1] < OFFSCREEN_THRESHOLD


def set_window_rect(hwnd, x, y, width, height, activate: bool = False) -> bool:
    """Move/size a window without activating it unless asked."""
    if not IS_WINDOWS or not hwnd:
        return False
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    if not activate:
        flags |= SWP_NOACTIVATE
    try:
        return bool(user32.SetWindowPos(hwnd, HWND_TOP, int(x), int(y), int(width), int(height), flags))
    except Exception:
        return False


def park_offscreen(hwnd, width: int, height: int) -> bool:
    """Restore from minimise then move the window into the off-screen area.

    Order matters: SetWindowPos on a minimised window applies to the icon, so the 1.x
    code had to call SW_RESTORE first. It then relied on the caller to have saved a
    good geometry; if the window was minimised the saved rect came from a stale
    GetWindowRect. Both steps are now explicit.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        if is_minimized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.04)
        return set_window_rect(hwnd, OFFSCREEN_POS[0], OFFSCREEN_POS[1], width, height)
    except Exception:
        return False


def restore_from_offscreen(hwnd, x, y, width, height) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        if is_minimized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.04)
        return set_window_rect(hwnd, x, y, width, height)
    except Exception:
        return False


def clamp_restore_rect(rect, screen_width: int, screen_height: int):
    """Pull a remembered position back onto a screen that still exists.

    A laptop undocked from a 2560-wide monitor would otherwise restore WeChat to
    coordinates outside the visible desktop.
    """
    try:
        x, y, width, height = (int(v) for v in rect[:4])
    except (TypeError, ValueError, IndexError):
        return [100, 100, 1100, 750]
    width = max(320, min(width, max(640, screen_width)))
    height = max(240, min(height, max(480, screen_height)))
    if x < 0 or x > max(0, screen_width - 200):
        x = max(20, int((screen_width - width) / 2))
    if y < 0 or y > max(0, screen_height - 200):
        y = max(20, int((screen_height - height) / 2))
    return [x, y, width, height]


def get_screen_size() -> Tuple[int, int]:
    if not IS_WINDOWS:
        return 1920, 1080
    try:
        return (
            int(user32.GetSystemMetrics(0)),
            int(user32.GetSystemMetrics(1)),
        )
    except Exception:
        return 1920, 1080


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------
def make_lparam(x: int, y: int) -> int:
    """Pack client coordinates into a Win32 LPARAM, clamping to 16-bit fields."""
    cx = max(0, min(0xFFFF, int(x)))
    cy = max(0, min(0xFFFF, int(y)))
    return ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)


def post_click_client_point(hwnd, client_x, client_y, settle: float = 0.03) -> bool:
    """Click a client-area point via posted messages.

    The physical cursor is never moved: no SetCursorPos, no SendInput. WeChat's Qt
    event loop handles these exactly like real clicks as long as the target widget is
    hit-tested by the coordinates, which is why the caller must derive them from the
    live window size.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        lparam = make_lparam(client_x, client_y)
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        time.sleep(0.01)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 0x0001, lparam)
        time.sleep(0.02)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0x0000, lparam)
        time.sleep(settle)
        return True
    except Exception:
        return False


def is_foreground(hwnd) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def get_foreground_window():
    if not IS_WINDOWS:
        return None
    try:
        return user32.GetForegroundWindow()
    except Exception:
        return None


def activate_window(hwnd) -> bool:
    """Bring a window to the foreground across thread-input boundaries.

    Windows refuses SetForegroundWindow from a process that does not own the current
    foreground. Attaching the input queues of (current foreground thread -> target) and
    (our thread -> target) lifts that restriction for the duration of the call, which is
    the standard AttachThreadInput workaround.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        ensure_default_desktop()
        if is_minimized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.04)

        current = user32.GetForegroundWindow()
        current_thread = user32.GetWindowThreadProcessId(current, None) if current else 0
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        own_thread = kernel32.GetCurrentThreadId()

        attached = []
        try:
            if current_thread and current_thread != target_thread:
                if user32.AttachThreadInput(current_thread, target_thread, True):
                    attached.append(current_thread)
            if own_thread and own_thread != target_thread:
                if user32.AttachThreadInput(own_thread, target_thread, True):
                    attached.append(own_thread)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
        finally:
            for thread_id in attached:
                try:
                    user32.AttachThreadInput(thread_id, target_thread, False)
                except Exception:
                    pass
        time.sleep(0.03)
        return is_foreground(hwnd)
    except Exception:
        return False


def restore_foreground(previous_hwnd) -> bool:
    """Best-effort return of focus to whatever the user was using."""
    if not IS_WINDOWS or not previous_hwnd:
        return False
    try:
        return bool(user32.SetForegroundWindow(previous_hwnd))
    except Exception:
        return False


def send_key_combo(hwnd, virtual_keys, hold: float = 0.02, gap: float = 0.04) -> bool:
    """Synthesise a key combination with keybd_event (requires target foreground).

    keybd_event feeds the global input queue, so the caller must have verified that the
    target window is foreground immediately beforehand. A mismatch here is how a reply
    ends up pasted into the wrong application.
    """
    if not IS_WINDOWS:
        return False
    try:
        for vk in virtual_keys:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(hold)
        for vk in reversed(virtual_keys):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(hold)
        time.sleep(gap)
        return True
    except Exception:
        return False


def post_text_background(hwnd, text: str, char_delay: float = 0.012) -> bool:
    """Type text into a background window with WM_CHAR messages.

    This is the only injection path that touches neither the cursor nor the foreground
    window. Qt accepts WM_CHAR on its focused widget, so this works while the user keeps
    typing elsewhere. It is offered as an opt-in mode because some WeChat builds filter
    synthetic character messages.
    """
    if not IS_WINDOWS or not hwnd or not text:
        return False
    try:
        for char in text:
            user32.PostMessageW(hwnd, WM_CHAR, ord(char), 0)
            if char_delay:
                time.sleep(char_delay)
        return True
    except Exception:
        return False


def post_key(hwnd, virtual_key: int, settle: float = 0.02) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        user32.PostMessageW(hwnd, WM_KEYDOWN, virtual_key, 0)
        time.sleep(settle)
        user32.PostMessageW(hwnd, WM_KEYUP, virtual_key, 0)
        time.sleep(settle)
        return True
    except Exception:
        return False


def window_process_id(hwnd):
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def show_window(hwnd, cmd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        user32.ShowWindow(hwnd, cmd)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def print_window_bitmap(hwnd, width: int, height: int, flags: int = PW_RENDERFULLCONTENT):
    """Capture a window with PrintWindow. Returns a PIL RGB image or None.

    PrintWindow works while the window is covered or parked off-screen, which is what
    makes the whole zero-interference design possible.
    """
    if not IS_WINDOWS or not hwnd or width <= 0 or height <= 0:
        return None
    from PIL import Image

    hwnd_dc = None
    mem_dc = None
    bitmap = None
    try:
        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        if not mem_dc:
            return None
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        if not bitmap:
            return None
        gdi32.SelectObject(mem_dc, bitmap)

        rendered = user32.PrintWindow(hwnd, mem_dc, flags)
        if not rendered and flags != PW_CLIENTONLY:
            rendered = user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY)
        if not rendered:
            return None

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # negative: top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
        if not copied:
            return None
        return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
    except Exception:
        return None
    finally:
        try:
            if bitmap:
                gdi32.DeleteObject(bitmap)
        except Exception:
            pass
        try:
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
        except Exception:
            pass
        try:
            if hwnd_dc:
                user32.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def grab_window(hwnd, dpi_scale: float = None, allow_screen_fallback: bool = True):
    """Capture a window as (PIL.Image, rect_tuple, dpi_scale).

    Returns (None, None, scale) when nothing usable could be captured, and the caller
    can distinguish "capture failed" from "captured an all-black frame" by checking the
    image. An all-black PrintWindow result is reported as None rather than being passed
    upstream, because a black frame silently feeds the OCR a blank screen.
    """
    from PIL import ImageGrab

    rect = get_window_rect(hwnd)
    if rect is None:
        return None, None, dpi_scale or get_dpi_scale()
    scale = dpi_scale if dpi_scale else get_dpi_scale()

    left, top, right, bottom = rect
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        width, height = 1100, 750

    image = print_window_bitmap(hwnd, width, height)
    if image is not None and _has_content(image):
        return image, rect, scale

    if allow_screen_fallback:
        # Only meaningful while the window is on the visible desktop.
        visible = left > OFFSCREEN_THRESHOLD and top > OFFSCREEN_THRESHOLD
        if visible:
            try:
                bbox = (
                    int(left * scale),
                    int(top * scale),
                    int(right * scale),
                    int(bottom * scale),
                )
                screen = ImageGrab.grab(bbox=bbox)
                if screen is not None:
                    screen = screen.convert("RGB")
                    if _has_content(screen):
                        return screen, rect, scale
                    return screen, rect, scale
            except Exception:
                pass
    return None, rect, scale


def _has_content(image) -> bool:
    """True when the frame is not uniformly black (PrintWindow's failure mode)."""
    try:
        import numpy as np

        return bool(np.asarray(image).max() > 10)
    except Exception:
        return True
