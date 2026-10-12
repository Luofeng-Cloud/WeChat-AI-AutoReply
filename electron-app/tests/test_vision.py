"""Perception tests against synthetic frames.

Real WeChat screenshots cannot be committed, so the frames here are constructed to
exercise the exact geometric and chromatic conditions the analyser relies on. Each helper
places content relative to compute_layout() output, so the tests fail loudly if the layout
contract changes.
"""

from __future__ import annotations

import numpy as np
import pytest

from wechat_engine import layout as layout_mod
from wechat_engine import vision

WHITE = (255, 255, 255)
GREEN = (149, 236, 105)  # WeChat outgoing bubble
RED = (250, 60, 60)


def make_frame(width=1100, height=750, fill=WHITE):
    return np.full((height, width, 3), fill, dtype=np.uint8)


def draw_bubble(frame, box, colour, pad=6):
    x1 = max(0, int(min(p[0] for p in box) - pad))
    y1 = max(0, int(min(p[1] for p in box) - pad))
    x2 = min(frame.shape[1], int(max(p[0] for p in box) + pad))
    y2 = min(frame.shape[0], int(max(p[1] for p in box) + pad))
    frame[y1:y2, x1:x2] = colour
    return frame


def text_box(x, y, width=80, height=20):
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


def frame_with_bubbles(layout, incoming=(), outgoing=()):
    """Build a frame and matching OCR results.

    incoming: list of (text, y) placed on the left half of the message viewport.
    outgoing: list of (text, y) placed on the right half over a green bubble.
    """
    frame = make_frame(layout["total_w_px"], layout["total_h_px"])
    results = []
    left_x = layout["chat_start_x"] + 40
    right_x = layout["chat_start_x"] + int(0.55 * (layout["total_w_px"] - layout["chat_start_x"]))

    for text, y in incoming:
        box = text_box(left_x, y)
        results.append((box, text, 0.99))
    for text, y in outgoing:
        box = text_box(right_x, y)
        draw_bubble(frame, box, GREEN)
        results.append((box, text, 0.99))
    results.sort(key=lambda item: item[0][0][1])
    return frame, results


class TestBubbleParsing:
    def test_outgoing_bubble_is_classified_by_green_background(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = frame_with_bubbles(layout, outgoing=[("我发的", 400)])
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert len(bubbles) == 1
        assert bubbles[0].is_incoming is False

    def test_incoming_bubble_is_classified_by_position(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = frame_with_bubbles(layout, incoming=[("对方发的", 400)])
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert len(bubbles) == 1
        assert bubbles[0].is_incoming is True

    def test_green_emoji_from_friend_is_not_mistaken_for_my_message(self):
        """A friend sending a green sticker must stay incoming: the decisive test is the
        background colour, and the position fallback only applies when there is none."""
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        box = text_box(layout["chat_start_x"] + 40, 400)
        results = [(box, "哈哈", 0.99)]
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert bubbles[0].is_incoming is True

    @pytest.mark.parametrize(
        "noise",
        [
            "12:30",
            "昨天",
            "星期一",
            "[转账] 请收款",
            "你撤回了一条消息",
            "对方拍了拍我",
            "按住说话",
            "按住鼠标 语音输入文字",
        ],
    )
    def test_system_noise_is_filtered(self, noise):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        results = [
            (text_box(layout["chat_start_x"] + 40, 400), noise, 0.99),
            (text_box(layout["chat_start_x"] + 40, 460), "真实消息", 0.99),
        ]
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert [b.text for b in bubbles] == ["真实消息"]

    def test_custom_ignored_keywords_are_honoured(self):
        """The configured keyword list is additive to the built-in one, never a replacement."""
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        results = [(text_box(layout["chat_start_x"] + 40, 400), "关键词测试句子", 0.99)]
        # Not ignored by default.
        assert len(vision.parse_chat_bubbles(results, frame, layout)) == 1
        # Ignored once the user adds the keyword.
        assert vision.parse_chat_bubbles(results, frame, layout, ["关键词测试"]) == []

    def test_builtin_keywords_are_always_applied(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        results = [(text_box(layout["chat_start_x"] + 40, 400), "按住说话", 0.99)]
        # A user-supplied list must not disable the built-in placeholder filters.
        assert vision.parse_chat_bubbles(results, frame, layout, ["别的词"]) == []

    def test_header_and_toolbar_regions_are_excluded(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        results = [
            (text_box(layout["chat_start_x"] + 40, 5), "顶栏文字", 0.99),
            (text_box(layout["chat_start_x"] + 40, layout["total_h_px"] - 20), "输入框文字", 0.99),
        ]
        assert vision.parse_chat_bubbles(results, frame, layout) == []

    def test_sidebar_text_is_not_a_bubble(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        results = [(text_box(70, 400), "会话列表项", 0.99)]
        assert vision.parse_chat_bubbles(results, frame, layout) == []


class TestBubbleMerging:
    def test_consecutive_short_messages_merge(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = frame_with_bubbles(layout, incoming=[("在吗", 400), ("帮个忙", 425)])
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert len(bubbles) == 1
        assert bubbles[0].text == "在吗，帮个忙"

    def test_distant_messages_do_not_merge(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = frame_with_bubbles(layout, incoming=[("第一条", 300), ("第二条", 500)])
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert len(bubbles) == 2

    def test_merge_distance_scales_with_dpi(self):
        """The 1.x 40 px constant collapsed on high-DPI displays and stopped merging."""
        layout = layout_mod.compute_layout(2200, 1500, 2.0)
        frame, results = frame_with_bubbles(layout, incoming=[("在吗", 800), ("帮个忙", 850)])
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert len(bubbles) == 1

    def test_direction_change_breaks_the_merge(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = frame_with_bubbles(layout, incoming=[("你说的", 400)], outgoing=[("我说的", 420)])
        bubbles = vision.parse_chat_bubbles(results, frame, layout)
        assert len(bubbles) == 2
        assert [b.is_incoming for b in bubbles] == [True, False]


class TestPendingExtraction:
    def test_messages_after_my_last_reply_are_pending(self):
        bubbles = [
            vision.Bubble(True, 100, 100, "你好", 0, 0),
            vision.Bubble(False, 200, 200, "你也好", 0, 0),
            vision.Bubble(True, 300, 300, "在忙吗", 0, 0),
            vision.Bubble(True, 320, 320, "回我一下", 0, 0),
        ]
        pending, text = vision.extract_pending_incoming_messages(bubbles)
        assert len(pending) == 2
        assert text == "在忙吗，回我一下"

    def test_no_pending_when_i_replied_last(self):
        bubbles = [
            vision.Bubble(True, 100, 100, "你好", 0, 0),
            vision.Bubble(False, 200, 200, "你也好", 0, 0),
        ]
        pending, text = vision.extract_pending_incoming_messages(bubbles)
        assert pending == [] and text == ""

    def test_all_incoming_when_i_never_replied(self):
        bubbles = [
            vision.Bubble(True, 100, 100, "第一条", 0, 0),
            vision.Bubble(True, 200, 200, "第二条", 0, 0),
        ]
        _pending, text = vision.extract_pending_incoming_messages(bubbles)
        assert text == "第一条，第二条"

    def test_legacy_dict_shape_is_accepted(self):
        legacy = [{"is_incoming": True, "first_y": 1, "last_y": 1, "text": "老格式"}]
        pending, text = vision.extract_pending_incoming_messages(legacy)
        assert text == "老格式"


class TestSendVerification:
    def _green_frame(self, layout, grow=False):
        frame = make_frame(layout["total_w_px"], layout["total_h_px"])
        _, bottom_band = layout_mod.green_metric_bands(layout)
        y1, y2, x1, x2 = bottom_band
        height = (y2 - y1) if grow else max(1, (y2 - y1) // 4)
        frame[y2 - height:y2, x1 + 10:x2 - 10] = GREEN
        return frame

    def test_growth_is_detected_as_success(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        pre = self._green_frame(layout, grow=False)
        post = self._green_frame(layout, grow=True)
        assert vision.verify_diff_bubble_success(pre, post, layout) is True

    def test_identical_frames_report_failure(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = self._green_frame(layout, grow=False)
        assert vision.verify_diff_bubble_success(frame, frame.copy(), layout) is False

    @pytest.mark.parametrize("blank", [None, "blank", np.zeros((10, 10, 3), dtype=np.uint8)])
    def test_unreadable_frames_are_uncertain(self, blank):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        assert vision.verify_diff_bubble_success(blank, self._green_frame(layout), layout) == "UNCERTAIN"

    def test_black_frames_are_uncertain_not_failure(self):
        """The critical distinction: a black frame must not trigger a resend."""
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        black = make_frame(1100, 750, (0, 0, 0))
        assert vision.verify_diff_bubble_success(black, black, layout) == "UNCERTAIN"

    def test_green_count_metrics_are_zero_on_white(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        assert vision.count_green_metrics(make_frame(1100, 750), layout) == (0, 0)


class TestRedBadge:
    def test_badge_left_of_the_name_is_found(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        box = text_box(200, 400)
        frame[390:410, 175:196] = RED
        assert vision.has_red_badge_by_text_anchor(frame, box, layout)

    def test_no_badge_when_the_area_is_clean(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        assert not vision.has_red_badge_by_text_anchor(frame, text_box(200, 400), layout)

    def test_empty_box_returns_false_instead_of_raising(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        assert not vision.has_red_badge_by_text_anchor(make_frame(1100, 750), [], layout)

    def test_row_at_the_top_edge_does_not_crash(self):
        """The 1.x slice could go negative and silently produce an empty patch."""
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        assert not vision.has_red_badge_by_text_anchor(frame, text_box(5, 0), layout)

    def test_sidebar_sentinel_needs_enough_red_pixels(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame = make_frame(1100, 750)
        assert not vision.sidebar_has_red(frame[:, : layout["chat_start_x"]])
        frame[100:120, 20:40] = RED
        assert vision.sidebar_has_red(frame[:, : layout["chat_start_x"]])


class TestTitleExtraction:
    def test_title_is_read_from_the_header(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        results = [(text_box(layout["chat_start_x"] + 60, 30), "张三", 0.99)]
        assert vision.extract_active_chat_title(results, layout) == "张三"

    def test_window_controls_are_excluded(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        results = [
            (text_box(layout["chat_start_x"] + 60, 30), "张三", 0.99),
            (text_box(layout["chat_start_x"] + 300, 30), "最小化", 0.99),
        ]
        assert vision.extract_active_chat_title(results, layout) == "张三"

    def test_message_bubbles_are_not_part_of_the_title(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        results = [(text_box(layout["chat_start_x"] + 60, 400), "这是消息", 0.99)]
        assert vision.extract_active_chat_title(results, layout) == ""


class TestSessionScan:
    def _sidebar_frame(self, layout, rows):
        frame = make_frame(layout["total_w_px"], layout["total_h_px"])
        results = []
        for name, y, badge in rows:
            box = text_box(layout["sidebar_w"] + 20, y, width=90)
            if badge:
                frame[y - 8:y + 8, layout["sidebar_w"] + 4:layout["sidebar_w"] + 18] = RED
            results.append((box, name, 0.99))
        return frame, results

    def test_whitelisted_unread_row_is_reported(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = self._sidebar_frame(layout, [("张三", 400, True), ("李四", 500, False)])
        rows, _active = vision.scan_session_rows(results, frame, layout, True, ["张三", "李四"], [])
        by_name = {row.name: row for row in rows}
        assert by_name["张三"].has_red_badge is True
        assert by_name["李四"].has_red_badge is False

    def test_non_whitelisted_rows_are_dropped_in_whitelist_mode(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = self._sidebar_frame(layout, [("陌生人", 400, True)])
        rows, _active = vision.scan_session_rows(results, frame, layout, True, ["张三"], [])
        assert rows == []

    def test_group_rows_are_dropped_in_open_mode(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = self._sidebar_frame(layout, [("同学交流群", 400, True), ("张三", 500, True)])
        rows, _active = vision.scan_session_rows(results, frame, layout, False, [], [])
        assert [row.name for row in rows] == ["张三"]

    def test_active_conversation_is_detected_from_the_green_pill(self):
        layout = layout_mod.compute_layout(1100, 750, 1.0)
        frame, results = self._sidebar_frame(layout, [("张三", 400, False)])
        frame[382:418, layout["sidebar_w"]:layout["chat_start_x"]] = (200, 240, 200)
        _rows, active = vision.scan_session_rows(results, frame, layout, True, ["张三"], [])
        assert active == "张三"


class TestTargetResolution:
    def test_header_wins_over_sidebar(self):
        target, source, _tier = vision.resolve_active_target("张三", "李四", True, ["张三", "李四"], [])
        assert (target, source) == ("张三", "header")

    def test_sidebar_is_the_fallback(self):
        target, source, _tier = vision.resolve_active_target("", "李四", True, ["李四"], [])
        assert (target, source) == ("李四", "sidebar")

    def test_unknown_contact_yields_nothing_in_whitelist_mode(self):
        target, _source, _tier = vision.resolve_active_target("陌生人", "路人", True, ["张三"], [])
        assert target is None

    def test_open_mode_accepts_a_plain_friend(self):
        target, _source, _tier = vision.resolve_active_target("李四", None, False, [], [])
        assert target == "李四"

    def test_open_mode_still_rejects_groups(self):
        target, _source, _tier = vision.resolve_active_target("同学交流群", None, False, [], [])
        assert target is None
