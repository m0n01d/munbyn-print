"""Build TSPL (TSC Printer Language) jobs for the Munbyn RW403B.

The printer's own IEEE-1284 device ID reports ``CMD:TSPL`` (confirmed on
hardware) -- this is the older "TSPL" dialect, not "TSPL2". Per the TSC
programming manual, TSPL2-only features (scalable font "0"/ROMAN.TTF) are
NOT assumed to work here; only the manual's fixed dot-matrix fonts are used.

Two details in this module are configurable specifically because research
could not pin them down with certainty for THIS printer/firmware, and are
meant to be confirmed from a real test print rather than trusted blindly:

* ``JobSettings.bitmap_black_is_one`` -- bit polarity of ``BITMAP`` data.
  The default is ``False`` (a CLEAR bit prints a black dot, a set bit is
  white), which is the TSC/EPL convention used by independent working TSPL
  implementations (an open-source TSPL CUPS driver and a hand-written TSPL
  image encoder both clear a bit to print a dot). Munbyn's web editor treats
  1 as black, but that code builds protobuf messages for the Bluetooth path,
  not TSPL, so it says nothing about how the TSPL ``BITMAP`` interpreter
  reads bits. The self-test label's polarity swatch settles it on paper;
  ``--black-is-one 1`` (or the config key) flips it.
* The vendor's own (broken, x86-only) CUPS filter's extracted strings show
  every ``SIZE``/``GAP``/``BLINE``/``OFFSET`` line formatted with a plain
  ``%d`` (integer mm), not decimals -- even though the TSC manual's own
  examples use decimal mm elsewhere. ``header()`` follows the vendor's own
  template literally (round to the nearest whole mm).

``BITMAP`` always uses mode 1 (OR), the raw-bitmap template in the vendor
filter. The vendor's compressed mode 3 has no public spec and is not used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .labels import LabelSize, mm_to_dots


@dataclass
class JobSettings:
    """Everything needed to build one TSPL print job."""

    size: LabelSize
    media: str = "gap"  # gap | bline | continuous
    gap_mm: float = 3.0
    gap_offset_mm: float = 0.0
    density: int = 12  # TSPL DENSITY 0..15; 12 mirrors the vendor PPD's default
    speed: int = 4  # 1..8
    direction: int = 0  # 0 | 1
    offset_mm: float = 0.0  # TSPL OFFSET (tear/peel stop)
    x_shift_mm: float = 0.0  # alignment nudge of the image (+right); negative crops
    y_shift_mm: float = 0.0  # (+down)
    copies: int = 1
    bitmap_black_is_one: bool = False  # see module docstring


def header(s: JobSettings) -> bytes:
    """Build the SIZE..DIRECTION preamble, mirroring the vendor's job sequence."""
    if s.media not in ("gap", "bline", "continuous"):
        raise ValueError("media must be gap, bline or continuous, not {!r}".format(s.media))
    lines = [
        "SIZE {} mm,{} mm".format(round(s.size.width_mm), round(s.size.height_mm)),
    ]
    if s.media == "continuous":
        lines.append("GAP 0,0")
    elif s.media == "bline":
        lines.append(
            "BLINE {} mm,{} mm".format(round(s.gap_mm), round(s.gap_offset_mm))
        )
    else:
        lines.append(
            "GAP {} mm,{} mm".format(round(s.gap_mm), round(s.gap_offset_mm))
        )
    lines.append("REFERENCE 0,0")
    lines.append("OFFSET {} mm".format(round(s.offset_mm)))
    lines.append("SETC AUTODOTTED OFF")
    lines.append("DENSITY {}".format(s.density))
    lines.append("SPEED {}".format(s.speed))
    lines.append("DIRECTION {},0".format(s.direction))
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


_INVERT_TABLE = bytes(255 - i for i in range(256))


def pack_bitmap(img: Image.Image, black_is_one: bool) -> Tuple[int, int, bytes]:
    """Pack a PIL image into TSPL BITMAP row-major, MSB-first bit data.

    Returns ``(width_bytes, height, data)``. Non-"1"-mode images are
    converted with a plain (non-dithered) threshold of 128. Row padding
    bits (when width is not a multiple of 8) always represent white,
    regardless of ``black_is_one``.
    """
    if img.mode != "1":
        img = img.convert("1", dither=Image.Dither.NONE)

    width, height = img.size
    width_bytes = (width + 7) // 8
    # Pillow packs mode "1" MSB-first, rows padded to whole bytes, with
    # 1 = white and padding bits = 0.
    raw = bytearray(img.tobytes("raw", "1"))
    if len(raw) != width_bytes * height:  # pragma: no cover - Pillow invariant
        raise ValueError("unexpected packed bitmap length")
    if black_is_one:
        raw = bytearray(raw.translate(_INVERT_TABLE))

    padding_bits = width_bytes * 8 - width
    if padding_bits and height:
        mask = (1 << padding_bits) - 1
        last = raw[width_bytes - 1 :: width_bytes]
        if black_is_one:  # white is 0: clear the padding bits
            fixed = bytes(b & ~mask & 0xFF for b in last)
        else:  # white is 1: set the padding bits
            fixed = bytes(b | mask for b in last)
        raw[width_bytes - 1 :: width_bytes] = fixed
    return width_bytes, height, bytes(raw)


def bitmap_command(x: int, y: int, img: Image.Image, black_is_one: bool) -> bytes:
    """Build one ``BITMAP`` command (mode 1/OR -- safe after a preceding CLS)."""
    width_bytes, height, data = pack_bitmap(img, black_is_one)
    head = "BITMAP {},{},{},{},1,".format(x, y, width_bytes, height).encode("ascii")
    return head + data + b"\r\n"


def _shift_and_bitmap(s: JobSettings, img: Image.Image) -> bytes:
    """Apply x/y shift (cropping on the negative side) and emit its BITMAP."""
    x_dots = mm_to_dots(s.x_shift_mm)
    y_dots = mm_to_dots(s.y_shift_mm)
    crop_left = max(0, -x_dots)
    crop_top = max(0, -y_dots)
    if crop_left or crop_top:
        img = img.crop((crop_left, crop_top, img.width, img.height))
    return bitmap_command(max(0, x_dots), max(0, y_dots), img, s.bitmap_black_is_one)


def build_job(s: JobSettings, pages: List[Image.Image]) -> bytes:
    """Build a full job: header once, then CLS/BITMAP/PRINT per page."""
    if s.copies < 1:
        raise ValueError("copies must be at least 1")
    parts = [header(s)]
    for page in pages:
        parts.append(b"CLS\r\n")
        parts.append(_shift_and_bitmap(s, page))
        parts.append("PRINT 1,{}\r\n".format(s.copies).encode("ascii"))
    return b"".join(parts)


# Fixed-pitch TSPL dot fonts: name -> (cell width, cell height) in dots.
_DOT_FONTS = {
    "1": (8, 12),
    "2": (12, 20),
    "3": (16, 24),
    "4": (24, 32),
    "5": (32, 48),
    "6": (14, 19),
    "7": (21, 27),
    "8": (14, 25),
}
_SELFTEST_FONTS = ("4", "3", "2", "1")  # largest first


def _rects_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _text_cmd(x: int, y: int, font: str, content: str) -> bytes:
    safe = content.replace('"', "'")
    return 'TEXT {},{},"{}",0,1,1,"{}"\r\n'.format(x, y, font, safe).encode("ascii")


def selftest_job(s: JobSettings) -> bytes:
    """Build a built-in alignment/polarity self-test label.

    Draws a border box (inset 1 mm), mm rulers along the top and left edges
    (5 mm ticks, 10 mm ticks longer), a center crosshair, identifying TEXT
    lines, and a BITMAP-drawn polarity swatch (left half black / right half
    white, thin black outline). BOX/BAR/TEXT are drawn by the printer; only
    the swatch goes through ``BITMAP``, so a wrong polarity shows up as a
    swatch whose RIGHT half is black. Parts that would not fit a small label
    are shrunk or skipped rather than overflowing.
    """
    mm = mm_to_dots
    width_dots = s.size.width_dots
    height_dots = s.size.height_dots
    width_mm = s.size.width_mm
    height_mm = s.size.height_mm
    inset = mm(1.0)
    line = 2  # dots; 1-dot lines are faint on thermal paper

    out: List[bytes] = [header(s), b"CLS\r\n"]

    # Border.
    bx0, by0 = inset, inset
    bx1 = max(bx0 + 1, width_dots - 1 - inset)
    by1 = max(by0 + 1, height_dots - 1 - inset)
    out.append("BOX {},{},{},{},{}\r\n".format(bx0, by0, bx1, by1, line + 1).encode("ascii"))

    # Rulers: ticks measured from the label's own left/top edge.
    short_tick, long_tick = mm(1.5), mm(3.0)
    k = 1
    while k * 5 < width_mm - 1:
        x = mm(k * 5)
        length = long_tick if k % 2 == 0 else short_tick
        out.append("BAR {},{},{},{}\r\n".format(x, by0, line, length).encode("ascii"))
        k += 1
    k = 1
    while k * 5 < height_mm - 1:
        y = mm(k * 5)
        length = long_tick if k % 2 == 0 else short_tick
        out.append("BAR {},{},{},{}\r\n".format(bx0, y, length, line).encode("ascii"))
        k += 1

    # Content area: clear of the rulers on the top/left.
    cx0, cy0 = mm(5.0), mm(5.0)
    cx1, cy1 = width_dots - mm(2.0), height_dots - mm(2.0)
    cw, ch = cx1 - cx0, cy1 - cy0
    occupied = []  # rects (x0, y0, x1, y1) the crosshair must avoid

    # Polarity swatch (bottom, centered) + caption above it.
    text_bottom = cy1
    swatch_cmds: List[bytes] = []
    sw = min(mm(30.0), cw)
    sh = min(mm(12.0), ch * 45 // 100)
    if sw >= mm(12.0) and sh >= mm(5.0):
        swatch = Image.new("1", (sw, sh), 255)
        draw = ImageDraw.Draw(swatch)
        draw.rectangle([0, 0, sw // 2 - 1, sh - 1], fill=0)
        draw.rectangle([0, 0, sw - 1, sh - 1], outline=0, width=max(2, mm(0.3)))
        sx = max(cx0, (width_dots - sw) // 2)
        sy = cy1 - sh
        swatch_cmds.append(bitmap_command(sx, sy, swatch, s.bitmap_black_is_one))
        occupied.append((sx, sy, sx + sw, sy + sh))
        text_bottom = sy - mm(1.0)

        fw, fh = _DOT_FONTS["1"]
        for caption in ("LEFT HALF SHOULD BE BLACK", "LEFT=BLACK"):
            if len(caption) * fw <= cw:
                cap_x = max(cx0, (width_dots - len(caption) * fw) // 2)
                cap_y = sy - mm(1.0) - fh
                if cap_y >= cy0:
                    swatch_cmds.append(_text_cmd(cap_x, cap_y, "1", caption))
                    occupied.append((cap_x, cap_y, cap_x + len(caption) * fw, cap_y + fh))
                    text_bottom = cap_y - mm(1.0)
                break

    # Identifying text, most important first.
    text_lines = [
        "MUNBYN RW403B TEST",
        "polarity: black_is_one={}".format(int(s.bitmap_black_is_one)),
        "{:.1f}x{:.1f}mm ({:.2f}x{:.2f}in)".format(
            width_mm, height_mm, width_mm / 25.4, height_mm / 25.4
        ),
        "density={} speed={}".format(s.density, s.speed),
    ]
    text_h = max(0, text_bottom - cy0)
    chosen = None
    for font in _SELFTEST_FONTS:
        fw, fh = _DOT_FONTS[font]
        pitch = fh + max(4, fh // 4)
        widest = max(len(t) for t in text_lines) * fw
        if widest <= cw and (text_h + (pitch - fh)) // pitch >= len(text_lines):
            chosen = (font, fw, fh, pitch, text_lines)
            break
    if chosen is None:
        fw, fh = _DOT_FONTS["1"]
        pitch = fh + 4
        n = (text_h + 4) // pitch
        max_chars = cw // fw
        shown = [t[:max_chars] for t in text_lines[:n]] if max_chars > 0 else []
        chosen = ("1", fw, fh, pitch, shown)
    font, fw, fh, pitch, shown = chosen
    for i, content in enumerate(shown):
        y = cy0 + i * pitch
        out.append(_text_cmd(cx0, y, font, content))
    if shown:
        widest = max(len(t) for t in shown) * fw
        occupied.append((cx0, cy0, cx0 + widest, cy0 + (len(shown) - 1) * pitch + fh))

    # Center crosshair, unless it would collide with text or the swatch.
    mx, my = width_dots // 2, height_dots // 2
    arm = min(mm(5.0), cw // 4, ch // 4)
    if arm >= mm(1.5):
        cross = (mx - arm, my - arm, mx + arm + line, my + arm + line)
        if not any(_rects_overlap(cross, r) for r in occupied):
            out.append("BAR {},{},{},{}\r\n".format(mx - arm, my, 2 * arm + line, line).encode("ascii"))
            out.append("BAR {},{},{},{}\r\n".format(mx, my - arm, line, 2 * arm + line).encode("ascii"))

    out.extend(swatch_cmds)
    out.append(b"PRINT 1,1\r\n")
    return b"".join(out)


def calibrate_job() -> bytes:
    """Gap auto-detect: feeds a few labels while the sensor learns spacing.

    Uses ``GAPDETECT`` (gap-sensor specific) since ``gap`` is our default
    media type. For black-mark stock, ``BLINEDETECT`` would be the
    equivalent, and ``AUTODETECT`` lets the printer pick the sensor itself
    (manual: don't also send GAP/BLINE when using AUTODETECT).
    """
    return b"GAPDETECT\r\n"


def feed_job() -> bytes:
    """Feed one label."""
    return b"FORMFEED\r\n"


#: Status Polling command (<ESC>!?), works over RS-232/USB/Ethernet per the
#: TSC manual. Returns exactly one status byte.
STATUS_QUERY = b"\x1b!?"

_STATUS_BITS = (
    (0x01, "head_open"),
    (0x02, "paper_jam"),
    (0x04, "out_of_paper"),
    (0x08, "out_of_ribbon"),
    (0x10, "pause"),
    (0x20, "printing"),
    (0x80, "other_error"),
)


def decode_status(b: int) -> List[str]:
    """Decode a single TSPL status byte into human-readable flags."""
    if b == 0:
        return ["ready"]
    return [name for mask, name in _STATUS_BITS if b & mask]


_BITMAP_RE = re.compile(rb"BITMAP (\d+),(\d+),(\d+),(\d+),(\d+),")


def _split_commands(job: bytes):
    """Yield ``(line, payload)`` per command; payload is BITMAP data or None."""
    pos = 0
    n = len(job)
    while pos < n:
        if job[pos : pos + 7] == b"BITMAP ":
            m = _BITMAP_RE.match(job, pos)
            if m:
                payload_len = int(m.group(3)) * int(m.group(4))
                data_start = m.end()
                yield job[pos:data_start], job[data_start : data_start + payload_len]
                pos = data_start + payload_len
                if job[pos : pos + 2] == b"\r\n":
                    pos += 2
                continue
        nl = job.find(b"\r\n", pos)
        if nl == -1:
            yield job[pos:], None
            pos = n
        else:
            yield job[pos:nl], None
            pos = nl + 2


def describe(job: bytes, preview_bytes: int = 32) -> str:
    """Human-readable rendering of a job: ASCII lines verbatim, BITMAP
    payloads summarized (never dumped in full)."""
    out_lines: List[str] = []
    for line, payload in _split_commands(job):
        text = line.decode("ascii", "replace")
        if payload is None:
            out_lines.append(text)
        else:
            preview = payload[:preview_bytes]
            out_lines.append(
                "{}<{} bytes of bitmap data: first {} bytes hex {}>".format(
                    text, len(payload), len(preview), preview.hex()
                )
            )
    out_lines.append("-- total {} bytes --".format(len(job)))
    return "\n".join(out_lines)


def _font(size_px: int):
    try:
        return ImageFont.load_default(size=size_px)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def simulate(
    job: bytes, width_dots: int, height_dots: int, black_is_one: Optional[bool] = None
) -> List[Image.Image]:
    """Approximate what the printer would print: one mode "1" image per PRINT.

    Handles CLS, BOX, BAR, TEXT (drawn with a stand-in font on the TSPL
    font's cell grid, so layout is right even though glyphs differ) and
    BITMAP (decoded with ``black_is_one``, default: ``JobSettings``'s
    default polarity). Anything else is ignored. Used for ``--preview`` of the self-test and to check jobs.
    """
    if black_is_one is None:
        black_is_one = JobSettings.bitmap_black_is_one
    canvas = Image.new("1", (max(1, width_dots), max(1, height_dots)), 255)
    draw = ImageDraw.Draw(canvas)
    pages: List[Image.Image] = []
    text_re = re.compile(r'TEXT (\d+),(\d+),"([^"]*)",(\d+),(\d+),(\d+),"(.*)"$')
    for line, payload in _split_commands(job):
        if payload is not None:
            m = _BITMAP_RE.match(line)
            x, y, wb, h = (int(m.group(i)) for i in range(1, 5))
            if wb and h:
                # Pillow reads a set bit as 255; turn that into a "print a
                # dot here" mask according to the polarity being simulated.
                bits = Image.frombytes("1", (wb * 8, h), payload, "raw", "1").convert("L")
                black_mask = bits if black_is_one else ImageOps.invert(bits)
                canvas.paste(0, (x, y), black_mask)
            continue
        text = line.decode("ascii", "replace").strip()
        if text == "CLS":
            draw.rectangle([0, 0, canvas.width, canvas.height], fill=255)
        elif text.startswith("BOX "):
            v = [int(p) for p in text[4:].split(",")[:5]]
            draw.rectangle([v[0], v[1], v[2], v[3]], outline=0, width=max(1, v[4]))
        elif text.startswith("BAR "):
            x, y, w, h = (int(p) for p in text[4:].split(",")[:4])
            if w > 0 and h > 0:
                draw.rectangle([x, y, x + w - 1, y + h - 1], fill=0)
        elif text.startswith("TEXT "):
            m = text_re.match(text)
            if m:
                x, y = int(m.group(1)), int(m.group(2))
                fw, fh = _DOT_FONTS.get(m.group(3), (8, 12))
                xm, ym = max(1, int(m.group(5))), max(1, int(m.group(6)))
                fnt = _font(int(fh * ym * 0.9))
                for i, ch in enumerate(m.group(7)):
                    draw.text((x + i * fw * xm, y), ch, fill=0, font=fnt)
        elif text.startswith("PRINT"):
            pages.append(canvas.copy())
    return pages


def hexdump(data: bytes, limit: Optional[int] = None) -> str:
    """Classic offset/hex/ascii dump, 16 bytes per line."""
    if limit is not None:
        data = data[:limit]
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join("{:02x}".format(b) for b in chunk)
        hex_part = hex_part.ljust(16 * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("{:08x}  {}  |{}|".format(i, hex_part, ascii_part))
    return "\n".join(lines)
