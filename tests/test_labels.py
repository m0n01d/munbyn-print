"""Tests for munbyn.labels: mm/dot conversion, presets, size parsing, and
feed_scale (this printer's mechanical feed-shortfall correction)."""
from __future__ import annotations

import pytest
from PIL import Image

from munbyn.labels import (
    DPI,
    FEED_SCALE_MAX,
    FEED_SCALE_MIN,
    MAX_WIDTH_MM,
    PRESETS,
    LabelSize,
    apply_feed_scale,
    mm_to_dots,
    parse_size,
    stretched_height_dots,
    validate_feed_scale,
)


def test_dpi_is_203():
    assert DPI == 203


def test_mm_to_dots_basic():
    assert mm_to_dots(0) == 0
    assert mm_to_dots(25.4) == 203  # exactly one inch
    assert mm_to_dots(12.7) == 102  # half inch, rounds up from 101.5


def test_mm_to_dots_rounds():
    # 1mm -> 7.992...dots -> rounds to 8
    assert mm_to_dots(1.0) == 8


def test_label_size_dots_properties():
    size = LabelSize(width_mm=101.6, height_mm=152.4, name="4x6")
    assert size.width_dots == 812
    assert size.height_dots == 1218


def test_label_size_is_frozen():
    size = LabelSize(50.0, 50.0)
    with pytest.raises(Exception):
        size.width_mm = 10.0  # type: ignore[misc]


def test_presets_cover_every_ppd_stock_size():
    expected_names = {
        "1.60x1.20",
        "1.96x1.20",
        "1.96x1.96",
        "2x1",
        "2x2",
        "2.25x1.25",
        "2.25x2.25",
        "2.30x2.30",
        "2.5x1.5",
        "3x2",
        "3x3",
        "3x5",
        "4x6",
    }
    assert set(PRESETS.keys()) == expected_names


def test_preset_4x6_matches_default_page_dots():
    # Spec: default page 4x6 in = 812x1218 dots at 203dpi.
    preset = PRESETS["4x6"]
    assert preset.width_dots == 812
    assert preset.height_dots == 1218
    assert preset.name == "4x6"


def test_preset_2x1_inches():
    preset = PRESETS["2x1"]
    assert preset.width_mm == pytest.approx(50.8)
    assert preset.height_mm == pytest.approx(25.4)


def test_all_presets_within_max_media_width():
    for size in PRESETS.values():
        assert size.width_mm <= MAX_WIDTH_MM


def test_parse_size_preset_name():
    result = parse_size("4x6")
    assert result == PRESETS["4x6"]
    assert result.name == "4x6"


def test_parse_size_preset_name_case_insensitive():
    result = parse_size("4X6")
    assert result == PRESETS["4x6"]


def test_parse_size_preset_decimal_name():
    result = parse_size("2.25x1.25")
    assert result == PRESETS["2.25x1.25"]


def test_parse_size_explicit_inches_suffix():
    result = parse_size("4x6in")
    assert result.width_mm == pytest.approx(101.6)
    assert result.height_mm == pytest.approx(152.4)


def test_parse_size_spaced():
    result = parse_size("4 x 6")
    assert result.width_mm == pytest.approx(101.6)
    assert result.height_mm == pytest.approx(152.4)


def test_parse_size_unitless_is_inches():
    result = parse_size("2x3")
    assert result.width_mm == pytest.approx(2 * 25.4)
    assert result.height_mm == pytest.approx(3 * 25.4)


def test_parse_size_mm_suffix():
    result = parse_size("100x150mm")
    assert result.width_mm == pytest.approx(100.0)
    assert result.height_mm == pytest.approx(150.0)


def test_parse_size_mm_suffix_decimal():
    result = parse_size("101.6x152.4mm")
    assert result.width_mm == pytest.approx(101.6)
    assert result.height_mm == pytest.approx(152.4)


def test_parse_size_junk_raises():
    with pytest.raises(ValueError):
        parse_size("banana")


def test_parse_size_empty_raises():
    with pytest.raises(ValueError):
        parse_size("")
    with pytest.raises(ValueError):
        parse_size("   ")


def test_parse_size_non_string_raises():
    with pytest.raises(ValueError):
        parse_size(None)  # type: ignore[arg-type]


def test_parse_size_width_over_max_raises_inches():
    # 5in = 127mm > 108mm max media width.
    with pytest.raises(ValueError):
        parse_size("5x3in")


def test_parse_size_width_over_max_raises_mm():
    with pytest.raises(ValueError):
        parse_size("200x100mm")


def test_parse_size_zero_or_negative_raises():
    # A blank Custom-size web form field used to fall back to "0", producing
    # a degenerate SIZE 0 mm,0 mm / BITMAP 0,0,1,1 job instead of an error.
    with pytest.raises(ValueError):
        parse_size("0x0mm")
    with pytest.raises(ValueError):
        parse_size("0x4in")
    with pytest.raises(ValueError):
        parse_size("4x0in")
    with pytest.raises(ValueError):
        parse_size("-4x6in")


def test_parse_size_height_over_max_raises():
    # An unbounded height (e.g. a typo'd 100x99999mm) used to render an
    # unbounded bitmap (hundreds of MB) instead of a clean error.
    with pytest.raises(ValueError):
        parse_size("100x99999mm")


def test_parse_size_width_at_boundary_ok():
    # 108mm exactly should be accepted (limit is inclusive).
    result = parse_size("108x50mm")
    assert result.width_mm == pytest.approx(108.0)


# ---------------------------------------------------------------------------
# feed_scale: this printer's mechanical feed-shortfall correction
# ---------------------------------------------------------------------------


def test_feed_scale_bounds_are_0_9_to_1_1():
    assert FEED_SCALE_MIN == 0.9
    assert FEED_SCALE_MAX == 1.1


def test_validate_feed_scale_accepts_1_0_and_in_range_values():
    validate_feed_scale(1.0)
    validate_feed_scale(0.981)
    validate_feed_scale(0.9)
    validate_feed_scale(1.1)


@pytest.mark.parametrize("bad", [0.89, 1.11, 0.5, 2.0, 0.0])
def test_validate_feed_scale_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        validate_feed_scale(bad)


def test_stretched_height_dots_noop_at_1_0():
    assert stretched_height_dots(1218, 1.0) == 1218
    assert stretched_height_dots(0, 1.0) == 0


def test_stretched_height_dots_matches_verified_4x6_job():
    # The verified real-hardware job: 1218 physical rows -> 1242 stretched
    # rows at feed_scale=0.981 (round(1218/0.981) = 1242).
    assert stretched_height_dots(1218, 0.981) == 1242


def test_stretched_height_dots_rounds():
    assert stretched_height_dots(100, 0.981) == round(100 / 0.981)


def test_apply_feed_scale_noop_returns_same_object_at_1_0():
    img = Image.new("L", (10, 20), 255)
    assert apply_feed_scale(img, 1.0) is img


def test_apply_feed_scale_stretches_height_only():
    img = Image.new("L", (100, 1218), 255)
    out = apply_feed_scale(img, 0.981)
    assert out.width == 100
    assert out.height == 1242


def test_apply_feed_scale_rejects_out_of_range():
    img = Image.new("L", (10, 20), 255)
    with pytest.raises(ValueError):
        apply_feed_scale(img, 2.0)
