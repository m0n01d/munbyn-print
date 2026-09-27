"""Label size math: mm/dot conversion, stock-size presets, and size parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict

#: Printer resolution in dots per inch (verified: RW403B is a fixed 203 dpi head).
DPI = 203

#: Max printable media width per the vendor PPD (4.25in). Anything wider is rejected.
MAX_WIDTH_MM = 108.0

#: Sane upper bound on label height (the vendor PPD's continuous-media max is
#: 1440pt = ~508mm; this is a generous multiple of that, not a hardware spec,
#: to catch fat-fingered/garbage input (e.g. a blank web form field) before it
#: turns into a multi-hundred-megabyte bitmap). Anything taller is rejected.
MAX_HEIGHT_MM = 1000.0


def mm_to_dots(mm: float) -> int:
    """Convert a millimeter measurement to printer dots at ``DPI`` resolution."""
    return round(mm / 25.4 * DPI)


@dataclass(frozen=True)
class LabelSize:
    """A physical label size in millimeters, with an optional preset name."""

    width_mm: float
    height_mm: float
    name: str = ""

    @property
    def width_dots(self) -> int:
        return mm_to_dots(self.width_mm)

    @property
    def height_dots(self) -> int:
        return mm_to_dots(self.height_mm)


def _in(width_in: float, height_in: float, name: str) -> LabelSize:
    return LabelSize(width_in * 25.4, height_in * 25.4, name)


#: Every stock size listed in Munbyn's own macOS PPD, inch-based (name -> LabelSize).
PRESETS: Dict[str, LabelSize] = {
    name: _in(w, h, name)
    for name, (w, h) in {
        "1.60x1.20": (1.60, 1.20),
        "1.96x1.20": (1.96, 1.20),
        "1.96x1.96": (1.96, 1.96),
        "2x1": (2.0, 1.0),
        "2x2": (2.0, 2.0),
        "2.25x1.25": (2.25, 1.25),
        "2.25x2.25": (2.25, 2.25),
        "2.30x2.30": (2.30, 2.30),
        "2.5x1.5": (2.5, 1.5),
        "3x2": (3.0, 2.0),
        "3x3": (3.0, 3.0),
        "3x5": (3.0, 5.0),
        "4x6": (4.0, 6.0),
    }.items()
}

_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*x\s*([0-9]*\.?[0-9]+)\s*(mm|in)?$")


def parse_size(text: str) -> LabelSize:
    """Parse a label size string.

    Accepts a preset name ("4x6"), an explicit unit ("4x6in", "100x150mm"),
    or spaced forms ("4 x 6"). Unitless numbers are inches. Raises
    ``ValueError`` on anything unparsable, or a width over 108mm (4.25in),
    the printer's max media width.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Cannot parse label size: {text!r}")
    normalized = text.strip().lower()

    preset = PRESETS.get(normalized)
    if preset is not None:
        return preset

    match = _SIZE_RE.match(normalized)
    if not match:
        raise ValueError(f"Cannot parse label size: {text!r}")

    width = float(match.group(1))
    height = float(match.group(2))
    unit = match.group(3) or "in"
    if unit == "mm":
        width_mm, height_mm = width, height
    else:
        width_mm, height_mm = width * 25.4, height * 25.4

    if width_mm <= 0 or height_mm <= 0:
        raise ValueError(f"Label width and height must be positive: {text!r}")
    if width_mm > MAX_WIDTH_MM + 1e-6:
        raise ValueError(
            f"Label width {width_mm:.2f}mm exceeds the printer's max media "
            f"width of {MAX_WIDTH_MM:.2f}mm (4.25in): {text!r}"
        )
    if height_mm > MAX_HEIGHT_MM:
        raise ValueError(
            f"Label height {height_mm:.2f}mm exceeds the sane maximum of "
            f"{MAX_HEIGHT_MM:.2f}mm: {text!r}"
        )
    return LabelSize(width_mm, height_mm)
