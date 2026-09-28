"""Parse the TSPL subset this repo emits back into page images.

Used by the Bluetooth bridge (``munbyn.ble_bridge``): the CUPS queue
"Munbyn RW403B (Bluetooth)" and ``print_label.py --ble`` both hand the bridge
an ordinary TSPL job, and the bridge turns it into Bluetooth pages. Pure: no
I/O, no Bluetooth.

Accepted commands are exactly ``munbyn.tspl.SUPPORTED_COMMANDS`` -- ``SIZE``,
``GAP``, ``BLINE``, ``REFERENCE``, ``OFFSET``, ``SETC AUTODOTTED OFF``,
``DENSITY``, ``SPEED``, ``DIRECTION``, ``CLS``, ``BITMAP x,y,wb,h,1,<data>``
and ``PRINT m[,n]`` -- which is everything ``cups/rastertotspl`` and
``munbyn.tspl`` produce. Anything else (another ``BITMAP`` mode, ``TEXT``,
``SETC PAUSEKEY``, a status query ...) raises ``TsplParseError``: the USB
firmware silently drops jobs like that, so a job carrying one was not built
by this repo and printing a guess of it would hide the problem.

Semantics follow the printer's image buffer: ``BITMAP`` (mode 1 = OR) adds
black dots to the buffer, ``PRINT m,n`` prints the buffer ``m*n`` times, and
``CLS`` clears it (the buffer survives ``PRINT`` until the next ``CLS``).
In TSPL ``BITMAP`` data a **clear bit is a black dot** (hardware-verified
2026-09-27; ``munbyn.tspl.JobSettings.bitmap_black_is_one`` = False and the
CUPS filter both use it). The returned images are Pillow mode "1" (0 = black),
so ``munbyn.ble_protocol.pack_page`` turns them into Bluetooth's 1 = black.

A page is as big as the bitmaps placed on it: from the origin to the
furthest bitmap edge (a job from this repo has one full-page
``BITMAP 0,0,...`` per page, so the page is exactly that bitmap). Rows are
NOT rescaled: whoever built the job already stretched them by
``1/feed_scale``. ``SIZE``/``GAP``/``DENSITY``/``SPEED``/... are parsed and
returned for logging; Bluetooth doesn't send them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageOps

#: Dots per mm at 203 dpi (``SIZE`` in mm -> dots for an empty page).
DOTS_PER_MM = 203 / 25.4
#: Widest page Bluetooth takes (``munbyn.ble_protocol.MAX_WIDTH_DOTS``).
MAX_WIDTH_DOTS = 880
#: Refuse absurd jobs before allocating them.
MAX_ROWS = 16000  # ~2 m of label at 203 dpi
MAX_COPIES = 1000
MAX_PAGES = 1000

_BITMAP_RE = re.compile(
    rb"BITMAP[ \t]+(-?\d+)[ \t]*,[ \t]*(-?\d+)[ \t]*,[ \t]*(\d+)[ \t]*,[ \t]*(\d+)[ \t]*,[ \t]*(\d+)[ \t]*,",
    re.IGNORECASE,
)
_HEADER_COMMANDS = ("SIZE", "GAP", "BLINE", "REFERENCE", "OFFSET", "DENSITY", "SPEED", "DIRECTION")


class TsplParseError(ValueError):
    """The job isn't one this repo could have built (or is truncated)."""


@dataclass
class ParsedPage:
    image: Image.Image  # mode "1", 0 = black, rows as sent (already feed-corrected)
    copies: int
    #: The header commands in effect for this page, e.g. {"SIZE": "102 mm,155 mm", "DENSITY": "12"}.
    header: Dict[str, str] = field(default_factory=dict)

    @property
    def size_mm(self) -> Optional[Tuple[float, float]]:
        return parse_size_mm(self.header.get("SIZE", ""))


@dataclass
class ParsedJob:
    pages: List[ParsedPage]
    #: Every header command seen, last value wins (for logging).
    header: Dict[str, str]
    notes: List[str] = field(default_factory=list)

    @property
    def total_labels(self) -> int:
        return sum(p.copies for p in self.pages)


def parse_size_mm(text: str) -> Optional[Tuple[float, float]]:
    """``"102 mm,155 mm"`` -> (102.0, 155.0); ``"4,6"`` (inches) -> (101.6, 152.4);
    ``"812 dot,1242 dot"`` -> mm. ``None`` if it doesn't parse."""
    parts = [p.strip().lower() for p in text.split(",")]
    if len(parts) != 2:
        return None
    out = []
    for p in parts:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(mm|dot|dots)?", p)
        if not m:
            return None
        v = float(m.group(1))
        unit = m.group(2)
        if unit == "mm":
            out.append(v)
        elif unit in ("dot", "dots"):
            out.append(v / DOTS_PER_MM)
        else:
            out.append(v * 25.4)
    return out[0], out[1]


def _parse_print(args: str, lineno: int) -> int:
    parts = [p.strip() for p in args.split(",")] if args.strip() else []
    if not 1 <= len(parts) <= 2 or not all(p.isdigit() for p in parts):
        raise TsplParseError("line {}: PRINT needs 'PRINT m[,n]', got 'PRINT {}'".format(lineno, args.strip()))
    m = int(parts[0])
    n = int(parts[1]) if len(parts) == 2 else 1
    if m < 1 or n < 1:
        raise TsplParseError("line {}: PRINT {} prints nothing".format(lineno, args.strip()))
    total = m * n
    if total > MAX_COPIES:
        raise TsplParseError("line {}: PRINT {} asks for {} labels (limit {})".format(
            lineno, args.strip(), total, MAX_COPIES))
    return total


def _compose(placed: List[Tuple[int, int, Image.Image]], header: Dict[str, str], notes: List[str]) -> Image.Image:
    if not placed:
        size = parse_size_mm(header.get("SIZE", ""))
        if size is None:
            raise TsplParseError("PRINT with nothing on the page and no usable SIZE to feed a blank label")
        w = min(MAX_WIDTH_DOTS, max(8, int(round(size[0] * DOTS_PER_MM))))
        h = max(1, int(round(size[1] * DOTS_PER_MM)))
        notes.append("blank page ({}x{} dots from SIZE {})".format(w, h, header.get("SIZE")))
        return Image.new("1", (w, h), 255)
    if len(placed) == 1 and placed[0][0] == 0 and placed[0][1] == 0 and placed[0][2].width <= MAX_WIDTH_DOTS:
        return placed[0][2].copy()
    width = max(x + img.width for x, _y, img in placed)
    height = max(y + img.height for _x, y, img in placed)
    if width > MAX_WIDTH_DOTS:
        notes.append("page is {} dots wide; clipped to {} (the Bluetooth maximum)".format(width, MAX_WIDTH_DOTS))
        width = MAX_WIDTH_DOTS
    canvas = Image.new("1", (width, height), 255)
    for x, y, img in placed:
        black = ImageOps.invert(img.convert("L"))  # 255 where the bitmap has a black dot
        canvas.paste(0, (x, y), black)  # mode 1 = OR: black dots accumulate, white never erases
    return canvas


def parse_job(data: bytes, *, black_is_one: bool = False) -> ParsedJob:
    """Parse a whole TSPL job. Raises ``TsplParseError`` on anything outside
    the supported subset, a truncated ``BITMAP``, or a job with no ``PRINT``.
    ``black_is_one`` flips the ``BITMAP`` polarity (default: clear bit = black,
    as every job this repo builds)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    data = bytes(data)
    pos, n, lineno = 0, len(data), 0
    header: Dict[str, str] = {}
    seen_header: Dict[str, str] = {}
    placed: List[Tuple[int, int, Image.Image]] = []
    pages: List[ParsedPage] = []
    notes: List[str] = []
    unprinted = False  # a BITMAP arrived after the last PRINT
    while pos < n:
        # Skip blank lines / stray line-end bytes between commands.
        while pos < n and data[pos] in b"\r\n \t\x00":
            if data[pos] == 0x0A:
                lineno += 1
            pos += 1
        if pos >= n:
            break
        lineno += 1
        if data[pos : pos + 6].upper() == b"BITMAP":
            m = _BITMAP_RE.match(data, pos)
            if not m:
                end = data.find(b"\n", pos)
                raise TsplParseError("line {}: malformed BITMAP header {!r}".format(
                    lineno, data[pos : (end if end != -1 else pos + 40)][:40]))
            x, y, wb, h, mode = (int(g) for g in m.groups())
            if mode != 1:
                raise TsplParseError("line {}: BITMAP mode {} is not supported (only mode 1)".format(lineno, mode))
            if x < 0 or y < 0:
                raise TsplParseError("line {}: BITMAP at negative position {},{}".format(lineno, x, y))
            if h > MAX_ROWS or y + h > MAX_ROWS:
                raise TsplParseError("line {}: BITMAP is {} rows tall (limit {})".format(lineno, y + h, MAX_ROWS))
            if x >= MAX_WIDTH_DOTS or wb * 8 > 4 * MAX_WIDTH_DOTS:
                raise TsplParseError("line {}: BITMAP {} bytes wide at x={} is wider than any label".format(
                    lineno, wb, x))
            start = m.end()
            end = start + wb * h
            if end > n:
                raise TsplParseError("line {}: BITMAP {}x{} needs {} bytes of data, only {} left (truncated job?)"
                                     .format(lineno, wb, h, wb * h, n - start))
            if wb and h:
                img = Image.frombytes("1", (wb * 8, h), data[start:end], "raw", "1")  # set bit = white
                if black_is_one:
                    img = ImageOps.invert(img.convert("L")).convert("1")
                if x + img.width > MAX_WIDTH_DOTS:
                    img = img.crop((0, 0, MAX_WIDTH_DOTS - x, img.height))
                    notes.append("line {}: BITMAP clipped at {} dots wide".format(lineno, MAX_WIDTH_DOTS))
                placed.append((x, y, img))
                unprinted = True
            else:
                notes.append("line {}: empty BITMAP ({}x{}) ignored".format(lineno, wb, h))
            pos = end
            continue
        nl = data.find(b"\n", pos)
        raw = data[pos : (nl if nl != -1 else n)]
        pos = nl + 1 if nl != -1 else n
        try:
            line = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise TsplParseError("line {}: not a TSPL command (binary data {!r}...)".format(lineno, raw[:16])) from None
        if not line:
            continue
        word, _, args = line.partition(" ")
        cmd = word.upper()
        if cmd in _HEADER_COMMANDS:
            header[cmd] = args.strip()
            seen_header[cmd] = args.strip()
        elif cmd == "SETC":
            if " ".join(args.upper().split()) != "AUTODOTTED OFF":
                raise TsplParseError("line {}: unsupported 'SETC {}' (only SETC AUTODOTTED OFF)".format(
                    lineno, args.strip()))
        elif cmd == "CLS":
            if args.strip():
                raise TsplParseError("line {}: CLS takes no arguments".format(lineno))
            placed = []
        elif cmd == "PRINT":
            copies = _parse_print(args, lineno)
            if len(pages) >= MAX_PAGES:
                raise TsplParseError("more than {} pages".format(MAX_PAGES))
            pages.append(ParsedPage(_compose(placed, header, notes), copies, dict(header)))
            unprinted = False
        else:
            shown = line if len(line) <= 40 else line[:40] + "..."
            raise TsplParseError(
                "line {}: unsupported TSPL command {!r} (the bridge takes only {})".format(
                    lineno, shown, ", ".join(_HEADER_COMMANDS + ("SETC AUTODOTTED OFF", "CLS", "BITMAP", "PRINT")))
            )
    if not pages:
        raise TsplParseError("no PRINT command: nothing to print" if n else "empty job")
    if unprinted:
        notes.append("bitmaps after the last PRINT were not printed")
    return ParsedJob(pages, seen_header, notes)


def group_pages(pages: List[ParsedPage]) -> List[Tuple[List[ParsedPage], int]]:
    """Group pages into Bluetooth jobs, as ``(pages, copies)`` runs -- one
    Bluetooth job (``transport.print_pages``) each.

    Consecutive 1-copy pages are merged into a single multi-page run: with
    only one copy of each, page order doesn't depend on how they're grouped,
    and one run is more efficient than many. A page asking for more than one
    copy is always its own run, even next to another page with the same copy
    count: ``ble_protocol.plan_sends`` sends a multi-page run's copies
    interleaved ("ABAB", one of each page per pass), while TSPL's per-page
    ``PRINT m,n`` prints each page's copies together ("AAABBB"). A run with
    exactly one page and N copies is sent as a single ``page = N`` write
    (``plan_sends``' 1-page case), so it reproduces TSPL's grouped order.
    """
    runs: List[Tuple[List[ParsedPage], int]] = []
    for page in pages:
        if page.copies == 1 and runs and runs[-1][1] == 1:
            runs[-1][0].append(page)
        else:
            runs.append(([page], page.copies))
    return runs
