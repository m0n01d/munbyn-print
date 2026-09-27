"""Label size math: mm/dot conversion, stock-size presets, and size parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict

from PIL import Image

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


# ---------------------------------------------------------------------------
# feed_scale: this printer is mechanically short along the paper feed (an
# 800-row bar measured 98.1mm on paper, not 100mm -- caliper-measured
# 2026-09-27, mechanical rather than a math error since Munbyn's own phone
# app shows the same kind of shortfall). feed_scale = printed length /
# intended length; stretching a job's bitmap height and its SIZE length by
# 1/feed_scale before sending compensates for it. See CLAUDE.md/PLANS/PLAN.md
# for the hardware verification and munbyn.config.DEFAULTS for this
# printer's measured value (0.981).
# ---------------------------------------------------------------------------

#: Sane band around 1.0 for feed_scale -- wide enough for real mechanical
#: variation between units, narrow enough to catch a fat-fingered/garbage
#: value (e.g. a stray percentage like 98) before it turns into a wildly
#: wrong bitmap height.
FEED_SCALE_MIN = 0.9
FEED_SCALE_MAX = 1.1


def validate_feed_scale(feed_scale: float) -> None:
    """Raise ``ValueError`` if ``feed_scale`` is outside the sane
    ``[FEED_SCALE_MIN, FEED_SCALE_MAX]`` band. ``1.0`` (the correction
    disabled) is always valid."""
    if feed_scale == 1.0:
        return
    if not FEED_SCALE_MIN <= feed_scale <= FEED_SCALE_MAX:
        raise ValueError(
            "feed_scale must be between {} and {} (1.0 disables the feed "
            "correction), not {!r}".format(FEED_SCALE_MIN, FEED_SCALE_MAX, feed_scale)
        )


def stretched_height_dots(height_dots: int, feed_scale: float) -> int:
    """Bitmap rows needed along the feed axis so the printed label comes out
    at its intended physical height, given this printer's ``feed_scale``
    (measured printed-length / intended-length along the paper feed). The
    stretch factor applied is ``1 / feed_scale``.

    ``feed_scale == 1.0`` is an exact no-op (returns ``height_dots``
    unchanged) -- this is what keeps every job path byte-identical to
    pre-feed-scale behaviour when the correction is disabled.
    """
    if feed_scale == 1.0:
        return height_dots
    return round(height_dots / feed_scale)


def apply_feed_scale(img: "Image.Image", feed_scale: float) -> "Image.Image":
    """Stretch a grayscale (or any-mode) image's height by ``1/feed_scale``
    with LANCZOS resampling, leaving width untouched. Meant to be called
    while an image is still grayscale, before dithering/thresholding to 1-bit
    (see ``munbyn.render.render_image`` and ``munbyn.tspl.selftest_image``).

    ``feed_scale == 1.0`` returns ``img`` unchanged (the very same object,
    not merely resized to the same size) -- the no-op path that keeps output
    byte-identical to pre-feed-scale behaviour. A non-1.0 value is validated
    with ``validate_feed_scale`` first, so a bad value raises here rather
    than silently producing a wildly wrong bitmap.
    """
    if feed_scale == 1.0:
        return img
    validate_feed_scale(feed_scale)
    new_height = stretched_height_dots(img.height, feed_scale)
    if new_height == img.height:
        return img
    return img.resize((img.width, new_height), Image.Resampling.LANCZOS)
