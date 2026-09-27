"""Tests for the native CUPS filter cups/rastertotspl.c.

The filter is compiled into a temp dir (host arch only, via cups/Makefile) and
run on synthetic CUPS raster v3 streams written by a tiny writer below, and --
when macOS ``cupsfilter`` is available -- end to end on a PDF through Apple's
cgpdftoraster exactly as cupsd would chain it. Nothing here installs anything
or talks to a printer: the filter only writes TSPL to stdout.

Skips cleanly when clang/make or the SDK's CUPS headers are missing.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from conftest import build_pdf, pdf_rect_fill, pdf_text
from munbyn import tspl
from munbyn.labels import LabelSize

REPO = Path(__file__).resolve().parent.parent
CUPS_DIR = REPO / "cups"
PPD_PATH = CUPS_DIR / "munbyn-rw403b-native.ppd"
INSTALL_SCRIPT = REPO / "scripts" / "install-cups-queue.sh"

# The job verified on the real RW403B (4x6in gap labels, 2026-09-27), up to
# the bitmap payload.
VERIFIED_PREFIX = (
    b"SIZE 102 mm,152 mm\r\n"
    b"GAP 3 mm,0 mm\r\n"
    b"REFERENCE 0,0\r\n"
    b"OFFSET 0 mm\r\n"
    b"SETC AUTODOTTED OFF\r\n"
    b"DENSITY 12\r\n"
    b"SPEED 4\r\n"
    b"DIRECTION 0,0\r\n"
    b"CLS\r\n"
    b"BITMAP 0,0,102,1218,1,"
)

# Every text command the firmware is known to accept. Anything else (TEXT,
# BOX, BAR, ...) made the printer print nothing, so the filter must never
# emit it.
ALLOWED = [
    re.compile(p)
    for p in (
        r"SIZE \d+ mm,\d+ mm",
        r"GAP \d+ mm,\d+ mm",
        r"GAP 0,0",
        r"BLINE \d+ mm,\d+ mm",
        r"REFERENCE 0,0",
        r"OFFSET 0 mm",
        r"SETC AUTODOTTED OFF",
        r"DENSITY (\d|1[0-5])",
        r"SPEED [1-8]",
        r"DIRECTION 0,0",
        r"CLS",
        r"PRINT 1,\d+",
    )
]

# CUPS raster color spaces used here (cups/raster.h).
CS_W, CS_RGB, CS_K = 0, 1, 3

W4X6, H4X6 = 812, 1218  # 4x6in at 203 dpi


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def filter_bin(tmp_path_factory):
    if sys.platform != "darwin":
        pytest.skip("the filter builds against the macOS SDK")
    for tool in ("make", "clang", "xcrun"):
        if shutil.which(tool) is None:
            pytest.skip("{} not found".format(tool))
    sdk = subprocess.run(
        ["xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    if not sdk or not Path(sdk, "usr/include/cups/raster.h").exists():
        pytest.skip("macOS SDK with CUPS headers not found")
    out = tmp_path_factory.mktemp("cupsfilter") / "rastertotspl"
    proc = subprocess.run(
        ["make", "-C", str(CUPS_DIR), "OUT={}".format(out), "ARCHS=-arch {}".format(platform.machine())],
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "warning" not in proc.stderr.lower(), proc.stderr
    return out


# ---------------------------------------------------------------------------
# Minimal CUPS raster v3 writer (uncompressed, little-endian "3SaR")
# ---------------------------------------------------------------------------


def raster_page(img, cspace=CS_W, bpc=8, num_copies=1, page_pt=None):
    """One page: (1796-byte cups_page_header2_t, pixel data) from a PIL image."""
    gray = img.convert("L")
    width, height = gray.size
    if cspace == CS_RGB:
        data = img.convert("RGB").tobytes()
        bpp, ncolors = bpc * 3, 3
    elif bpc == 8:
        data = gray.tobytes()
        if cspace == CS_K:  # ink: 255 = black
            data = bytes(255 - b for b in data)
        bpp, ncolors = 8, 1
    elif bpc == 1:
        data = gray.convert("1", dither=Image.Dither.NONE).tobytes()  # 1 = white
        if cspace == CS_K:  # 1 = black
            data = bytes(255 - b for b in data)
        bpp, ncolors = 1, 1
    else:  # pragma: no cover - test helper misuse
        raise ValueError(bpc)
    bpl = (width * bpp + 7) // 8
    assert len(data) == bpl * height
    wpt, hpt = page_pt or (width * 72.0 / 203, height * 72.0 / 203)

    h = bytearray(1796)

    def u32(off, v):
        struct.pack_into("<I", h, off, v)

    def f32(off, v):
        struct.pack_into("<f", h, off, v)

    u32(276, 203)  # HWResolution
    u32(280, 203)
    u32(340, num_copies)  # NumCopies
    u32(352, int(round(wpt)))  # PageSize (points)
    u32(356, int(round(hpt)))
    u32(372, width)  # cupsWidth
    u32(376, height)  # cupsHeight
    u32(384, bpc)  # cupsBitsPerColor
    u32(388, bpp)  # cupsBitsPerPixel
    u32(392, bpl)  # cupsBytesPerLine
    u32(396, 0)  # cupsColorOrder: chunked
    u32(400, cspace)  # cupsColorSpace
    u32(420, ncolors)  # cupsNumColors
    f32(428, wpt)  # cupsPageSize (points)
    f32(432, hpt)
    return bytes(h) + data


def raster(*pages):
    return struct.pack("<I", 0x52615333) + b"".join(pages)


def test_label(width=W4X6, height=H4X6):
    """White page: black 100-dot square top-left, gray 150 and gray 200 blocks,
    and the rightmost column black (to check row padding stays white)."""
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 99, 99], fill=0)
    d.rectangle([300, 600, 399, 699], fill=150)
    d.rectangle([500, 600, 599, 699], fill=200)
    d.line([(width - 1, 0), (width - 1, height - 1)], fill=0)
    return img


test_label.__test__ = False  # a helper, not a test


# ---------------------------------------------------------------------------
# Running and decoding
# ---------------------------------------------------------------------------


def run_filter(binary, data, tmp_path, options="", copies="1", ppd=PPD_PATH, use_stdin=False):
    env = dict(os.environ)
    env.pop("PPD", None)
    if ppd is not None:
        env["PPD"] = str(ppd)
    args = [str(binary), "42", "dwight", "test", copies, options]
    kwargs = {}
    if use_stdin:
        kwargs["input"] = data
    else:
        path = tmp_path / "page.ras"
        path.write_bytes(data)
        args.append(str(path))
    return subprocess.run(args, capture_output=True, env=env, timeout=60, **kwargs)


def ok_job(*args, **kwargs):
    proc = run_filter(*args, **kwargs)
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"ERROR" not in proc.stderr, proc.stderr.decode()
    return proc.stdout


_BITMAP_RE = re.compile(rb"BITMAP (\d+),(\d+),(\d+),(\d+),1,")


def parse(job):
    """[(text, None) | ("BITMAP", (x, y, wb, h, data))], strictly CRLF-framed."""
    cmds = []
    pos = 0
    while pos < len(job):
        m = _BITMAP_RE.match(job, pos)
        if m:
            x, y, wb, h = (int(g) for g in m.groups())
            end = m.end() + wb * h
            assert job[end : end + 2] == b"\r\n", "BITMAP payload not followed by CRLF"
            cmds.append(("BITMAP", (x, y, wb, h, job[m.end() : end])))
            pos = end + 2
            continue
        nl = job.index(b"\r\n", pos)
        cmds.append((job[pos:nl].decode("ascii"), None))
        pos = nl + 2
    return cmds


def text_lines(job):
    return [c for c, payload in parse(job) if payload is None]


def bitmaps(job):
    """Each BITMAP as a mode "L" image: 0 = black dot, 255 = white (bit 1)."""
    out = []
    for cmd, payload in parse(job):
        if payload is not None:
            x, y, wb, h, data = payload
            assert (x, y) == (0, 0)
            out.append(Image.frombytes("1", (wb * 8, h), data).convert("L"))
    return out


def assert_only_verified_commands(job):
    for line in text_lines(job):
        assert any(p.fullmatch(line) for p in ALLOWED), "unverified TSPL command: {!r}".format(line)


# ---------------------------------------------------------------------------
# The verified 4x6 job
# ---------------------------------------------------------------------------


def test_4x6_defaults_match_the_verified_job_byte_for_byte(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path)
    assert job.startswith(VERIFIED_PREFIX)
    assert len(job) == len(VERIFIED_PREFIX) + 124236 + len(b"\r\nPRINT 1,1\r\n")
    assert job.endswith(b"\r\nPRINT 1,1\r\n")
    # The Python side builds the same preamble.
    py = tspl.header(tspl.JobSettings(size=LabelSize(4 * 25.4, 6 * 25.4)))
    assert VERIFIED_PREFIX.startswith(py)
    assert_only_verified_commands(job)


def test_bitmap_is_upright_black_is_zero_and_padding_white(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path)
    (img,) = bitmaps(job)
    assert img.size == (816, H4X6)
    assert img.getpixel((50, 50)) == 0  # black square stays top-left
    assert img.getpixel((150, 50)) == 255
    assert img.getpixel((700, 1100)) == 255
    assert img.getpixel((350, 650)) == 0  # gray 150 < threshold 160
    assert img.getpixel((550, 650)) == 255  # gray 200 >= 160
    assert img.getpixel((811, 600)) == 0  # rightmost real column
    # Last byte = dots 808..815: 808-810 white, 811 black, padding 812-815 white.
    (_, (_, _, wb, h, data)) = [c for c in parse(job) if c[1] is not None][0]
    assert all(data[r * wb + wb - 1] == 0b11101111 for r in range(h))


@pytest.mark.parametrize(
    "cspace,bpc",
    [(CS_W, 8), (CS_K, 8), (CS_W, 1), (CS_K, 1), (CS_RGB, 8)],
    ids=["W8", "K8", "W1", "K1", "RGB8"],
)
def test_color_space_polarities_decode_the_same(filter_bin, tmp_path, cspace, bpc):
    img = test_label()
    if bpc == 1:  # 1-bit input has no gray levels; drop the gray blocks
        img = img.point(lambda v: 0 if v < 128 else 255)
    job = ok_job(filter_bin, raster(raster_page(img, cspace=cspace, bpc=bpc)), tmp_path)
    (out,) = bitmaps(job)
    assert out.getpixel((50, 50)) == 0
    assert out.getpixel((150, 50)) == 255
    assert out.getpixel((811, 10)) == 0
    assert out.getpixel((700, 1100)) == 255
    assert out.getpixel((814, 10)) == 255  # padding


def test_stdin_input(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path, use_stdin=True)
    assert job.startswith(VERIFIED_PREFIX)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def head(job):
    return text_lines(job)[:8]


def test_job_options_map_to_tspl(filter_bin, tmp_path):
    data = raster(raster_page(test_label()))
    job = ok_job(filter_bin, data, tmp_path, options="Darkness=8 PrintSpeed=30 MediaType=0")
    assert head(job) == [
        "SIZE 102 mm,152 mm", "GAP 0,0", "REFERENCE 0,0", "OFFSET 0 mm",
        "SETC AUTODOTTED OFF", "DENSITY 8", "SPEED 3", "DIRECTION 0,0",
    ]
    job = ok_job(filter_bin, data, tmp_path, options="MediaType=2 GapHeight=5 GapOffset=1 Darkness=16 PrintSpeed=80")
    assert head(job)[1] == "BLINE 5 mm,1 mm"
    assert "DENSITY 15" in head(job)  # 16 clamps to TSPL's max
    assert "SPEED 8" in head(job)
    assert_only_verified_commands(job)


def test_bad_option_values_fall_back_to_defaults(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path, options="Darkness=abc PrintSpeed=")
    assert job.startswith(VERIFIED_PREFIX)


def test_ppd_defaults_are_used_and_job_options_override_them(filter_bin, tmp_path):
    ppd = tmp_path / "custom.ppd"
    text = PPD_PATH.read_text(encoding="ascii")
    text = text.replace("*DefaultDarkness: 12", "*DefaultDarkness: 10")
    text = text.replace("*DefaultMediaType: 1", "*DefaultMediaType: 2")
    ppd.write_text(text, encoding="ascii")
    data = raster(raster_page(test_label()))
    job = ok_job(filter_bin, data, tmp_path, ppd=ppd)
    assert head(job)[1] == "BLINE 3 mm,0 mm"
    assert "DENSITY 10" in head(job)
    job = ok_job(filter_bin, data, tmp_path, ppd=ppd, options="Darkness=7 MediaType=1")
    assert head(job)[1] == "GAP 3 mm,0 mm"
    assert "DENSITY 7" in head(job)


def test_no_ppd_uses_built_in_defaults(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path, ppd=None)
    assert job.startswith(VERIFIED_PREFIX)


def test_threshold_option(filter_bin, tmp_path):
    data = raster(raster_page(test_label()))
    (img,) = bitmaps(ok_job(filter_bin, data, tmp_path, options="Threshold=128"))
    assert img.getpixel((350, 650)) == 255  # gray 150 is now white
    (img,) = bitmaps(ok_job(filter_bin, data, tmp_path, options="Threshold=224"))
    assert img.getpixel((550, 650)) == 0  # gray 200 is now black


@pytest.mark.parametrize("mode", ["2", "3", "4"])
def test_dither_modes_give_mid_gray_a_mix_of_dots(filter_bin, tmp_path, mode):
    img = Image.new("L", (W4X6, H4X6), 255)
    ImageDraw.Draw(img).rectangle([100, 100, 499, 499], fill=128)
    job = ok_job(filter_bin, raster(raster_page(img)), tmp_path, options="PrintMode=" + mode)
    (out,) = bitmaps(job)
    patch = out.crop((100, 100, 500, 500))
    black = sum(1 for v in patch.getdata() if v == 0) / (400 * 400)
    assert 0.35 < black < 0.65
    assert out.getpixel((700, 1100)) == 255
    assert_only_verified_commands(job)


def test_rotate_180_is_done_in_the_bitmap(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path, options="Rotate=1")
    assert "DIRECTION 0,0" in head(job)
    (img,) = bitmaps(job)
    assert img.getpixel((50, 50)) == 255
    assert img.getpixel((811 - 50, H4X6 - 1 - 50)) == 0


def test_rotate_90_turns_a_landscape_page_onto_the_label(filter_bin, tmp_path):
    # A 6x4in landscape page with a black square top-left.
    img = Image.new("L", (H4X6, W4X6), 255)
    ImageDraw.Draw(img).rectangle([0, 0, 99, 99], fill=0)
    job = ok_job(filter_bin, raster(raster_page(img)), tmp_path, options="Rotate=2")
    assert head(job)[0] == "SIZE 102 mm,152 mm"
    (out,) = bitmaps(job)
    assert out.size == (816, H4X6)
    assert out.getpixel((811 - 50, 50)) == 0  # clockwise: top-left -> top-right
    assert out.getpixel((50, 50)) == 255


def test_horizontal_and_vertical_offsets_shift_and_crop(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label())), tmp_path, options="Horizontal=5 Vertical=-3")
    (img,) = bitmaps(job)
    dx, dy = 40, 24  # round(5 / 25.4 * 203), round(3 / 25.4 * 203)
    assert img.size == (816, H4X6)  # full label height kept
    assert img.getpixel((dx, 0)) == 0 and img.getpixel((dx - 1, 0)) == 255
    assert img.getpixel((dx + 99, 99 - dy)) == 0 and img.getpixel((dx + 99, 100 - dy)) == 255


# ---------------------------------------------------------------------------
# Copies, pages, sizes, errors
# ---------------------------------------------------------------------------


def test_copies_come_from_numcopies_else_argv(filter_bin, tmp_path):
    page = test_label(200, 100)
    # cgpdftoraster sets NumCopies to the count when the printer does copies...
    job = ok_job(filter_bin, raster(raster_page(page, num_copies=2)), tmp_path, copies="2")
    assert text_lines(job)[-1] == "PRINT 1,2"
    # ...and to 1 when it already produced collated copies itself.
    job = ok_job(filter_bin, raster(raster_page(page, num_copies=1)), tmp_path, copies="2")
    assert text_lines(job)[-1] == "PRINT 1,1"
    # A header without NumCopies falls back to argv[4].
    job = ok_job(filter_bin, raster(raster_page(page, num_copies=0)), tmp_path, copies="3")
    assert text_lines(job)[-1] == "PRINT 1,3"


def test_multi_page_sends_the_header_once(filter_bin, tmp_path):
    job = ok_job(filter_bin, raster(raster_page(test_label()), raster_page(test_label())), tmp_path)
    lines = text_lines(job)
    assert sum(1 for l in lines if l.startswith("SIZE ")) == 1
    assert lines.count("CLS") == 2 and lines.count("PRINT 1,1") == 2
    assert len(bitmaps(job)) == 2


def test_header_resent_when_the_page_size_changes(filter_bin, tmp_path):
    small = Image.new("L", (812, 406), 255)  # 4x2in
    job = ok_job(filter_bin, raster(raster_page(test_label()), raster_page(small)), tmp_path)
    sizes = [l for l in text_lines(job) if l.startswith("SIZE ")]
    assert sizes == ["SIZE 102 mm,152 mm", "SIZE 102 mm,51 mm"]


def ppd_page_sizes():
    sizes = re.findall(r"^\*PageSize w(\d+)h(\d+)/", PPD_PATH.read_text(encoding="ascii"), re.M)
    return [(int(w), int(h)) for w, h in sizes]


def test_size_rounding_matches_python_header_for_every_ppd_size(filter_bin, tmp_path):
    sizes = ppd_page_sizes()
    assert (288, 432) in sizes and (288, 288) in sizes and (288, 72) in sizes
    for wpt, hpt in sizes + [(288, 180), (306.14, 1440)]:
        wd, hd = int(round(wpt / 72 * 203)), int(round(hpt / 72 * 203))
        img = Image.new("L", (wd, hd), 255)
        job = ok_job(filter_bin, raster(raster_page(img, page_pt=(wpt, hpt))), tmp_path)
        expected = tspl.header(tspl.JobSettings(size=LabelSize(wpt / 72 * 25.4, hpt / 72 * 25.4)))
        assert head(job)[0] == expected.split(b"\r\n")[0].decode(), (wpt, hpt)
        (out,) = bitmaps(job)
        assert out.size == ((wd + 7) // 8 * 8, hd)


def test_empty_stream_and_bad_args_fail_cleanly(filter_bin, tmp_path):
    proc = run_filter(filter_bin, raster(), tmp_path)
    assert proc.returncode != 0 and b"ERROR: no pages" in proc.stderr
    assert proc.stdout == b""
    proc = subprocess.run([str(filter_bin), "1", "u"], capture_output=True, timeout=60)
    assert proc.returncode != 0 and b"Usage" in proc.stderr


# ---------------------------------------------------------------------------
# PPD and install script (static checks only; the script is never run)
# ---------------------------------------------------------------------------


def test_ppd_names_and_filter():
    text = PPD_PATH.read_text(encoding="ascii")
    assert '*cupsFilter:            "application/vnd.cups-raster 0 /Library/Printers/Munbyn/rastertotspl"' in text
    assert '*NickName:              "Munbyn RW403B (native)"' in text
    assert '*PCFileName:            "RW403BN.ppd"' in text
    assert "*cupsManualCopies:      False" in text
    for default in ("DefaultPageSize: w288h432", "DefaultDarkness: 12", "DefaultPrintSpeed: 40",
                    "DefaultMediaType: 1", "DefaultGapHeight: 3", "DefaultGapOffset: 0",
                    "DefaultThreshold: 160"):
        assert "*" + default in text
    assert "cupsBitsPerColor 8/cupsRowCount 8/cupsRowFeed 0/cupsRowStep 0/cupsColorSpace 0" in text
    assert "rastertorw403b" not in text


def test_ppd_passes_cupstestppd():
    if shutil.which("cupstestppd") is None:
        pytest.skip("cupstestppd not found")
    # -I filters: the filter is only present once installed (root-owned).
    proc = subprocess.run(
        ["cupstestppd", "-W", "translations", "-I", "filters", str(PPD_PATH)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout


def test_install_script_parses_and_requires_root():
    proc = subprocess.run(["/bin/bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    text = INSTALL_SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert "EUID" in text
    assert "lpadmin -p \"$QUEUE\"" in text and "QUEUE=Munbyn_RW403B_Native" in text
    # Never touches Munbyn's queue or files, nor the default printer by accident.
    assert text.count("lpadmin -x") == 1 and 'lpadmin -x "$QUEUE"' in text
    assert "rastertorw403b" not in text and "RW403B.ppd" not in text
    assert text.count("lpadmin -d") == 1


# ---------------------------------------------------------------------------
# End to end through macOS cupsfilter (PDF -> cgpdftoraster -> rastertotspl)
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_ppd(filter_bin, tmp_path):
    if shutil.which("cupsfilter") is None:
        pytest.skip("cupsfilter not found")
    text = PPD_PATH.read_text(encoding="ascii").replace(
        "/Library/Printers/Munbyn/rastertotspl", str(filter_bin)
    )
    path = tmp_path / "chain.ppd"
    path.write_text(text, encoding="ascii")
    return path


def cupsfilter_job(ppd, pdf_bytes, tmp_path, *args):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(pdf_bytes)
    proc = subprocess.run(
        ["cupsfilter", *args, "-p", str(ppd), "-m", "printer/foo", "-e", str(pdf)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    return proc.stdout


def label_pdf(tag):
    content = pdf_rect_fill(0, 432 - 72, 72, 72) + pdf_text(90, 432 - 50, 30, tag)
    return 288, 432, content


def test_cupsfilter_pdf_end_to_end(chain_ppd, tmp_path):
    job = cupsfilter_job(chain_ppd, build_pdf([label_pdf("TOP")]), tmp_path)
    assert job.startswith(VERIFIED_PREFIX)
    assert job.endswith(b"\r\nPRINT 1,1\r\n")
    (img,) = bitmaps(job)
    assert img.getpixel((100, 100)) == 0  # the 1in square: top-left, black
    assert img.getpixel((100, 300)) == 255
    assert img.getpixel((700, 1100)) == 255


def test_cupsfilter_copies_and_collation(chain_ppd, tmp_path):
    pdf = build_pdf([label_pdf("P1"), label_pdf("P2")])
    job = cupsfilter_job(chain_ppd, pdf, tmp_path, "-n", "2")
    assert [l for l in text_lines(job) if l.startswith("PRINT")] == ["PRINT 1,2", "PRINT 1,2"]
    # Collated: cgpdftoraster renders P1 P2 P1 P2 with NumCopies 1 and argv[4]
    # still 2 -- so using argv[4] would print 8 labels instead of 4.
    job = cupsfilter_job(chain_ppd, pdf, tmp_path, "-n", "2", "-o", "Collate=True")
    assert [l for l in text_lines(job) if l.startswith("PRINT")] == ["PRINT 1,1"] * 4
    job = cupsfilter_job(chain_ppd, pdf, tmp_path, "-o", "Darkness=8", "-o", "PrintSpeed=30", "-o", "MediaType=0")
    assert head(job)[1] == "GAP 0,0" and "DENSITY 8" in head(job) and "SPEED 3" in head(job)
    assert_only_verified_commands(job)
