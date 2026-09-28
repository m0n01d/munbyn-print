"""munbyn.tspl_parse: TSPL jobs (as this repo and the CUPS filter build them)
back into page images for the Bluetooth bridge."""
from __future__ import annotations

import pytest
from PIL import Image, ImageChops, ImageDraw

from munbyn import ble_protocol as bp
from munbyn import labels, tspl
from munbyn import tspl_parse as tp


def _settings(**kw):
    kw.setdefault("feed_scale", 0.981)
    return tspl.JobSettings(size=labels.parse_size("4x6"), **kw)


def _page(settings, mark=(40, 60, 300, 200)):
    h = labels.stretched_height_dots(settings.size.height_dots, settings.feed_scale)
    img = Image.new("1", (settings.size.width_dots, h), 255)
    ImageDraw.Draw(img).rectangle(mark, fill=0)
    return img


def _same(a, b):
    return a.size == b.size and ImageChops.difference(a.convert("L"), b.convert("L")).getbbox() is None


def test_build_job_round_trips_to_the_same_page():
    s = _settings(copies=2)
    page = _page(s)
    job = tp.parse_job(tspl.build_job(s, [page]))
    assert len(job.pages) == 1
    got = job.pages[0]
    assert got.copies == 2
    assert got.image.size == (816, 1242)  # 812 padded to whole bytes, rows already feed-stretched
    assert _same(got.image.crop((0, 0, 812, 1242)), page)
    assert got.image.crop((812, 0, 816, 1242)).getextrema() == (255, 255)  # padding is white
    assert job.header["SIZE"] == "102 mm,155 mm" and job.header["DENSITY"] == "12"
    assert got.size_mm == (102.0, 155.0)


def test_polarity_clear_bit_is_black_and_ble_gets_one_is_black():
    s = _settings()
    page = _page(s)
    raw = tspl.build_job(s, [page])
    width_bytes, height, tspl_data = tspl.pack_bitmap(page, black_is_one=False)
    job = tp.parse_job(raw)
    ble = bp.BlePage.from_image(bp.compose_page(job.pages[0].image))
    assert ble.data == bytes(255 - b for b in tspl_data)  # TSPL 0 = black -> BLE 1 = black
    assert ble.data == bp.BlePage.from_image(bp.compose_page(page)).data  # == what --ble-direct sends


def test_selftest_job_matches_the_selftest_image():
    s = _settings()
    job = tp.parse_job(tspl.selftest_job(s))
    assert _same(job.pages[0].image.crop((0, 0, 812, 1242)), tspl.selftest_image(s))


def test_multi_page_single_copy_groups_into_one_run():
    s = _settings(copies=1)
    job = tp.parse_job(tspl.build_job(s, [_page(s), _page(s, (10, 10, 100, 100))]))
    assert [p.copies for p in job.pages] == [1, 1]
    runs = tp.group_pages(job.pages)
    assert len(runs) == 1 and runs[0][1] == 1 and len(runs[0][0]) == 2
    assert job.total_labels == 2


def test_multi_page_with_copies_does_not_collate_pages_across_bluetooth_jobs():
    """2 pages x PRINT 1,3: TSPL prints AAABBB (each page's copies together).
    A single 2-page/3-copy Bluetooth job would send ABABAB instead
    (ble_protocol.plan_sends interleaves a multi-page run's copies), so each
    page must be its own run -- sent as a single page=3 write, which keeps
    AAABBB order."""
    s = _settings(copies=3)
    job = tp.parse_job(tspl.build_job(s, [_page(s), _page(s, (10, 10, 100, 100))]))
    assert [p.copies for p in job.pages] == [3, 3]
    runs = tp.group_pages(job.pages)
    assert len(runs) == 2
    assert [(len(pages), copies) for pages, copies in runs] == [(1, 3), (1, 3)]
    assert job.total_labels == 6


def test_print_m_n_is_m_times_n_and_runs_split_on_different_counts():
    bmp = b"BITMAP 0,0,1,1,1,\x00\r\n"
    job = tp.parse_job(b"CLS\r\n" + bmp + b"PRINT 2,3\r\nPRINT 1\r\n")
    assert [p.copies for p in job.pages] == [6, 1]
    assert [c for _p, c in tp.group_pages(job.pages)] == [6, 1]


def test_buffer_survives_print_until_cls_and_bitmaps_or_together():
    a = b"BITMAP 0,0,1,2,1," + bytes([0b01111111, 0xFF]) + b"\r\n"  # black dot at (0,0)
    b = b"BITMAP 8,1,1,1,1," + bytes([0b11111110]) + b"\r\n"  # black dot at (15,1), extends the page
    job = tp.parse_job(b"CLS\r\n" + a + b"PRINT 1,1\r\n" + b + b"PRINT 1,1\r\nCLS\r\n" + b + b"PRINT 1,1\r\n")
    p1, p2, p3 = (p.image for p in job.pages)
    assert p1.size == (8, 2) and p1.getpixel((0, 0)) == 0
    assert p2.size == (16, 2) and p2.getpixel((0, 0)) == 0 and p2.getpixel((15, 1)) == 0  # OR: both
    assert p3.size == (16, 2) and p3.getpixel((0, 0)) == 255 and p3.getpixel((15, 1)) == 0  # CLS cleared a


def test_mode_1_white_never_erases_black():
    under = b"BITMAP 0,0,1,1,1," + bytes([0x00]) + b"\r\n"  # 8 black dots
    over = b"BITMAP 0,0,1,1,1," + bytes([0xFF]) + b"\r\n"  # 8 white dots on top
    img = tp.parse_job(b"CLS\r\n" + under + over + b"PRINT 1\r\n").pages[0].image
    assert list(img.getdata()) == [0] * 8


def test_cups_filter_style_job_with_lf_endings_and_lowercase():
    data = bytes([0xAA] * 4)
    job = tp.parse_job(b"size 4,6\ngap 3 mm,0 mm\nreference 0,0\noffset 0 mm\nsetc   autodotted off\n"
                       b"density 12\nspeed 4\ndirection 0,0\ncls\nbitmap 0,0,2,2,1," + data + b"\nprint 1,1\n")
    assert job.pages[0].image.size == (16, 2)
    assert job.pages[0].size_mm == pytest.approx((101.6, 152.4))


def test_print_without_bitmap_feeds_a_blank_page_from_size():
    job = tp.parse_job(b"SIZE 102 mm,152 mm\r\nCLS\r\nPRINT 1,1\r\n")
    img = job.pages[0].image
    assert img.size == (815, 1215) and img.getextrema() == (255, 255)
    with pytest.raises(tp.TsplParseError, match="SIZE"):
        tp.parse_job(b"CLS\r\nPRINT 1,1\r\n")


def test_feed_job_parses():
    s = _settings()
    job = tp.parse_job(tspl.feed_job(s))
    assert len(job.pages) == 1


def test_notes_for_unprinted_bitmaps():
    job = tp.parse_job(b"CLS\r\nBITMAP 0,0,1,1,1,\x00\r\nPRINT 1\r\nCLS\r\nBITMAP 0,0,1,1,1,\x00\r\n")
    assert any("after the last PRINT" in n for n in job.notes)


@pytest.mark.parametrize("data, match", [
    (b"", "empty"),
    (b"SIZE 102 mm,152 mm\r\nCLS\r\n", "no PRINT"),
    (b'CLS\r\nTEXT 10,10,"3",0,1,1,"HI"\r\nPRINT 1\r\n', "unsupported TSPL command 'TEXT"),
    (b"CLS\r\nBAR 0,0,10,10\r\nPRINT 1\r\n", "unsupported TSPL command 'BAR"),
    (b"SETC PAUSEKEY OFF\r\nCLS\r\nPRINT 1\r\n", "SETC PAUSEKEY"),
    (b"CLS\r\nBITMAP 0,0,1,1,3,\x00\r\nPRINT 1\r\n", "mode 3"),
    (b"CLS\r\nBITMAP 0,0,4,4,1,\x00\x00\r\n", "truncated"),
    (b"CLS\r\nBITMAP 0,0,x\r\nPRINT 1\r\n", "malformed BITMAP"),
    (b"\x1b!?", "unsupported TSPL command"),
    (b"\xff\xfe%PDF", "not a TSPL command"),
    (b"CLS\r\nBITMAP 0,0,1,1,1,\x00\r\nPRINT 0\r\n", "prints nothing"),
    (b"CLS\r\nBITMAP 0,0,1,1,1,\x00\r\nPRINT 100,100\r\n", "limit"),
    (b"CLS\r\nBITMAP 0,0,1,1,1,\x00\r\nPRINT a\r\n", "PRINT m"),
    (b"CLS\r\nBITMAP 0,0,1,20000,1," + b"\x00" * 20000 + b"\r\nPRINT 1\r\n", "rows tall"),
    (b"CLS 1\r\nPRINT 1\r\n", "CLS takes no"),
])
def test_rejects_anything_outside_the_subset(data, match):
    with pytest.raises(tp.TsplParseError, match=match):
        tp.parse_job(data)


def test_too_wide_bitmap_is_clipped_to_the_bluetooth_maximum():
    wb = 120  # 960 dots
    job = tp.parse_job(b"CLS\r\nBITMAP 0,0,%d,1,1," % wb + b"\x00" * wb + b"\r\nPRINT 1\r\n")
    assert job.pages[0].image.width == tp.MAX_WIDTH_DOTS
    assert any("clipped" in n for n in job.notes)


def test_every_job_builder_output_parses():
    s = _settings()
    for raw in (tspl.selftest_job(s), tspl.scale_test_job(s), tspl.feed_job(s), tspl.build_job(s, [_page(s)])):
        assert tp.parse_job(raw).pages
