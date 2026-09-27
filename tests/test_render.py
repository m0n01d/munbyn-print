"""Tests for munbyn/render.py."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageOps

from conftest import BARCODE_BAR_WIDTHS, build_pdf, dark_bbox, pdf_rect_fill
from munbyn.labels import PRESETS, LabelSize
from munbyn.render import RenderError, RenderOptions, parse_pages, render_file, render_image

LABEL_4X6 = PRESETS["4x6"]

PREVIEW_DIR = Path("/tmp/munbyn-render-previews")


def save_preview(name, img):
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    img.save(PREVIEW_DIR / name)


# ---------------------------------------------------------------------------
# parse_pages
# ---------------------------------------------------------------------------


def test_parse_pages_none_means_all():
    assert parse_pages(None, 4) == [0, 1, 2, 3]


def test_parse_pages_single_and_range():
    assert parse_pages("1-3,5", 5) == [0, 1, 2, 4]


def test_parse_pages_dedupes_preserving_order():
    assert parse_pages("2,1-2", 3) == [1, 0]


@pytest.mark.parametrize("spec", ["0", "-1", "3-1", "abc", "", "1-", "1,,2"])
def test_parse_pages_junk_raises(spec):
    with pytest.raises(ValueError):
        parse_pages(spec, 5)


def test_parse_pages_out_of_range_raises():
    with pytest.raises(ValueError):
        parse_pages("9", 3)


# ---------------------------------------------------------------------------
# Output size / mode contract, for every fit mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fit", ["fit", "fill", "stretch", "actual"])
def test_output_is_exact_label_size_every_mode(landscape_solid_png, fit):
    size = LabelSize(width_mm=40.0, height_mm=60.0, name="test")
    out = render_file(landscape_solid_png, size, RenderOptions(fit=fit))
    assert len(out) == 1
    assert out[0].size == (size.width_dots, size.height_dots)
    assert out[0].mode == "1"


def test_output_is_exact_label_size_with_scale(full_bleed_square_png):
    size = PRESETS["3x2"]
    out = render_file(full_bleed_square_png, size, RenderOptions(scale=50))
    assert out[0].size == (size.width_dots, size.height_dots)
    assert out[0].mode == "1"


def test_pdf_output_is_exact_label_size(letter_shipping_label_pdf):
    out = render_file(letter_shipping_label_pdf, LABEL_4X6, RenderOptions())
    assert out[0].size == (LABEL_4X6.width_dots, LABEL_4X6.height_dots)
    assert out[0].mode == "1"


# ---------------------------------------------------------------------------
# Auto-crop: the Letter-corner shipping label must fill >90% of the label
# ---------------------------------------------------------------------------


def test_auto_crop_fills_label(letter_shipping_label_pdf):
    out = render_file(letter_shipping_label_pdf, LABEL_4X6, RenderOptions(crop="auto"))[0]
    save_preview("auto_crop_letter_corner.png", out)
    bbox = dark_bbox(out)
    assert bbox is not None
    left, upper, right, lower = bbox
    width_frac = (right - left) / LABEL_4X6.width_dots
    height_frac = (lower - upper) / LABEL_4X6.height_dots
    assert width_frac > 0.90, width_frac
    assert height_frac > 0.90, height_frac


def test_crop_none_leaves_content_small(letter_shipping_label_pdf):
    # Without auto-crop, the tiny label-in-the-corner of a Letter page should
    # NOT be blown up to fill the 4x6 target -- it stays a small fraction.
    out = render_file(letter_shipping_label_pdf, LABEL_4X6, RenderOptions(crop="none"))[0]
    bbox = dark_bbox(out)
    assert bbox is not None
    left, upper, right, lower = bbox
    width_frac = (right - left) / LABEL_4X6.width_dots
    assert width_frac < 0.6, width_frac


# ---------------------------------------------------------------------------
# Auto-rotate
# ---------------------------------------------------------------------------


def test_auto_rotate_for_landscape_content(landscape_solid_png):
    tall_narrow = LabelSize(width_mm=30.0, height_mm=70.0, name="tall-narrow")
    auto = render_file(landscape_solid_png, tall_narrow, RenderOptions(rotate="auto"))[0]
    fixed = render_file(landscape_solid_png, tall_narrow, RenderOptions(rotate="0"))[0]
    save_preview("auto_rotate_on.png", auto)
    save_preview("auto_rotate_off.png", fixed)

    auto_bbox = dark_bbox(auto)
    fixed_bbox = dark_bbox(fixed)
    assert auto_bbox is not None and fixed_bbox is not None

    auto_height_frac = (auto_bbox[3] - auto_bbox[1]) / tall_narrow.height_dots
    fixed_height_frac = (fixed_bbox[3] - fixed_bbox[1]) / tall_narrow.height_dots

    # Rotated to match the tall label, the landscape frame should use far
    # more of the available height than it does unrotated.
    assert auto_height_frac > fixed_height_frac + 0.2


# ---------------------------------------------------------------------------
# fit / fill / stretch / actual / scale semantics
# ---------------------------------------------------------------------------


def test_fill_covers_the_entire_canvas(full_bleed_square_png):
    size = LabelSize(width_mm=40.0, height_mm=70.0, name="test")  # not square
    out = render_image(Image.open(__import__("io").BytesIO(full_bleed_square_png)), size, RenderOptions(fit="fill"))
    bbox = dark_bbox(out)
    assert bbox == (0, 0, size.width_dots, size.height_dots)


def test_fit_leaves_margin_on_non_matching_aspect(circle_png):
    size = LabelSize(width_mm=40.0, height_mm=70.0, name="test")  # not square
    import io

    out = render_file(circle_png, size, RenderOptions(fit="fit"))[0]
    bbox = dark_bbox(out)
    assert bbox is not None
    # The circle is square; fit into a taller-than-wide label is
    # width-constrained, so it fills the width but leaves white bands
    # top/bottom -- unlike "fill" or "stretch".
    height_frac = (bbox[3] - bbox[1]) / size.height_dots
    assert height_frac < 0.9


def test_stretch_distorts_aspect(circle_png):
    size = LabelSize(width_mm=40.0, height_mm=70.0, name="test")  # not square
    out = render_file(circle_png, size, RenderOptions(fit="stretch"))[0]
    save_preview("stretch_circle.png", out)
    bbox = dark_bbox(out)
    assert bbox is not None
    width_frac = (bbox[2] - bbox[0]) / size.width_dots
    height_frac = (bbox[3] - bbox[1]) / size.height_dots
    # The circle touched all 4 edges of its source square, so stretching to
    # fill a non-square canvas (ignoring aspect) makes it touch all 4 edges
    # of the label too -- unlike "fit", which would not.
    assert width_frac > 0.95
    assert height_frac > 0.95


def test_actual_uses_pixel_for_dot_with_no_dpi_metadata():
    import io

    raw_w, raw_h = 100, 50
    img = Image.new("RGB", (raw_w, raw_h), (0, 0, 0))
    size = LabelSize(width_mm=100.0, height_mm=100.0, name="big")  # plenty of room
    out = render_image(img, size, RenderOptions(fit="actual", align="top-left", margin_mm=0.0))
    bbox = dark_bbox(out)
    assert bbox == (0, 0, raw_w, raw_h)


def test_actual_honours_dpi_metadata():
    raw_w, raw_h = 100, 60
    img = Image.new("RGB", (raw_w, raw_h), (0, 0, 0))
    img.info["dpi"] = (406, 406)  # 2x native label DPI -> should render at half size
    size = LabelSize(width_mm=100.0, height_mm=100.0, name="big")
    out = render_image(img, size, RenderOptions(fit="actual", align="top-left", margin_mm=0.0))
    bbox = dark_bbox(out)
    assert bbox is not None
    assert bbox[2] == pytest.approx(raw_w / 2, abs=2)
    assert bbox[3] == pytest.approx(raw_h / 2, abs=2)


def test_scale_option_resizes_by_percent():
    raw_w, raw_h = 100, 40
    img = Image.new("RGB", (raw_w, raw_h), (0, 0, 0))
    size = LabelSize(width_mm=100.0, height_mm=100.0, name="big")
    out = render_image(img, size, RenderOptions(scale=50, align="top-left", margin_mm=0.0))
    bbox = dark_bbox(out)
    assert bbox is not None
    assert bbox[2] == pytest.approx(raw_w * 0.5, abs=2)
    assert bbox[3] == pytest.approx(raw_h * 0.5, abs=2)


# ---------------------------------------------------------------------------
# Barcode bars stay crisp
# ---------------------------------------------------------------------------


def test_barcode_bars_stay_crisp(barcode_only_pdf):
    size = LabelSize(width_mm=70.0, height_mm=21.0, name="barcode-test")
    out = render_file(barcode_only_pdf, size, RenderOptions(crop="auto", fit="fit", rotate="0"))[0]
    save_preview("barcode_crisp.png", out)

    bbox = dark_bbox(out)
    assert bbox is not None
    left, upper, right, lower = bbox
    mid_row = (upper + lower) // 2

    pixels = out.convert("L").load()
    runs = 0
    was_black = False
    for x in range(left, right):
        is_black = pixels[x, mid_row] < 128
        if is_black and not was_black:
            runs += 1
        was_black = is_black

    assert runs == len(BARCODE_BAR_WIDTHS), runs


# ---------------------------------------------------------------------------
# pages spec
# ---------------------------------------------------------------------------


def test_pages_spec_selects_subset(two_page_pdf):
    size = LabelSize(width_mm=30.0, height_mm=30.0, name="square")
    both = render_file(two_page_pdf, size, RenderOptions())
    assert len(both) == 2

    only_second = render_file(two_page_pdf, size, RenderOptions(pages="2"))
    assert len(only_second) == 1


def test_pages_spec_out_of_range_raises(two_page_pdf):
    size = LabelSize(width_mm=30.0, height_mm=30.0, name="square")
    with pytest.raises(RenderError):
        render_file(two_page_pdf, size, RenderOptions(pages="5"))


# ---------------------------------------------------------------------------
# Blank page must not crash
# ---------------------------------------------------------------------------


def test_blank_page_does_not_crash(blank_page_pdf):
    out = render_file(blank_page_pdf, LABEL_4X6, RenderOptions())
    assert len(out) == 1
    assert out[0].size == (LABEL_4X6.width_dots, LABEL_4X6.height_dots)
    bbox = dark_bbox(out[0])
    assert bbox is None  # entirely white, no crash, no phantom content


# ---------------------------------------------------------------------------
# Alpha becomes white
# ---------------------------------------------------------------------------


def test_alpha_becomes_white(landscape_alpha_png):
    size = LabelSize(width_mm=60.0, height_mm=30.0, name="wide")
    out = render_file(landscape_alpha_png, size, RenderOptions(fit="fit"))[0]
    save_preview("alpha_becomes_white.png", out)
    # Corner pixels came from fully-transparent source -> must render white,
    # never black.
    assert out.convert("L").getpixel((1, 1)) == 255
    assert out.convert("L").getpixel((size.width_dots - 2, size.height_dots - 2)) == 255
    # But there must still be dark content somewhere (the opaque block).
    assert dark_bbox(out) is not None


# ---------------------------------------------------------------------------
# Unknown bytes -> RenderError
# ---------------------------------------------------------------------------


def test_unknown_bytes_raise_render_error():
    with pytest.raises(RenderError):
        render_file(b"this is not any known file format" * 4, LABEL_4X6, RenderOptions())


def test_empty_bytes_raise_render_error():
    with pytest.raises(RenderError):
        render_file(b"", LABEL_4X6, RenderOptions())


# ---------------------------------------------------------------------------
# 2-page PDF, EXIF orientation, and 16-bit/palette PNG edge cases
# ---------------------------------------------------------------------------


def test_two_page_pdf_all_pages(two_page_pdf):
    out = render_file(two_page_pdf, LABEL_4X6, RenderOptions())
    assert len(out) == 2
    for img in out:
        assert img.size == (LABEL_4X6.width_dots, LABEL_4X6.height_dots)
        assert dark_bbox(img) is not None


def test_exif_orientation_is_applied(exif_orientation_jpeg):
    import io

    reference = ImageOps.exif_transpose(Image.open(io.BytesIO(exif_orientation_jpeg)))
    ref_w, ref_h = reference.size
    ref_mask = ImageOps.invert(reference.convert("L")).point(lambda p: 255 if p > 10 else 0)
    ref_bbox = ref_mask.getbbox()
    assert ref_bbox is not None

    size = LabelSize(width_mm=100.0, height_mm=100.0, name="big")  # plenty of room, no scaling
    out = render_file(
        exif_orientation_jpeg,
        size,
        RenderOptions(fit="actual", rotate="0", crop="none", align="top-left", margin_mm=0.0),
    )[0]
    save_preview("exif_orientation.png", out)

    cropped = out.crop((0, 0, ref_w, ref_h))
    out_bbox = dark_bbox(cropped)
    assert out_bbox is not None
    for a, b in zip(out_bbox, ref_bbox):
        assert abs(a - b) <= 6, (out_bbox, ref_bbox)


def test_sixteen_bit_png_does_not_crash(sixteen_bit_png):
    size = LabelSize(width_mm=30.0, height_mm=30.0, name="square")
    out = render_file(sixteen_bit_png, size, RenderOptions())
    assert len(out) == 1
    assert out[0].size == (size.width_dots, size.height_dots)
    assert dark_bbox(out[0]) is not None  # right half was black


def test_palette_png_with_transparency_does_not_crash(palette_png_with_transparency):
    size = LabelSize(width_mm=30.0, height_mm=25.0, name="rect")
    out = render_file(palette_png_with_transparency, size, RenderOptions())
    assert len(out) == 1
    assert out[0].size == (size.width_dots, size.height_dots)
    assert dark_bbox(out[0]) is not None


# ---------------------------------------------------------------------------
# Source can be a path, not just bytes
# ---------------------------------------------------------------------------


def test_render_file_accepts_a_path(tmp_path, two_page_pdf):
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(two_page_pdf)
    out = render_file(pdf_path, LABEL_4X6, RenderOptions(pages="1"))
    assert len(out) == 1


# ---------------------------------------------------------------------------
# PDF physical size, margins, /Rotate, image auto-crop, option validation
# ---------------------------------------------------------------------------


def _one_inch_by_half_pdf():
    # A 72 x 36 pt (1in x 0.5in) black box on a Letter page.
    return build_pdf([(612.0, 792.0, pdf_rect_fill(100, 500, 72, 36))])


@pytest.mark.parametrize("opts", [RenderOptions(scale=100, rotate="0"), RenderOptions(fit="actual", rotate="0")])
def test_pdf_scale_100_and_actual_are_physical_size(opts):
    out = render_file(_one_inch_by_half_pdf(), LABEL_4X6, opts)[0]
    bbox = dark_bbox(out)
    assert bbox is not None
    assert bbox[2] - bbox[0] == pytest.approx(203, abs=2)
    assert bbox[3] - bbox[1] == pytest.approx(101.5, abs=2)


def test_pdf_scale_50_is_half_physical_size():
    out = render_file(_one_inch_by_half_pdf(), LABEL_4X6, RenderOptions(scale=50, rotate="0"))[0]
    bbox = dark_bbox(out)
    assert bbox[2] - bbox[0] == pytest.approx(101.5, abs=2)


def test_pdf_big_scale_is_center_cropped_not_huge():
    out = render_file(_one_inch_by_half_pdf(), LABEL_4X6, RenderOptions(scale=2000, rotate="0"))[0]
    assert out.size == (LABEL_4X6.width_dots, LABEL_4X6.height_dots)
    assert dark_bbox(out) == (0, 0, LABEL_4X6.width_dots, LABEL_4X6.height_dots)


def test_margin_shrinks_content_instead_of_cropping(full_bleed_square_png):
    margin = 5.0
    out = render_file(full_bleed_square_png, LABEL_4X6, RenderOptions(fit="fit", margin_mm=margin))[0]
    bbox = dark_bbox(out)
    m = round(margin / 25.4 * 203)
    assert bbox[0] == pytest.approx(m, abs=1)
    assert bbox[2] == pytest.approx(LABEL_4X6.width_dots - m, abs=1)


def test_pdf_page_rotate_is_honoured():
    # Landscape MediaBox with /Rotate 90 displays portrait; the box on the
    # page's left edge ends up along the top when displayed.
    pdf = build_pdf([(200.0, 100.0, pdf_rect_fill(0, 0, 40, 100), 90)])
    size = LabelSize(width_mm=50.0, height_mm=100.0, name="tall")
    out = render_file(pdf, size, RenderOptions(crop="none", rotate="0", fit="fit"))[0]
    bbox = dark_bbox(out)
    assert bbox is not None
    assert bbox[1] <= 2 and bbox[3] < size.height_dots * 0.3
    assert bbox[2] - bbox[0] > size.width_dots * 0.95


def test_image_auto_crop_trims_white_page_around_label():
    # A "screenshot" of a Letter page with a 4x6-proportioned label in a corner.
    page = Image.new("RGB", (850, 1100), "white")
    from PIL import ImageDraw

    draw = ImageDraw.Draw(page)
    draw.rectangle([20, 20, 20 + 400, 20 + 600], outline="black", width=4)
    import io

    buf = io.BytesIO()
    page.save(buf, format="PNG")
    out = render_file(buf.getvalue(), LABEL_4X6, RenderOptions())[0]
    bbox = dark_bbox(out)
    assert (bbox[3] - bbox[1]) / LABEL_4X6.height_dots > 0.95
    out_none = render_file(buf.getvalue(), LABEL_4X6, RenderOptions(crop="none"))[0]
    box_none = dark_bbox(out_none)
    assert (box_none[3] - box_none[1]) / LABEL_4X6.height_dots < 0.6


@pytest.mark.parametrize(
    "opts",
    [
        RenderOptions(fit="zoom"),
        RenderOptions(crop="maybe"),
        RenderOptions(align="bottom"),
        RenderOptions(dither="atkinson"),
        RenderOptions(rotate="45"),
        RenderOptions(scale=0),
        RenderOptions(threshold=300),
    ],
)
def test_bad_options_raise_render_error(full_bleed_square_png, opts):
    with pytest.raises(RenderError):
        render_file(full_bleed_square_png, LABEL_4X6, opts)
