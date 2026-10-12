"""Window-size invariant layout engine.

The 1.x engine called compute_wechat_layout(W, H) and then used the returned numbers to
slice raw pixel bands with hard-coded sizes: a 40 px bubble-merge threshold, a 220/80 px
green-bubble band, 15 px padding, a 36 px badge box. Every one of those is a *physical*
pixel count, so on a 200 percent display (common on 4K laptops) all of them halve in
effective size: bubbles stop merging, the green-delta acceptance window misses the new
bubble, and the unread badge falls outside its sampling box.

The threshold constants are therefore exposed as ratios of the window height and
multiplied by the DPI scale, so behaviour is identical at 100, 150 and 250 percent.
"""

from __future__ import annotations

from typing import TypedDict

MIN_WINDOW_WIDTH = 320
MIN_WINDOW_HEIGHT = 240


class Layout(TypedDict):
    sidebar_w: int
    chat_start_x: int
    header_h: int
    input_h: int
    total_w_px: int
    total_h_px: int
    scale: float
    bubble_merge_px: float
    band_pad_px: float
    badge_pad_px: float
    bubble_bleed_px: float
    title_scan_ratio: float
    incoming_left_ratio: float
    outgoing_right_ratio: float
    input_safe_x_ratio: float
    input_safe_y_ratio: float
    sidebar_click_ratio: float


def sanitize_scale(value) -> float:
    """Reject DPI probes that returned zero, NaN or an implausible ratio."""
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    if scale != scale or scale <= 0:
        return 1.0
    if not (0.5 <= scale <= 8.0):
        return 1.0
    return scale


def compute_layout(width: int, height: int, scale: float = 1.0) -> Layout:
    """Derive all key regions from the live window size.

    The fractions reproduce WeChat PC native proportions and hold from the minimum
    window size up to a maximised 4K window.
    """
    width = max(MIN_WINDOW_WIDTH, int(width))
    height = max(MIN_WINDOW_HEIGHT, int(height))
    scale = sanitize_scale(scale)

    sidebar_w = max(55, int(0.055 * width))
    chat_start_x = max(320, int(0.285 * width))
    # Guard against thin windows where the two regions would overlap.
    if chat_start_x <= sidebar_w + 40:
        chat_start_x = min(width - 40, sidebar_w + 40)
    header_h = max(85, int(0.110 * height))
    input_h = min(220, max(140, int(0.160 * height)))

    return Layout(
        sidebar_w=sidebar_w,
        chat_start_x=chat_start_x,
        header_h=header_h,
        input_h=input_h,
        total_w_px=width,
        total_h_px=height,
        scale=scale,
        bubble_merge_px=40.0 * scale,
        band_pad_px=15.0 * scale,
        badge_pad_px=36.0 * scale,
        bubble_bleed_px=8.0 * scale,
        title_scan_ratio=0.42,
        incoming_left_ratio=0.55,
        outgoing_right_ratio=0.35,
        input_safe_x_ratio=0.55,
        input_safe_y_ratio=0.55,
        sidebar_click_ratio=0.45,
    )


def chat_crop_box(layout: Layout):
    """Pixel box of the message viewport, excluding header and input toolbar."""
    return (
        max(330, int(layout["chat_start_x"] + 10)),
        max(0, int(layout["header_h"] + 10)),
        min(layout["total_w_px"], int(layout["total_w_px"] - 5)),
        min(layout["total_h_px"], int(layout["total_h_px"] - int(layout["input_h"] * 0.70))),
    )


def sidebar_click_x(layout: Layout) -> int:
    """Horizontal centre of the conversation list rows."""
    return int(
        layout["sidebar_w"]
        + layout["sidebar_click_ratio"] * (layout["chat_start_x"] - layout["sidebar_w"])
    )


def input_box_point(layout: Layout):
    """A point inside the text input that is clear of both toolbars."""
    return (
        int(layout["input_safe_x_ratio"] * layout["total_w_px"]),
        int(layout["total_h_px"] - layout["input_h"] * layout["input_safe_y_ratio"]),
    )


def session_rows_band(layout: Layout):
    """Vertical band of the sidebar conversation list as (y_min, y_max)."""
    return (int(layout["header_h"] + 5), int(layout["total_h_px"] - 20))


def green_metric_bands(layout: Layout):
    """The two bands used for send verification.

    Returns ((y1, y2, x1, x2) for the wide band,
             (y1, y2, x1, x2) for the bottom-anchored band).
    Both are scaled so a 200 percent display keeps the same effective coverage.
    """
    width = layout["total_w_px"]
    height = layout["total_h_px"]
    chat_start_x = layout["chat_start_x"]
    input_h = layout["input_h"]
    scale = layout["scale"]

    total_band = int(220 * scale)
    bottom_band = int(80 * scale)

    x1_total = int(chat_start_x + layout["outgoing_right_ratio"] * (width - chat_start_x))
    x2_total = min(width, int(width - 10 * scale))
    x2_bottom = min(width, int(width - 20 * scale))

    total = (
        max(0, int(height - input_h - total_band)),
        min(height, int(height - input_h + 10 * scale)),
        x1_total,
        x2_total,
    )
    bottom = (
        max(0, int(height - input_h - bottom_band)),
        min(height, int(height - input_h)),
        x1_total,
        x2_bottom,
    )
    return total, bottom
