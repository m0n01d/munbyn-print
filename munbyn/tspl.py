"""Build TSPL (TSC Printer Language) jobs for the Munbyn RW403B.

The printer's own IEEE-1284 device ID reports ``CMD:TSPL`` (confirmed on
hardware) -- this is the older "TSPL" dialect, not "TSPL2". No native TSPL
font (fixed dot-matrix or the TSPL2-only scalable "0"/ROMAN.TTF) is emitted
by this module at all any more: every job builder here draws all text as
pixels with Pillow and ships it as one ``BITMAP`` (see ``selftest_image``),
since native ``TEXT`` was verified on hardware to print nothing -- see
below.

**Verified on hardware (2026-09-27, real RW403B, real 4x6 gap labels):** a
job built from exactly ``SUPPORTED_COMMANDS`` below -- header, ``CLS``, one
``BITMAP`` (mode 1), ``PRINT`` -- printed correctly with the right polarity.
A second job using the *same* header plus native ``TEXT``/``BOX``/``BAR``
commands printed **nothing at all** (no feed). Conclusion: this firmware's
USB path only implements the vendor filter's own command subset; anything
outside it is silently dropped, not merely unsupported-but-harmless. Every
job builder in this module is therefore restricted to
``SUPPORTED_COMMANDS``, and ``GAPDETECT`` (tested alone: also ignored) is
never sent either -- see ``calibrate_instructions()``.

Two details in this module are configurable specifically because research
could not pin them down with certainty for THIS printer/firmware, and are
meant to be confirmed from a real test print rather than trusted blindly:

* ``JobSettings.bitmap_black_is_one`` -- bit polarity of ``BITMAP`` data.
  The default is ``False`` (a CLEAR bit prints a black dot, a set bit is
  white) -- **confirmed on hardware** 2026-09-27 against a real 4x6 label.
* The vendor's own (broken, x86-only) CUPS filter's extracted strings show
  every ``SIZE``/``GAP``/``BLINE``/``OFFSET`` line formatted with a plain
  ``%d`` (integer mm), not decimals -- even though the TSC manual's own
  examples use decimal mm elsewhere. ``header()`` follows the vendor's own
  template literally (round to the nearest whole mm), and is byte-identical
  to the verified working job for the default settings.

``BITMAP`` always uses mode 1 (OR), the raw-bitmap template in the vendor
filter. The vendor's compressed mode 3 (``BITMAP x,y,wb,h,3,len,<data>``,
links libz) and ``SETC PAUSEKEY OFF`` both exist in the vendor filter binary
but are untested here and are never used.
"""
from __future__ import annotations

import datetime
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


#: The complete set of TSPL commands this firmware's USB path was verified to
#: honour (2026-09-27, real hardware). Everything this module builds
#: (``build_job``, ``selftest_job``, ``feed_job``) is restricted to exactly
#: this set -- notably no ``TEXT``/``BOX``/``BAR`` (verified to print
#: nothing, not even a feed) and no ``GAPDETECT`` (verified to be ignored).
#: ``SETC AUTODOTTED OFF`` is listed as a full line since ``SETC`` alone
#: covers other, untested sub-commands (e.g. ``SETC PAUSEKEY OFF``) that must
#: not be sent.
SUPPORTED_COMMANDS: Tuple[str, ...] = (
    "SIZE",
    "GAP",
    "BLINE",
    "REFERENCE",
    "OFFSET",
    "SETC AUTODOTTED OFF",
    "DENSITY",
    "SPEED",
    "DIRECTION",
    "CLS",
    "BITMAP",
    "PRINT",
)


def header(s: JobSettings) -> bytes:
    """Build the SIZE..DIRECTION preamble, mirroring the vendor's job sequence.

    Byte-identical (for default settings) to the job verified on real
    hardware 2026-09-27 -- do not add ``SETC PAUSEKEY OFF`` or anything else
    here without a fresh hardware verification.
    """
    if s.media not in ("gap", "bline", "continuous"):
        raise ValueError("media must be gap, bline or continuous, not {!r}".format(s.media))
    if not 0 <= s.density <= 15:
        raise ValueError("density must be 0..15, not {!r}".format(s.density))
    if not 1 <= s.speed <= 8:
        raise ValueError("speed must be 1..8, not {!r}".format(s.speed))
    if s.direction not in (0, 1):
        raise ValueError("direction must be 0 or 1, not {!r}".format(s.direction))
    if s.gap_mm < 0 or s.gap_offset_mm < 0 or s.offset_mm < 0:
        raise ValueError("gap, gap offset and offset must be >= 0")
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


# Fixed-pitch TSPL dot fonts: name -> (cell width, cell height) in dots. Kept
# only so ``simulate()`` can approximate a foreign job's native TEXT command
# (e.g. one hand-built in a test); nothing in this module emits TEXT anymore
# -- see the module docstring.
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


def _rects_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _font(size_px: int):
    """Load a legible TrueType font at ``size_px`` pixels.

    Pillow >= 10.1 ships a scalable default font that
    ``ImageFont.load_default(size=...)`` can render at any size; older
    Pillow raises ``TypeError`` on the ``size`` kwarg, so fall back to
    macOS's own Helvetica, and to the bare (small, fixed-size) default as a
    last resort so this never raises.
    """
    try:
        return ImageFont.load_default(size=max(1, int(size_px)))
    except TypeError:
        pass
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", max(1, int(size_px)))
    except Exception:
        return ImageFont.load_default()


def _text_wh(draw: "ImageDraw.ImageDraw", text: str, font) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def selftest_image(s: JobSettings) -> Image.Image:
    """Draw the self-test label as a single plain (mode "1") image.

    This firmware silently drops every native TSPL ``TEXT``/``BOX``/``BAR``
    command (verified on hardware 2026-09-27 -- a job using them printed
    nothing at all), so the self-test's border, mm rulers, crosshair and
    identifying text are all drawn as pixels with Pillow and shipped as one
    ``BITMAP``, exactly like ``build_job`` does for a rendered page.

    Draws: a border inset 1 mm; ruler ticks every 5 mm along the top and
    left edges (10 mm ticks drawn longer); a center crosshair; identifying
    text (model, label size in mm/in, density/speed, the configured bitmap
    polarity, and today's date); and a polarity swatch captioned "LEFT HALF
    SHOULD BE BLACK". If ``bitmap_black_is_one`` doesn't match this
    firmware's real ``BITMAP`` bit convention, the *whole* label inverts
    (not just the swatch) -- an even more obvious tell than the old
    TEXT/BOX-based self-test could give. Elements that don't fit a small
    label are shrunk or skipped rather than overflowing it.
    """
    mm = mm_to_dots
    width = max(1, s.size.width_dots)
    height = max(1, s.size.height_dots)
    width_mm, height_mm = s.size.width_mm, s.size.height_mm

    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    # Draw text 1-bit (no anti-aliasing). Anti-aliased glyphs at small pixel
    # sizes leave isolated low-coverage pixels (e.g. the gap between an "i"'s
    # dot and stem) that the final mode "1" conversion's 128 threshold fuses
    # into solid ink, making small text illegible (found by review: "MUNBYN"
    # read as "MUNEYN", "density" as "densIty"). Drawing without
    # anti-aliasing keeps every glyph pixel fully black or fully white, so
    # thresholding can't fuse anything.
    draw.fontmode = "1"

    # Border.
    inset = mm(1.0)
    border_w = max(1, mm(0.3))
    bx0, by0 = inset, inset
    bx1 = max(bx0 + 1, width - 1 - inset)
    by1 = max(by0 + 1, height - 1 - inset)
    draw.rectangle([bx0, by0, bx1, by1], outline=0, width=border_w)

    # Rulers: ticks measured from the label's own left/top edge.
    tick_w = max(1, mm(0.25))
    short_tick, long_tick = mm(1.5), mm(3.0)
    k = 1
    while k * 5 < width_mm - 1:
        x = mm(k * 5.0)
        length = long_tick if k % 2 == 0 else short_tick
        draw.rectangle([x, by0, x + tick_w - 1, by0 + length - 1], fill=0)
        k += 1
    k = 1
    while k * 5 < height_mm - 1:
        y = mm(k * 5.0)
        length = long_tick if k % 2 == 0 else short_tick
        draw.rectangle([bx0, y, bx0 + length - 1, y + tick_w - 1], fill=0)
        k += 1

    # Content area: clear of the rulers on the top/left.
    cx0, cy0 = mm(5.0), mm(5.0)
    cx1 = max(cx0 + 1, width - mm(2.0))
    cy1 = max(cy0 + 1, height - mm(2.0))
    cw, ch = cx1 - cx0, cy1 - cy0
    occupied: List[Tuple[int, int, int, int]] = []  # rects the crosshair must avoid

    # Polarity swatch (bottom, centered) + caption above it.
    text_bottom = cy1
    sw = min(mm(30.0), cw)
    sh = min(mm(12.0), (ch * 45) // 100)
    if sw >= mm(12.0) and sh >= mm(5.0):
        sx = max(cx0, (width - sw) // 2)
        sy = cy1 - sh
        draw.rectangle([sx, sy, sx + sw - 1, sy + sh - 1], fill=255)
        draw.rectangle([sx, sy, sx + sw // 2 - 1, sy + sh - 1], fill=0)
        draw.rectangle([sx, sy, sx + sw - 1, sy + sh - 1], outline=0, width=max(2, mm(0.3)))
        occupied.append((sx, sy, sx + sw, sy + sh))
        text_bottom = sy - mm(1.0)

        for caption in ("LEFT HALF SHOULD BE BLACK", "LEFT=BLACK"):
            font_px = max(6, mm(2.2))
            font = _font(font_px)
            tw, th = _text_wh(draw, caption, font)
            while tw > cw and font_px > 6:
                font_px -= 1
                font = _font(font_px)
                tw, th = _text_wh(draw, caption, font)
            if tw <= cw:
                cap_x = max(cx0, (width - tw) // 2)
                cap_y = sy - mm(1.0) - th
                if cap_y >= cy0:
                    draw.text((cap_x, cap_y), caption, fill=0, font=font)
                    occupied.append((cap_x, cap_y, cap_x + tw, cap_y + th))
                    text_bottom = cap_y - mm(1.0)
                break

    # Identifying text, most important first.
    text_lines = [
        "MUNBYN RW403B TEST",
        "{:.1f}x{:.1f}mm ({:.2f}x{:.2f}in)".format(
            width_mm, height_mm, width_mm / 25.4, height_mm / 25.4
        ),
        "density={} speed={}".format(s.density, s.speed),
        "polarity: black_is_one={}".format(int(s.bitmap_black_is_one)),
        datetime.date.today().isoformat(),
    ]
    text_h = max(0, text_bottom - cy0)
    font_px = max(6, mm(3.0))
    font = _font(font_px)
    while font_px > 6:
        widest = max(_text_wh(draw, t, font)[0] for t in text_lines)
        _, lh = _text_wh(draw, "Xgy", font)
        pitch = lh + max(2, lh // 4)
        if widest <= cw and pitch * len(text_lines) <= text_h:
            break
        font_px -= 1
        font = _font(font_px)
    _, lh = _text_wh(draw, "Xgy", font)
    pitch = lh + max(2, lh // 4)
    max_lines = (text_h // pitch) if pitch > 0 else 0
    shown = text_lines[: max(0, min(len(text_lines), max_lines))]
    for i, content in enumerate(shown):
        y = cy0 + i * pitch
        draw.text((cx0, y), content, fill=0, font=font)
    if shown:
        widest_shown = max(_text_wh(draw, t, font)[0] for t in shown)
        occupied.append((cx0, cy0, cx0 + widest_shown, cy0 + (len(shown) - 1) * pitch + lh))

    # Center crosshair, unless it would collide with text or the swatch.
    mx, my = width // 2, height // 2
    arm = min(mm(5.0), cw // 4, ch // 4)
    if arm >= mm(1.5):
        cross_w = max(1, mm(0.3))
        cross = (mx - arm, my - arm, mx + arm + cross_w, my + arm + cross_w)
        if not any(_rects_overlap(cross, r) for r in occupied):
            draw.rectangle([mx - arm, my, mx + arm + cross_w - 1, my + cross_w - 1], fill=0)
            draw.rectangle([mx, my - arm, mx + cross_w - 1, my + arm + cross_w - 1], fill=0)

    return img.convert("1", dither=Image.Dither.NONE)


def selftest_job(s: JobSettings) -> bytes:
    """Build the built-in alignment/polarity self-test as a pure BITMAP job:
    header, ``CLS``, one ``BITMAP`` for the whole label (see
    ``selftest_image``), ``PRINT``. Only commands in ``SUPPORTED_COMMANDS``
    are ever emitted.

    Honours ``x_shift_mm``/``y_shift_mm`` (like ``build_job``, via
    ``_shift_and_bitmap``) and ``copies``, so nudging alignment against the
    self-test and then ``--save-defaults`` actually changes what gets
    printed -- see the README's calibration steps. ``--preview``/the web
    UI's self-test preview shows ``selftest_image()`` directly, which is
    unshifted (the full label canvas), the same way a rendered page's own
    preview doesn't reflect its shift either.
    """
    img = selftest_image(s)
    return (
        header(s)
        + b"CLS\r\n"
        + _shift_and_bitmap(s, img)
        + "PRINT 1,{}\r\n".format(s.copies).encode("ascii")
    )


#: Manual calibration procedure (RW403B manual, item 4). There is no TSPL
#: command this firmware honours for calibration: ``GAPDETECT`` alone was
#: sent to real hardware 2026-09-27 and verified to do nothing at all.
#: ``calibrate_instructions()`` never builds or sends a job.
CALIBRATION_INSTRUCTIONS = """\
Nothing is sent to the printer for calibration -- it's a manual, physical
procedure. (TSPL's GAPDETECT was tried alone on real hardware and verified
to do nothing; this firmware's USB path does not implement it.)

To calibrate label gap/length detection:
  1. Load at least 4 labels into the printer.
  2. Close the cover -- this triggers automatic label identification.
  3. If that doesn't work, hold the FEED button until the printer beeps
     ONCE (label identification).

Feed-button reference (RW403B manual):
  * Single click                        -- feed one label
  * Hold to ONE beep                    -- label identification (calibrate)
  * Double-click, or hold to TWO beeps  -- printer self-test page
  * Hold to THREE beeps (~6s)           -- reset

LED reference:
  * green               -- ready
  * blue                -- Bluetooth connected
  * red                 -- label not identified, or cover open
  * flashing green+red  -- print head overheated
"""


def calibrate_instructions() -> str:
    """Return the manual gap/label calibration procedure. Never touches
    the printer -- see ``CALIBRATION_INSTRUCTIONS``."""
    return CALIBRATION_INSTRUCTIONS


def feed_job(s: JobSettings) -> bytes:
    """Feed one label by printing a blank one: header + ``CLS`` + ``PRINT
    1,1``. There is no dedicated feed command in this firmware's verified
    command subset (``FORMFEED`` is not in ``SUPPORTED_COMMANDS`` and has
    not been tested against real hardware, so it is never sent)."""
    return header(s) + b"CLS\r\n" + b"PRINT 1,1\r\n"


#: Status Polling command (<ESC>!?), works over RS-232/USB/Ethernet per the
#: TSC manual. Returns exactly one status byte. **Verified on hardware
#: 2026-09-27: this firmware does not reply** (no bytes on bulk IN, 500ms
#: timeout). It is 3 bytes with no CR/LF terminator, and it is NOT in
#: ``SUPPORTED_COMMANDS`` -- whether those unterminated bytes are safe to
#: send right before the next job's ``SIZE`` line has never been verified on
#: hardware, so nothing in this repo sends it automatically any more: the
#: web UI never sends it, and the CLI only does with an explicit
#: ``--status --probe`` (see ``Printer.query_status`` / ``CLAUDE.md``).
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


def simulate(
    job: bytes, width_dots: int, height_dots: int, black_is_one: Optional[bool] = None
) -> List[Image.Image]:
    """Approximate what the printer would print: one mode "1" image per PRINT.

    Handles CLS, BOX, BAR, TEXT (drawn with a stand-in font on the TSPL
    font's cell grid, so layout is right even though glyphs differ) and
    BITMAP (decoded with ``black_is_one``, default: ``JobSettings``'s
    default polarity). Anything else is ignored. BOX/BAR/TEXT decoding is
    kept for generality (this can decode any TSPL job, not just ones this
    module builds) even though nothing here emits them anymore -- see the
    module docstring. Used to check jobs and for direct pixel comparisons in
    tests; ``--preview``/the web UI use ``selftest_image()`` directly for an
    exact (not approximated) self-test preview.
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
