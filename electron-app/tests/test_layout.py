"""Layout engine: fractions, DPI scaling and degenerate window sizes."""

from __future__ import annotations

import pytest

from wechat_engine import layout as layout_mod


def test_reference_size_matches_version_1():
    """The 1100x750 default window must produce the 1.x numbers exactly."""
    result = layout_mod.compute_layout(1100, 750, 1.0)
    assert result["sidebar_w"] == 60
    assert result["chat_start_x"] == 320
    assert result["header_h"] == 85
    assert result["input_h"] == 140


def test_regions_never_overlap_in_thin_windows():
    for width in range(320, 2000, 37):
        result = layout_mod.compute_layout(width, 600, 1.0)
        assert result["chat_start_x"] > result["sidebar_w"]
        assert result["chat_start_x"] <= result["total_w_px"]


def test_minimum_window_is_clamped():
    result = layout_mod.compute_layout(50, 20, 1.0)
    assert result["total_w_px"] == layout_mod.MIN_WINDOW_WIDTH
    assert result["total_h_px"] == layout_mod.MIN_WINDOW_HEIGHT


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0, 2.5])
def test_thresholds_scale_with_dpi(scale):
    result = layout_mod.compute_layout(1100, 750, scale)
    assert result["bubble_merge_px"] == pytest.approx(40.0 * scale)
    assert result["band_pad_px"] == pytest.approx(15.0 * scale)
    assert result["badge_pad_px"] == pytest.approx(36.0 * scale)


def test_invalid_scale_falls_back_to_one():
    for bad in (0, -3, float("nan"), float("inf"), None, "abc"):
        result = layout_mod.compute_layout(1100, 750, bad)
        assert result["scale"] == 1.0


def test_green_bands_scale_with_dpi():
    base_total, base_bottom = layout_mod.green_metric_bands(layout_mod.compute_layout(1100, 750, 1.0))
    dbl_total, dbl_bottom = layout_mod.green_metric_bands(layout_mod.compute_layout(1100, 750, 2.0))
    assert (base_total[1] - base_total[0]) * 2 == pytest.approx(dbl_total[1] - dbl_total[0], abs=3)
    assert (base_bottom[1] - base_bottom[0]) * 2 == pytest.approx(dbl_bottom[1] - dbl_bottom[0], abs=3)


def test_input_box_point_is_inside_the_input_region():
    result = layout_mod.compute_layout(1400, 900, 1.25)
    x, y = layout_mod.input_box_point(result)
    assert 0 < x < result["total_w_px"]
    assert result["header_h"] < y < result["total_h_px"]


def test_chat_crop_box_is_ordered():
    result = layout_mod.compute_layout(1200, 800, 1.0)
    x1, y1, x2, y2 = layout_mod.chat_crop_box(result)
    assert x1 < x2 and y1 < y2
