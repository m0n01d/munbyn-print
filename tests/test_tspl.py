"""Tests for munbyn.tspl: header bytes, bitmap packing, describe/hexdump,
status decoding, and the self-test/calibrate/feed jobs."""
from __future__ import annotations

import pytest
from PIL import Image

from munbyn.labels import LabelSize, PRESETS, mm_to_dots, parse_size
from munbyn.tspl import (
    CALIBRATION_INSTRUCTIONS,
    STATUS_QUERY,
    SUPPORTED_COMMANDS,
    JobSettings,
    _split_commands,
    bitmap_command,
    build_job,
    calibrate_instructions,
    decode_status,
    describe,
    feed_job,
    header,
    hexdump,
    pack_bitmap,
    selftest_image,
    selftest_job,
    simulate,
)


# --------------------------------------------------------------------------
# JobSettings defaults
# --------------------------------------------------------------------------


def test_job_settings_defaults():
    s = JobSettings(size=PRESETS["4x6"])
    assert s.media == "gap"
    assert s.gap_mm == 3.0
    assert s.gap_offset_mm == 0.0
    assert s.speed == 4
    assert s.direction == 0
    assert s.offset_mm == 0.0
    assert s.x_shift_mm == 0.0
    assert s.y_shift_mm == 0.0
    assert s.copies == 1
    # TSC/EPL convention: a clear bit prints a dot (see module docstring).
    assert s.bitmap_black_is_one is False


# --------------------------------------------------------------------------
# header()
# --------------------------------------------------------------------------


def test_header_gap_4x6_exact_bytes():
    s = JobSettings(size=PRESETS["4x6"])
    assert header(s) == (
        b"SIZE 102 mm,152 mm\r\n"
        b"GAP 3 mm,0 mm\r\n"
        b"REFERENCE 0,0\r\n"
        b"OFFSET 0 mm\r\n"
        b"SETC AUTODOTTED OFF\r\n"
        b"DENSITY 12\r\n"
        b"SPEED 4\r\n"
        b"DIRECTION 0,0\r\n"
    )


def test_header_bline_exact_bytes():
    size = LabelSize(50.8, 25.4, "test")
    s = JobSettings(size=size, media="bline", gap_mm=2.0, gap_offset_mm=1.0)
    assert header(s) == (
        b"SIZE 51 mm,25 mm\r\n"
        b"BLINE 2 mm,1 mm\r\n"
        b"REFERENCE 0,0\r\n"
        b"OFFSET 0 mm\r\n"
        b"SETC AUTODOTTED OFF\r\n"
        b"DENSITY 12\r\n"
        b"SPEED 4\r\n"
        b"DIRECTION 0,0\r\n"
    )


def test_header_continuous_exact_bytes():
    size = LabelSize(50.8, 25.4, "test")
    s = JobSettings(size=size, media="continuous")
    assert header(s) == (
        b"SIZE 51 mm,25 mm\r\n"
        b"GAP 0,0\r\n"
        b"REFERENCE 0,0\r\n"
        b"OFFSET 0 mm\r\n"
        b"SETC AUTODOTTED OFF\r\n"
        b"DENSITY 12\r\n"
        b"SPEED 4\r\n"
        b"DIRECTION 0,0\r\n"
    )


def test_header_lines_end_with_crlf():
    s = JobSettings(size=PRESETS["4x6"])
    data = header(s)
    assert data.endswith(b"\r\n")
    assert b"\n\n" not in data  # no bare LF-only or doubled terminators
    for line in data.split(b"\r\n")[:-1]:
        assert b"\n" not in line and b"\r" not in line


def test_header_direction_and_density_speed_reflected():
    s = JobSettings(size=PRESETS["2x1"], density=5, speed=8, direction=1)
    data = header(s)
    assert b"DENSITY 5\r\n" in data
    assert b"SPEED 8\r\n" in data
    assert b"DIRECTION 1,0\r\n" in data


@pytest.mark.parametrize(
    "kwargs",
    [
        {"density": 99},
        {"density": -1},
        {"speed": 0},
        {"speed": 9},
        {"direction": 2},
        {"gap_mm": -1.0},
        {"gap_offset_mm": -1.0},
        {"offset_mm": -1.0},
    ],
)
def test_header_rejects_out_of_range_settings(kwargs):
    # Out-of-range values used to go straight into the job; on a firmware
    # that silently drops jobs it doesn't like, that's a silent no-print.
    s = JobSettings(size=PRESETS["2x1"], **kwargs)
    with pytest.raises(ValueError):
        header(s)


# --------------------------------------------------------------------------
# pack_bitmap: bit order, polarity, row padding
# --------------------------------------------------------------------------


def _make_row(pattern, height=1):
    """pattern: list of 0 (black) / 255 (white) pixel values, one row."""
    width = len(pattern)
    img = Image.new("1", (width, height))
    for y in range(height):
        for x, v in enumerate(pattern):
            img.putpixel((x, y), v)
    return img


def test_pack_bitmap_byte_multiple_width_black_is_one():
    img = _make_row([0, 255, 0, 255, 0, 255, 0, 255])  # black at even x
    wb, h, data = pack_bitmap(img, black_is_one=True)
    assert (wb, h) == (1, 1)
    assert data == b"\xaa"  # bits set at x=0,2,4,6 -> MSB-first 10101010


def test_pack_bitmap_byte_multiple_width_black_is_zero():
    img = _make_row([0, 255, 0, 255, 0, 255, 0, 255])
    wb, h, data = pack_bitmap(img, black_is_one=False)
    assert (wb, h) == (1, 1)
    assert data == b"\x55"  # inverted: bits set at white x=1,3,5,7


def test_pack_bitmap_msb_is_leftmost_pixel():
    # Only the leftmost pixel is black; with black_is_one it must land in
    # the MSB of the first byte.
    img = _make_row([0] + [255] * 7)
    _, _, data = pack_bitmap(img, black_is_one=True)
    assert data == b"\x80"


def test_pack_bitmap_row_padding_is_white_black_is_one():
    # width=10 not a multiple of 8 -> 2 bytes/row, 6 padding bits.
    img = Image.new("1", (10, 1), 0)  # all-black real pixels
    wb, h, data = pack_bitmap(img, black_is_one=True)
    assert wb == 2
    # real pixels (all black) -> bit=1 for x=0..9; padding (white) -> bit=0
    assert data == b"\xff\xc0"


def test_pack_bitmap_row_padding_is_white_black_is_zero():
    img = Image.new("1", (10, 1), 0)  # all-black real pixels
    wb, h, data = pack_bitmap(img, black_is_one=False)
    assert wb == 2
    # real pixels (all black) -> bit=0; padding (white) -> bit=1 (forced)
    assert data == b"\x00\x3f"


def test_pack_bitmap_multi_row_height():
    img = Image.new("1", (8, 3), 255)
    img.putpixel((0, 1), 0)  # one black pixel on the middle row
    wb, h, data = pack_bitmap(img, black_is_one=True)
    assert (wb, h) == (1, 3)
    assert data == b"\x00\x80\x00"


def test_pack_bitmap_converts_non_mode_1_with_threshold_128():
    img = Image.new("L", (2, 1))
    img.putpixel((0, 0), 200)  # >=128 -> white
    img.putpixel((1, 0), 50)  # <128 -> black
    _, _, data = pack_bitmap(img, black_is_one=True)
    assert data == b"\x40"  # only pixel 1 (black) set, at bit position 1


def test_bitmap_command_format():
    img = Image.new("1", (8, 1), 255)
    cmd = bitmap_command(10, 20, img, black_is_one=True)
    assert cmd.startswith(b"BITMAP 10,20,1,1,1,")
    assert cmd.endswith(b"\r\n")
    # header + 1 payload byte + CRLF
    assert len(cmd) == len(b"BITMAP 10,20,1,1,1,") + 1 + 2


# --------------------------------------------------------------------------
# build_job: page loop + x/y shift (including negative/cropping)
# --------------------------------------------------------------------------


def test_build_job_header_once_and_per_page_commands():
    size = LabelSize(20.0, 20.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    s = JobSettings(size=size, copies=2)
    job = build_job(s, [img, img])
    assert job.count(b"CLS\r\n") == 2
    assert job.count(b"PRINT 1,2\r\n") == 2
    assert job.startswith(header(s))
    # header must appear exactly once (not repeated per page)
    assert job.count(b"SIZE 20 mm,20 mm\r\n") == 1


def test_build_job_positive_x_shift_offsets_bitmap_without_cropping():
    size = LabelSize(20.0, 20.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    s = JobSettings(size=size, x_shift_mm=5.0)
    job = build_job(s, [img])
    x_dots = mm_to_dots(5.0)
    wb = size.width_dots // 8  # 160 // 8 = 20, unchanged (no crop)
    assert "BITMAP {},0,{},{},1,".format(x_dots, wb, size.height_dots).encode() in job


def test_build_job_negative_x_shift_crops_and_places_at_zero():
    size = LabelSize(20.0, 20.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    s = JobSettings(size=size, x_shift_mm=-5.0)
    job = build_job(s, [img])
    crop_dots = mm_to_dots(5.0)
    cropped_width = size.width_dots - crop_dots
    wb = (cropped_width + 7) // 8
    assert "BITMAP 0,0,{},{},1,".format(wb, size.height_dots).encode() in job


def test_build_job_negative_y_shift_crops_and_places_at_zero():
    size = LabelSize(20.0, 20.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    s = JobSettings(size=size, y_shift_mm=-5.0)
    job = build_job(s, [img])
    wb = size.width_dots // 8
    cropped_height = size.height_dots - mm_to_dots(5.0)
    assert "BITMAP 0,0,{},{},1,".format(wb, cropped_height).encode() in job


# --------------------------------------------------------------------------
# describe(): summarizes bitmap payloads, never dumps them in full
# --------------------------------------------------------------------------


def test_describe_never_dumps_full_bitmap_payload():
    size = LabelSize(50.0, 50.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 0)  # solid black
    s = JobSettings(size=size)
    job = build_job(s, [img])
    text = describe(job)
    # The full job is large; the description must stay small.
    assert len(job) > 5000
    assert len(text) < 2000
    assert "bytes of bitmap data" in text
    assert "-- total {} bytes --".format(len(job)) in text


def test_describe_preview_bytes_respected():
    size = LabelSize(50.0, 50.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 0)
    s = JobSettings(size=size)
    job = build_job(s, [img])
    text = describe(job, preview_bytes=4)
    assert "first 4 bytes hex" in text


def test_describe_plain_commands_are_verbatim():
    s = JobSettings(size=PRESETS["4x6"])
    job = header(s) + b"CLS\r\n" + b"PRINT 1,1\r\n"
    text = describe(job)
    assert "SIZE 102 mm,152 mm" in text
    assert "CLS" in text
    assert "PRINT 1,1" in text


def test_describe_handles_multiple_bitmaps():
    size = LabelSize(20.0, 20.0, "t")
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    s = JobSettings(size=size)
    job = build_job(s, [img, img])
    text = describe(job)
    assert text.count("bytes of bitmap data") == 2


# --------------------------------------------------------------------------
# hexdump()
# --------------------------------------------------------------------------


def test_hexdump_basic_format():
    data = bytes(range(16))
    out = hexdump(data)
    assert out.startswith("00000000  ")
    assert "0f" in out
    assert "|" in out


def test_hexdump_ascii_column_printable_and_dots():
    data = b"A\x00B\x01"
    out = hexdump(data)
    assert "|A.B.|" in out


def test_hexdump_multi_line_offsets():
    data = bytes(range(32))
    out = hexdump(data)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("00000000")
    assert lines[1].startswith("00000010")


def test_hexdump_limit_truncates():
    data = bytes(range(64))
    out_full = hexdump(data)
    out_limited = hexdump(data, limit=16)
    assert len(out_limited.splitlines()) == 1
    assert len(out_limited) < len(out_full)


# --------------------------------------------------------------------------
# decode_status()
# --------------------------------------------------------------------------


def test_decode_status_zero_is_ready():
    assert decode_status(0x00) == ["ready"]


def test_decode_status_single_bits():
    assert decode_status(0x01) == ["head_open"]
    assert decode_status(0x02) == ["paper_jam"]
    assert decode_status(0x04) == ["out_of_paper"]
    assert decode_status(0x08) == ["out_of_ribbon"]
    assert decode_status(0x10) == ["pause"]
    assert decode_status(0x20) == ["printing"]
    assert decode_status(0x80) == ["other_error"]


def test_decode_status_combination():
    assert decode_status(0x03) == ["head_open", "paper_jam"]
    assert decode_status(0x05) == ["head_open", "out_of_paper"]


def test_status_query_bytes():
    assert STATUS_QUERY == b"\x1b!?"


# --------------------------------------------------------------------------
# SUPPORTED_COMMANDS: every job we build must stick to the verified subset
# --------------------------------------------------------------------------


def _assert_only_supported_commands(job: bytes):
    for line, payload in _split_commands(job):
        if payload is not None:
            continue  # BITMAP payload bytes, not a command line
        text = line.decode("ascii", "replace").strip()
        if not text:
            continue
        assert any(text.startswith(cmd) for cmd in SUPPORTED_COMMANDS), (
            "job used an unsupported/untested command: {!r}".format(text)
        )


def test_supported_commands_excludes_text_box_bar_gapdetect():
    for banned in ("TEXT", "BOX", "BAR", "GAPDETECT", "FORMFEED", "SETC PAUSEKEY"):
        assert banned not in SUPPORTED_COMMANDS


def test_build_job_only_uses_supported_commands():
    size = PRESETS["4x6"]
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    for media in ("gap", "bline", "continuous"):
        job = build_job(JobSettings(size=size, media=media, copies=2), [img, img])
        _assert_only_supported_commands(job)


def test_selftest_job_only_uses_supported_commands():
    for size in list(PRESETS.values()) + [parse_size("0.6x0.6in"), parse_size("1x1in")]:
        _assert_only_supported_commands(selftest_job(JobSettings(size=size)))


def test_feed_job_only_uses_supported_commands():
    _assert_only_supported_commands(feed_job(JobSettings(size=PRESETS["4x6"])))


# --------------------------------------------------------------------------
# calibrate_instructions() / feed_job(): no printer commands, just a feed
# --------------------------------------------------------------------------


def test_calibrate_instructions_sends_nothing_and_describes_the_manual_procedure():
    text = calibrate_instructions()
    assert text is CALIBRATION_INSTRUCTIONS
    assert "Nothing is sent to the printer" in text
    assert "GAPDETECT" in text
    assert "close" in text.lower() and "cover" in text.lower()
    assert "ONE beep" in text
    assert "TWO beeps" in text
    assert "THREE beeps" in text
    assert "green" in text.lower() and "blue" in text.lower() and "red" in text.lower()


def test_feed_job_is_header_cls_print_no_bitmap():
    s = JobSettings(size=PRESETS["4x6"])
    job = feed_job(s)
    assert job == header(s) + b"CLS\r\n" + b"PRINT 1,1\r\n"
    assert b"BITMAP" not in job
    assert b"FORMFEED" not in job


def test_feed_job_reflects_settings():
    s = JobSettings(size=PRESETS["2x1"], density=7, speed=2)
    job = feed_job(s)
    assert b"DENSITY 7\r\n" in job
    assert b"SPEED 2\r\n" in job


# --------------------------------------------------------------------------
# selftest_image() / selftest_job(): must be readable, never crash, scale
# --------------------------------------------------------------------------


def test_selftest_image_is_label_sized_1bit():
    size = PRESETS["4x6"]
    img = selftest_image(JobSettings(size=size))
    assert img.mode == "1"
    assert img.size == (size.width_dots, size.height_dots)


def test_selftest_job_4x6_is_header_cls_one_bitmap_print():
    s = JobSettings(size=PRESETS["4x6"], bitmap_black_is_one=True)
    job = selftest_job(s)
    assert job.startswith(header(s) + b"CLS\r\n")
    assert job.count(b"BITMAP") == 1
    assert job.rstrip(b"\r\n").endswith(b"PRINT 1,1")
    assert b"TEXT" not in job and b"BOX " not in job and b"BAR " not in job
    assert b"GAPDETECT" not in job


def test_selftest_job_honours_x_and_y_shift():
    # Regression: selftest_job used to hard-code BITMAP 0,0, ignoring
    # --x-shift/--y-shift entirely, so calibrating against the self-test and
    # saving the shift silently did nothing for the self-test itself.
    s0 = JobSettings(size=PRESETS["4x6"])
    s5 = JobSettings(size=PRESETS["4x6"], x_shift_mm=5.0, y_shift_mm=5.0)
    job0 = selftest_job(s0)
    job5 = selftest_job(s5)
    assert b"BITMAP 0,0," in job0
    assert b"BITMAP 0,0," not in job5
    x_dots, y_dots = mm_to_dots(5.0), mm_to_dots(5.0)
    assert "BITMAP {},{},".format(x_dots, y_dots).encode("ascii") in job5


def test_selftest_job_honours_copies():
    s = JobSettings(size=PRESETS["2x1"], copies=3)
    assert selftest_job(s).rstrip(b"\r\n").endswith(b"PRINT 1,3")


def test_selftest_job_never_crashes_on_any_preset():
    for size in PRESETS.values():
        s = JobSettings(size=size)
        job = selftest_job(s)
        assert job.startswith(b"SIZE")
        assert job.rstrip(b"\r\n").endswith(b"PRINT 1,1")


def test_selftest_job_skips_swatch_on_tiny_label():
    # 0.6x0.6in leaves no room for the swatch: the lower-center interior
    # (well clear of the border/ruler ticks, which only run along the
    # top/left edges) must stay blank.
    tiny = parse_size("0.6x0.6in")
    img = selftest_image(JobSettings(size=tiny))
    assert img.getpixel((tiny.width_dots // 2, tiny.height_dots * 3 // 4)) == 255
    # Still exactly one BITMAP -- a valid, well-formed job.
    assert selftest_job(JobSettings(size=tiny)).count(b"BITMAP") == 1


def test_selftest_job_small_label_keeps_a_shrunk_swatch():
    # The polarity swatch is the most important part; 1x1in still gets one.
    size = parse_size("1x1in")
    img = selftest_image(JobSettings(size=size))
    assert img.getpixel((size.width_dots // 4, size.height_dots * 3 // 4)) == 0


def test_selftest_wrong_polarity_inverts_the_whole_label():
    # Everything (not just a swatch) now goes through one BITMAP, so a wrong
    # black_is_one guess (encode side) inverts the whole self-test label --
    # an even more obvious tell than the old TEXT/BOX self-test could give.
    size = PRESETS["4x6"]
    s = JobSettings(size=size, bitmap_black_is_one=False)
    img = selftest_image(s)
    right = header(s) + b"CLS\r\n" + bitmap_command(0, 0, img, False) + b"PRINT 1,1\r\n"
    wrong = header(s) + b"CLS\r\n" + bitmap_command(0, 0, img, True) + b"PRINT 1,1\r\n"
    page_right = simulate(right, size.width_dots, size.height_dots, False)[0]
    page_wrong = simulate(wrong, size.width_dots, size.height_dots, False)[0]
    assert list(page_wrong.getdata()) == [255 - v for v in page_right.getdata()]


# --------------------------------------------------------------------------
# simulate(): decodes a job back to pixels (self-test preview, round trips)
# --------------------------------------------------------------------------


def _odd_page():
    size = LabelSize(20.0, 10.0, "odd")  # 160 x 80 dots
    img = Image.new("1", (size.width_dots - 3, size.height_dots), 255)  # width not /8
    for x in range(0, img.width, 3):
        for y in range(0, img.height, 2):
            img.putpixel((x, y), 0)
    return size, img


def test_simulate_round_trips_build_job_both_polarities():
    size, img = _odd_page()
    for black_is_one in (False, True):
        s = JobSettings(size=size, bitmap_black_is_one=black_is_one)
        pages = simulate(build_job(s, [img]), size.width_dots, size.height_dots, black_is_one)
        assert len(pages) == 1
        got = pages[0].crop((0, 0, img.width, img.height))
        assert list(got.getdata()) == list(img.getdata())
        # padding/unused area stays white
        assert pages[0].crop((img.width, 0, size.width_dots, size.height_dots)).getextrema() == (255, 255)


def test_simulate_wrong_polarity_inverts():
    size, img = _odd_page()
    job = build_job(JobSettings(size=size, bitmap_black_is_one=False), [img])
    wrong = simulate(job, size.width_dots, size.height_dots, black_is_one=True)[0]
    got = wrong.crop((0, 0, img.width, img.height))
    assert list(got.getdata()) == [255 - v for v in img.getdata()]


def test_pack_bitmap_is_fast_on_a_4x6_page():
    import time

    size = PRESETS["4x6"]
    img = Image.new("1", (size.width_dots, size.height_dots), 255)
    t = time.perf_counter()
    wb, h, data = pack_bitmap(img, black_is_one=False)
    assert time.perf_counter() - t < 0.5
    assert (wb, h, len(data)) == (102, 1218, 102 * 1218)
    assert set(data) == {0xFF}  # all white, padding white


def test_selftest_swatch_left_half_black_in_simulation():
    size = PRESETS["4x6"]
    for black_is_one in (False, True):
        s = JobSettings(size=size, bitmap_black_is_one=black_is_one)
        page = simulate(selftest_job(s), size.width_dots, size.height_dots, black_is_one)[0]
        sw, sh = mm_to_dots(30.0), mm_to_dots(12.0)
        sx = (size.width_dots - sw) // 2
        sy = size.height_dots - mm_to_dots(2.0) - sh
        mid_y = sy + sh // 2
        assert page.getpixel((sx + sw // 4, mid_y)) == 0  # left half black
        assert page.getpixel((sx + 3 * sw // 4, mid_y)) == 255  # right half white


def test_selftest_image_never_crashes_on_extreme_aspect_ratios():
    # Very thin/tall or thin/wide labels used to be a risk for the old
    # TEXT/BOX layout math; the pixel-drawing version must degrade (skip
    # elements) rather than raise or draw outside the canvas.
    for size in (
        LabelSize(15.0, 100.0, "thin-tall"),
        LabelSize(100.0, 15.0, "thin-wide"),
        LabelSize(6.0, 6.0, "postage"),
    ):
        img = selftest_image(JobSettings(size=size))
        assert img.size == (size.width_dots, size.height_dots)
