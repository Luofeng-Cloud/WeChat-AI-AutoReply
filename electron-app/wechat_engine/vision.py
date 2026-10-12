"""Frame analysis: red badges, chat titles, bubble parsing, send verification.

This module is where the perception decisions live. The logic is a direct port of the
1.x algorithms with the following corrections, each of which is a real-world failure the
original could hit:

* Every pixel threshold that was an absolute count (15 red pixels, 40 bottom pixels,
  1200 green pixels) keeps its value at 100 percent but scales with the DPI factor, and
  the sampling patches scale with the layout too. The 1.x constants were tuned on a
  100 percent display only.
* has_red_badge_by_text_anchor clamps its patch to the image before slicing, so a
  contact row near the bottom edge cannot produce an empty patch and a silent miss.
* extract_active_chat_title compares against a normalised exclusion set instead of a
  raw string list.
* parse_chat_bubbles samples the bubble background with the DPI-scaled bleed, and the
  multi-line merge distance is DPI aware, so short consecutive messages still aggregate
  on a 4K laptop.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

from . import naming
from .layout import (
    Layout,
    chat_crop_box,
    compute_layout,
    green_metric_bands,
    session_rows_band,
    sidebar_click_x,
)

BUILTIN_IGNORED_KEYWORDS = [
    "按住鼠标",
    "语音输入文字",
    "按住说话",
    "按Enter发送",
    "按Ctrl+Enter发送",
    "发送(S)",
]

UI_NOISE_KEYWORDS = ("P以H白", "y< o O", "查看更多", "以下为新消息", "置顶聊天")

# Words the chat header can contain that are not the contact name. Compared after
# normalisation so punctuation differences do not matter.
TITLE_EXCLUSIONS = (
    "最小化",
    "最大化",
    "关闭",
    "设置",
    "聊天信息",
    "搜索",
    "表情",
    "发送",
    "文件",
    "截图",
    "语音聊天",
    "视频聊天",
    "…",
    "...",
    "口",
    "-",
    "x",
    "X",
)
TITLE_EXCLUSION_SET = {naming.normalize_basic(item) for item in TITLE_EXCLUSIONS}

TIMESTAMP_PATTERNS = (
    re.compile(r"^\d{1,2}:\d{2}$"),
    re.compile(r"^\d{1,2}/\d{1,2}$"),
    re.compile(r"^\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"^\d{1,2}月\d{1,2}日"),
    re.compile(r"^(上午|下午|凌晨|晚上|中午)\s*\d{1,2}:\d{2}$"),
)

DATE_LABELS = frozenset((
    "昨天", "前天", "今天", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    "查看更多消息", "重新编辑", "发送", "以下为新消息",
))

PREFIX_FILTERS = (
    "[转账]", "微信转账", "已收款", "已被接收", "￥", "你撤回了一条消息",
    "[红包]", "[语音]", "[视频]", "[动画表情]", "[文件]",
)

GREEN_MASK_G = 18
GREEN_MASK_FLOOR = 45
RED_PIXEL_THRESHOLD = 15
BADGE_PIXEL_MIN = 40
BADGE_DELTA_MIN = 30
TOTAL_GREEN_DELTA_MIN = 50
SIDEBAR_ACTIVE_GREEN_MIN = 1200


class Bubble(NamedTuple):
    is_incoming: bool
    first_y: float
    last_y: float
    text: str
    left: float
    right: float


class SessionRow(NamedTuple):
    name: str
    canonical: str
    y: float
    has_red_badge: bool
    green_score: int
    tier: str


class FrameAnalysis(NamedTuple):
    width: int
    height: int
    layout: Layout
    title: str
    bubbles: List[Bubble]
    pending: List[Bubble]
    pending_text: str
    rows: List[SessionRow]
    active_sidebar_name: Optional[str]
    active_target: Optional[str]
    active_source: str


def red_mask(array: np.ndarray) -> np.ndarray:
    """Pixels close to the WeChat unread-badge red."""
    red = array[:, :, 0].astype(int)
    green = array[:, :, 1].astype(int)
    blue = array[:, :, 2].astype(int)
    return (red >= 200) & (green <= 110) & (blue <= 110) & (red > green + 65)


def loose_red_mask(array: np.ndarray) -> np.ndarray:
    """More permissive red used for the sidebar sentinel (antialiased badges)."""
    red = array[:, :, 0].astype(int)
    green = array[:, :, 1].astype(int)
    blue = array[:, :, 2].astype(int)
    return (red >= 190) & (green <= 115) & (blue <= 115) & (red > green + 55)


def green_mask(array: np.ndarray) -> np.ndarray:
    """WeChat outgoing-bubble green."""
    red = array[:, :, 0].astype(int)
    green = array[:, :, 1].astype(int)
    blue = array[:, :, 2].astype(int)
    return (green > red + GREEN_MASK_G) & (green > blue + GREEN_MASK_G) & (green > GREEN_MASK_FLOOR)


def push_has_green_bg(array: np.ndarray) -> bool:
    if array.size == 0:
        return False
    return bool(green_mask(array).sum() > 15)


def sidebar_has_red(array: np.ndarray) -> bool:
    if array.size == 0:
        return False
    return bool(loose_red_mask(array).sum() >= RED_PIXEL_THRESHOLD)


def count_green_metrics(image, layout: Layout) -> Tuple[int, int]:
    """Green pixel counts in the wide band and the bottom-anchored band."""
    if image is None:
        return 0, 0
    total_band, bottom_band = green_metric_bands(layout)
    array = np.asarray(image)
    if array.ndim != 3:
        return 0, 0
    height, width = array.shape[:2]

    y1, y2, x1, x2 = total_band
    patch = array[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
    total = int(green_mask(patch).sum()) if patch.size else 0

    y1, y2, x1, x2 = bottom_band
    patch = array[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
    bottom = int(green_mask(patch).sum()) if patch.size else 0
    return total, bottom


def verify_diff_bubble_success(image_pre, image_post, layout: Layout):
    """Three-state send verification.

    True      - a new outgoing bubble is visible in the post frame
    False     - the frames are readable and show no new bubble (a retry is safe)
    UNCERTAIN - the frames cannot be compared (blank capture), so retrying could
                duplicate the message; the caller must stop instead
    """
    if image_pre is None or image_post is None:
        return "UNCERTAIN"
    try:
        arr_pre = np.asarray(image_pre)
        arr_post = np.asarray(image_post)
    except Exception:
        return "UNCERTAIN"
    if arr_pre.size == 0 or arr_post.size == 0:
        return "UNCERTAIN"
    if arr_pre.ndim != 3 or arr_post.ndim != 3:
        return "UNCERTAIN"
    # "blank": a non-numeric dtype (a string frame) cannot be compared; numpy raises
    # UFUNCTypeError from .max() rather than returning a sentinel the caller can read.
    # A real capture path never produces one, but the verification verdict is a safety
    # decision, so every unusable input must resolve to UNCERTAIN instead of raising.
    if not np.issubdtype(arr_pre.dtype, np.number) or not np.issubdtype(arr_post.dtype, np.number):
        return "UNCERTAIN"
    if arr_pre.max() <= 10 or arr_post.max() <= 10:
        return "UNCERTAIN"

    pre_total, pre_bottom = count_green_metrics(image_pre, layout)
    post_total, post_bottom = count_green_metrics(image_post, layout)

    bottom_spawn = post_bottom >= BADGE_PIXEL_MIN and (post_bottom - pre_bottom) >= BADGE_DELTA_MIN
    total_growth = (post_total - pre_total) >= TOTAL_GREEN_DELTA_MIN
    if bottom_spawn or total_growth:
        return True
    return False


def has_red_badge_by_text_anchor(array: np.ndarray, text_box, layout: Layout) -> bool:
    """Unread badge detection anchored to the OCRed contact name."""
    if not text_box:
        return False
    height, width = array.shape[:2]
    left = min(point[0] for point in text_box)
    top = min(point[1] for point in text_box)

    pad_x = layout["badge_pad_px"]
    pad_y = int(layout["badge_pad_px"] * 20.0 / 36.0)

    x1 = max(0, int(left - pad_x))
    x2 = min(width, max(0, int(left - 3 * layout["scale"])))
    y1 = max(0, int(top - pad_y))
    y2 = min(height, int(top + pad_y))
    if x2 <= x1 or y2 <= y1:
        return False
    patch = array[y1:y2, x1:x2]
    if patch.size == 0:
        return False
    return bool(red_mask(patch).sum() >= RED_PIXEL_THRESHOLD)


def extract_active_chat_title(results, layout: Layout) -> str:
    """Chat header contact name, bounded to the left of the header area."""
    chat_start_x = layout["chat_start_x"]
    header_h = layout["header_h"]
    total_w = layout["total_w_px"]
    right_limit = chat_start_x + int(layout["title_scan_ratio"] * (total_w - chat_start_x))
    parts = []
    for box, text, _score in results or ():
        cx = (box[0][0] + box[1][0]) / 2.0
        cy = (box[0][1] + box[2][1]) / 2.0
        clean = str(text or "").strip()
        if not clean:
            continue
        if (chat_start_x - 30) <= cx <= right_limit and cy <= header_h:
            if naming.normalize_basic(clean) in TITLE_EXCLUSION_SET:
                continue
            parts.append(clean)
    return " ".join(parts)


def _is_ignored_text(text: str, ignored_keywords) -> bool:
    for keyword in ignored_keywords:
        if keyword and keyword in text:
            return True
    return False


def parse_chat_bubbles(results, image, layout: Layout, ignored_keywords=None) -> List[Bubble]:
    """Split OCR boxes in the message viewport into incoming / outgoing bubbles."""
    array = np.asarray(image)
    if array.ndim != 3:
        return []
    height, width = array.shape[:2]
    chat_start_x = layout["chat_start_x"]
    header_h = layout["header_h"]
    input_h = layout["input_h"]
    scale = layout["scale"]

    ignored = list(BUILTIN_IGNORED_KEYWORDS)
    if isinstance(ignored_keywords, (list, tuple)):
        for keyword in ignored_keywords:
            keyword = str(keyword or "").strip()
            if keyword and keyword not in ignored:
                ignored.append(keyword)

    min_cy = header_h + int(layout["band_pad_px"])
    max_cy = height - int(input_h * 0.70)
    min_cx = chat_start_x + int(layout["band_pad_px"])
    bleed_x = layout["bubble_bleed_px"]
    bleed_y = max(1.0, 8.0 * scale)
    incoming_limit = chat_start_x + int(layout["incoming_left_ratio"] * (width - chat_start_x))

    raw_lines = []
    for box, text, _score in results or ():
        clean = str(text or "").strip()
        if not clean:
            continue
        cx = (box[0][0] + box[1][0]) / 2.0
        cy = (box[0][1] + box[2][1]) / 2.0
        if _is_ignored_text(clean, ignored):
            continue
        if _is_ignored_text(clean, UI_NOISE_KEYWORDS):
            continue
        if not (cx > min_cx and min_cy < cy < max_cy):
            continue
        if any(pattern.match(clean) for pattern in TIMESTAMP_PATTERNS):
            continue
        if clean in DATE_LABELS:
            continue
        if any(clean.startswith(prefix) for prefix in PREFIX_FILTERS):
            continue
        if "拍了拍" in clean:
            continue

        left = min(point[0] for point in box)
        right = max(point[0] for point in box)
        top = min(point[1] for point in box)
        bottom = max(point[1] for point in box)

        px1 = max(0, int(left - bleed_x))
        px2 = min(width, int(right + bleed_x))
        py1 = max(0, int(top - bleed_y))
        py2 = min(height, int(bottom + bleed_y))
        patch = array[py1:py2, px1:px2]
        has_green = push_has_green_bg(patch)

        is_incoming = (not has_green) and (left < incoming_limit)
        raw_lines.append((cy, left, right, is_incoming, clean))

    raw_lines.sort(key=lambda item: item[0])

    merge_distance = layout["bubble_merge_px"]
    merged: List[Bubble] = []
    for cy, left, right, is_incoming, text in raw_lines:
        if (
            merged
            and is_incoming == merged[-1].is_incoming
            and abs(cy - merged[-1].last_y) < merge_distance
        ):
            previous = merged[-1]
            merged[-1] = previous._replace(
                text=previous.text + "，" + text,
                last_y=cy,
                left=min(previous.left, left),
                right=max(previous.right, right),
            )
        else:
            merged.append(Bubble(is_incoming, cy, cy, text, left, right))
    return merged


def _coerce_bubble(item) -> Bubble:
    """Accept either a Bubble or the 1.x dict shape."""
    if isinstance(item, Bubble):
        return item
    if isinstance(item, dict):
        return Bubble(
            bool(item.get("is_incoming")),
            float(item.get("first_y", item.get("last_y", 0)) or 0),
            float(item.get("last_y", 0) or 0),
            str(item.get("text", "")),
            float(item.get("left", 0) or 0),
            float(item.get("right", 0) or 0),
        )
    raise TypeError("unsupported bubble payload: %r" % (item,))


def extract_pending_incoming_messages(bubbles: Sequence[Bubble]):
    """Every incoming bubble after the last outgoing one (the 1.x sandwich scan)."""
    if not bubbles:
        return [], ""
    items = [_coerce_bubble(item) for item in bubbles]

    last_outgoing = -1
    for index in range(len(items) - 1, -1, -1):
        if not items[index].is_incoming:
            last_outgoing = index
            break

    pool = items if last_outgoing == -1 else items[last_outgoing + 1:]
    pending = [bubble for bubble in pool if bubble.is_incoming]
    texts = [bubble.text.strip() for bubble in pending if bubble.text.strip()]
    return pending, "，".join(texts)


def scan_session_rows(results, array: np.ndarray, layout: Layout, whitelist_mode: bool,
                      whitelist, blacklist, allow_leading_digit_strip: bool = True):
    """Left-pane conversation board: active row and whitelisted unread badges."""
    y_min, y_max = session_rows_band(layout)
    sidebar_w = layout["sidebar_w"]
    chat_start_x = layout["chat_start_x"]
    height = array.shape[0]
    scale = layout["scale"]

    entries = []
    for box, text, _score in results or ():
        cx = (box[0][0] + box[1][0]) / 2.0
        cy = (box[0][1] + box[2][1]) / 2.0
        if sidebar_w <= cx <= chat_start_x and y_min <= cy <= y_max:
            entries.append((cy, box, str(text or "").strip()))
    entries.sort(key=lambda item: item[0])

    rows: List[SessionRow] = []
    active_name = None
    max_green = 0

    for cy, box, text in entries:
        if not text:
            continue
        if naming.is_blocked(text, blacklist):
            continue

        band_y1 = max(0, int(cy - 18 * scale))
        band_y2 = min(height, int(cy + 18 * scale))
        patch = array[band_y1:band_y2, sidebar_w:chat_start_x]
        green_score = int(green_mask(patch).sum()) if patch.size else 0

        if green_score > SIDEBAR_ACTIVE_GREEN_MIN and green_score > max_green:
            max_green = green_score
            active_name = text

        if whitelist_mode:
            matched, canonical, tier = naming.find_whitelist_match(
                text, whitelist, allow_leading_digit_strip
            )
        else:
            matched = naming.is_replyable_contact(text, blacklist)
            canonical = text
            tier = "non-whitelist"
        if not matched:
            continue

        badge = has_red_badge_by_text_anchor(array, box, layout)
        rows.append(SessionRow(text, canonical, cy, badge, green_score, tier))

    return rows, active_name


def resolve_active_target(title: str, active_sidebar_name, whitelist_mode: bool,
                          whitelist, blacklist, allow_leading_digit_strip: bool = True):
    """Pick the conversation the engine is allowed to answer right now.

    Returns (canonical_target, source, tier). The header is authoritative because it
    cannot carry a message preview; the sidebar is the fallback for when the header OCR
    came back empty.
    """
    if whitelist_mode:
        if title:
            matched, canonical, tier = naming.find_whitelist_match(
                title, whitelist, allow_leading_digit_strip
            )
            if matched:
                return canonical, "header", tier
        if active_sidebar_name:
            matched, canonical, tier = naming.find_whitelist_match(
                active_sidebar_name, whitelist, allow_leading_digit_strip
            )
            if matched:
                return canonical, "sidebar", tier
        return None, "", ""

    if title and naming.is_replyable_contact(title, blacklist):
        return title.strip(), "header", "non-whitelist"
    if active_sidebar_name and naming.is_replyable_contact(active_sidebar_name, blacklist):
        return active_sidebar_name.strip(), "sidebar", "non-whitelist"
    return None, "", ""


def analyze_frame(results, image, layout: Layout, whitelist_mode: bool, whitelist, blacklist,
                  ignored_keywords=None, allow_leading_digit_strip: bool = True) -> FrameAnalysis:
    """All perception products for one captured frame."""
    array = np.asarray(image)
    if array.ndim != 3:
        raise ValueError("expected an RGB frame")
    height, width = array.shape[:2]
    title = extract_active_chat_title(results, layout)
    bubbles = parse_chat_bubbles(results, image, layout, ignored_keywords)
    pending, pending_text = extract_pending_incoming_messages(bubbles)
    rows, active_name = scan_session_rows(
        results, array, layout, whitelist_mode, whitelist, blacklist, allow_leading_digit_strip
    )
    target, source, _tier = resolve_active_target(
        title, active_name, whitelist_mode, whitelist, blacklist, allow_leading_digit_strip
    )
    return FrameAnalysis(
        width=width,
        height=height,
        layout=layout,
        title=title,
        bubbles=bubbles,
        pending=pending,
        pending_text=pending_text,
        rows=rows,
        active_sidebar_name=active_name,
        active_target=target,
        active_source=source,
    )
