"""Tests for munbyn.tspl: header bytes, bitmap packing, describe/hexdump,
status decoding, and the self-test/calibrate/feed jobs."""
from __future__ import annotations

from PIL import Image

from munbyn.labels import LabelSize, PRESETS, mm_to_dots, parse_size
from munbyn.tspl import (
    _DOT_FONTS,
    STATUS_QUERY,
    JobSettings,
    bitmap_command,
    build_job,
    calibrate_job,
    decode_status,
    describe,
    feed_job,
    header,
    hexdump,
    pack_bitmap,
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
# calibrate_job() / feed_job()
# --------------------------------------------------------------------------


def test_calibrate_job():
    assert calibrate_job() == b"GAPDETECT\r\n"


def test_feed_job():
    assert feed_job() == b"FORMFEED\r\n"


# --------------------------------------------------------------------------
# selftest_job(): must be readable, never crash, and scale to small labels
# --------------------------------------------------------------------------


def test_selftest_job_4x6_contains_expected_elements():
    s = JobSettings(size=PRESETS["4x6"], bitmap_black_is_one=True)
    job = selftest_job(s)
    text = job.decode("ascii", "replace")
    assert "MUNBYN RW403B TEST" in text
    assert "polarity: black_is_one=1" in text
    assert "BOX " in text
    assert "BAR " in text
    assert "BITMAP" in text  # big enough for the polarity swatch
    assert "LEFT HALF SHOULD BE BLACK" in text
    assert text.strip().endswith("PRINT 1,1")


def test_selftest_job_never_crashes_on_any_preset():
    for size in PRESETS.values():
        s = JobSettings(size=size)
        job = selftest_job(s)
        assert job.startswith(b"SIZE")
        assert job.rstrip(b"\r\n").endswith(b"PRINT 1,1")


def test_selftest_job_skips_swatch_on_tiny_label():
    tiny = parse_size("0.6x0.6in")
    s = JobSettings(size=tiny)
    job = selftest_job(s)
    assert b"BITMAP" not in job
    assert b"LEFT HALF SHOULD BE BLACK" not in job
    # Still a valid, well-formed job.
    assert job.startswith(b"SIZE")


def test_selftest_job_small_label_keeps_a_shrunk_swatch():
    # The polarity swatch is the most important part; 1x1in still gets one,
    # with the short caption.
    job = selftest_job(JobSettings(size=parse_size("1x1in")))
    assert b"BITMAP" in job
    assert b"LEFT=BLACK" in job


def test_selftest_job_polarity_flip_reflected_in_text():
    s0 = JobSettings(size=PRESETS["4x6"], bitmap_black_is_one=False)
    job0 = selftest_job(s0)
    assert b"polarity: black_is_one=0" in job0


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


def test_selftest_text_stays_on_the_label_for_every_preset():
    import re

    text_re = re.compile(rb'TEXT (\d+),(\d+),"(\w+)",0,1,1,"([^"]*)"')
    for size in list(PRESETS.values()) + [parse_size("1x1in")]:
        job = selftest_job(JobSettings(size=size))
        for m in text_re.finditer(job):
            x, y = int(m.group(1)), int(m.group(2))
            fw, fh = _DOT_FONTS[m.group(3).decode()]
            right = x + len(m.group(4)) * fw
            assert right <= size.width_dots - mm_to_dots(1.0), (size, m.group(0))
            assert y + fh <= size.height_dots - mm_to_dots(1.0), (size, m.group(0))
