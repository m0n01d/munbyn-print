"""Render PDFs and images into 1-bit label bitmaps ready for TSPL BITMAP commands.

Handles two families of input:

* PDFs (detected by magic bytes) are rasterised with pypdfium2. The content
  bounding box is found with a cheap low-DPI grayscale scan, then only that
  region is rendered again -- directly at the resolution the label needs --
  so text and barcodes are never upscaled from a blurry low-res raster.
* Images (anything Pillow can open, plus a macOS ``sips`` fallback for
  formats Pillow does not support, e.g. HEIC) are normalised (EXIF
  orientation, alpha compositing, grayscale) and then run through the same
  fit/rotate/align/dither pipeline as a rasterised PDF page.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Union

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from munbyn.labels import DPI, LabelSize, mm_to_dots

# Points-per-inch used by PDF canvas units; dots-per-point at native label DPI.
_PT_PER_IN = 72.0
_DOTS_PER_PT = DPI / _PT_PER_IN

# Low-resolution scan used only to locate the content bounding box on a PDF
# page. Never used as the final raster.
_BBOX_SCAN_DPI = 50.0
_BBOX_DARK_THRESHOLD = 245
_BBOX_PADDING_PT = 2.0

# Defensive cap so a pathological input (a hairline stretched to a label)
# cannot blow up memory/time.
_MAX_RASTER_DIM = 8000

_FITS = ("fit", "fill", "stretch", "actual")
_ALIGNS = ("center", "top", "top-left")

SourceType = Union[str, "Path", bytes, bytearray]


class RenderError(Exception):
    """Raised when a source file cannot be understood or rendered."""


@dataclass
class RenderOptions:
    fit: str = "fit"  # fit | fill | stretch | actual
    scale: Optional[float] = None  # percent, like Preview "Scale: N%"; 100 = physical size
    rotate: str = "auto"  # auto | 0 | 90 | 180 | 270
    crop: str = "auto"  # auto = trim white margins | none
    margin_mm: float = 0.0
    align: str = "center"  # center | top | top-left
    dither: str = "threshold"  # threshold | floyd
    threshold: int = 160
    invert: bool = False
    pages: Optional[str] = None  # "1", "1-3,5"; None = all


def parse_pages(spec: Optional[str], count: int) -> List[int]:
    """Parse a 1-based page spec ("1", "1-3,5") into 0-based indexes.

    Raises ValueError on malformed specs or indexes out of [1, count].
    """
    if spec is None:
        return list(range(count))

    spec = spec.strip()
    if not spec:
        raise ValueError("empty page spec")

    indexes: List[int] = []
    seen = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError("invalid page spec: %r" % spec)
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise ValueError("invalid page range: %r" % part)
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError("invalid page range: %r" % part)
            if start < 1 or end < 1 or start > end:
                raise ValueError("invalid page range: %r" % part)
            numbers = range(start, end + 1)
        else:
            try:
                n = int(part)
            except ValueError:
                raise ValueError("invalid page number: %r" % part)
            if n < 1:
                raise ValueError("invalid page number: %r" % part)
            numbers = (n,)
        for n in numbers:
            if n > count:
                raise ValueError(
                    "page %d out of range (file has %d page(s))" % (n, count)
                )
            idx = n - 1
            if idx not in seen:
                seen.add(idx)
                indexes.append(idx)
    return indexes


def _load_bytes(src: SourceType) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    if src == "-":
        return sys.stdin.buffer.read()
    path = Path(src)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RenderError("could not read %r: %s" % (src, exc)) from exc


def render_file(
    src: SourceType,
    size: LabelSize,
    opts: Optional[RenderOptions] = None,
    filename: Optional[str] = None,
) -> List[Image.Image]:
    """Render a PDF/image source into one 1-bit PIL image per page/frame.

    Every returned image is mode "1" and exactly
    ``size.width_dots x size.height_dots``.
    """
    if opts is None:
        opts = RenderOptions()
    _validate(opts)

    data = _load_bytes(src)
    if not data:
        raise RenderError("empty input" + (" (%s)" % filename if filename else ""))

    if b"%PDF" in data[:1024]:
        return _render_pdf(data, size, opts)
    return _render_raster(data, size, opts, filename)


# ---------------------------------------------------------------------------
# PDF path
# ---------------------------------------------------------------------------


def _render_pdf(data: bytes, size: LabelSize, opts: RenderOptions) -> List[Image.Image]:
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as exc:  # pragma: no cover - pdfium raises many types
        raise RenderError("could not open PDF: %s" % exc) from exc

    try:
        count = len(doc)
        if count == 0:
            raise RenderError("PDF has no pages")
        try:
            indexes = parse_pages(opts.pages, count)
        except ValueError as exc:
            raise RenderError(str(exc)) from exc

        results: List[Image.Image] = []
        for i in indexes:
            page = doc.get_page(i)
            try:
                raster, raster_opts = _rasterize_pdf_page(page, size, opts)
            finally:
                page.close()
            results.append(render_image(raster, size, raster_opts))
        return results
    finally:
        doc.close()


def _dark_lut():
    return [255 if i < _BBOX_DARK_THRESHOLD else 0 for i in range(256)]


def _detect_pdf_content_bbox(page, width_pt: float, height_pt: float):
    """Return (x0, y0, x1, y1) in PDF points (origin bottom-left) of the
    content, from a cheap low-DPI grayscale scan of the page as displayed
    (page /Rotate applied).

    Falls back to the full page when nothing is dark enough (a blank page)
    or when the scan itself fails for any reason.
    """
    low_scale = _BBOX_SCAN_DPI / _PT_PER_IN
    try:
        bmp = page.render(scale=low_scale, grayscale=True)
        low_img = bmp.to_pil()
    except Exception:
        return (0.0, 0.0, width_pt, height_pt)

    if low_img.mode != "L":
        low_img = low_img.convert("L")

    bbox_px = low_img.point(_dark_lut()).getbbox()
    if bbox_px is None:
        return (0.0, 0.0, width_pt, height_pt)

    left, upper, right, lower = bbox_px
    x0 = left / low_scale
    x1 = right / low_scale
    y0 = height_pt - (lower / low_scale)
    y1 = height_pt - (upper / low_scale)
    return (x0, y0, x1, y1)


def _shrink_to(lo: float, hi: float, span: float):
    """Center-shrink [lo, hi] to at most ``span`` wide."""
    if hi - lo <= span:
        return lo, hi
    mid = (lo + hi) / 2.0
    return mid - span / 2.0, mid + span / 2.0


def _rasterize_pdf_page(page, size: LabelSize, opts: RenderOptions):
    """Render one PDF page region straight at the label's final resolution.

    Returns ``(image, options_for_render_image)``. The region (content bbox
    when ``crop="auto"``) is rendered by pdfium at exactly the scale that
    maps it onto the label -- fit/fill/scale/actual are all decided here --
    so render_image() only rotates (losslessly) and places it; nothing is
    ever rasterised small and scaled up.
    """
    # get_size() is the displayed size, i.e. with the page's /Rotate applied,
    # which is also the space render()'s crop and the bbox scan work in.
    width_pt, height_pt = page.get_size()
    width_pt = max(width_pt, 1.0)
    height_pt = max(height_pt, 1.0)

    if opts.crop == "none":
        x0, y0, x1, y1 = 0.0, 0.0, width_pt, height_pt
    else:
        x0, y0, x1, y1 = _detect_pdf_content_bbox(page, width_pt, height_pt)
        x0 = max(0.0, x0 - _BBOX_PADDING_PT)
        y0 = max(0.0, y0 - _BBOX_PADDING_PT)
        x1 = min(width_pt, x1 + _BBOX_PADDING_PT)
        y1 = min(height_pt, y1 + _BBOX_PADDING_PT)
        if x1 <= x0 or y1 <= y0:
            x0, y0, x1, y1 = 0.0, 0.0, width_pt, height_pt

    box_w = x1 - x0
    box_h = y1 - y0
    avail_w, avail_h = _available_dots(size, opts)

    rotate_deg = _resolve_rotation((box_w, box_h), (size.width_dots, size.height_dots), opts.rotate)
    quarter = rotate_deg in (90, 270)
    # Content extent in label orientation, in points.
    eff_w, eff_h = (box_h, box_w) if quarter else (box_w, box_h)

    stretch = opts.scale is None and opts.fit == "stretch"
    if opts.scale is not None:
        px_per_pt = _DOTS_PER_PT * opts.scale / 100.0
    elif opts.fit == "actual":
        px_per_pt = _DOTS_PER_PT
    elif opts.fit in ("fill", "stretch"):
        px_per_pt = max(avail_w / eff_w, avail_h / eff_h)
    else:  # fit
        px_per_pt = min(avail_w / eff_w, avail_h / eff_h)

    if not stretch:
        # Anything that will not fit is center-cropped later anyway; only
        # render the visible window (keeps big --scale values cheap).
        vis_w_pt = (avail_w + 2) / px_per_pt
        vis_h_pt = (avail_h + 2) / px_per_pt
        page_vis_w, page_vis_h = (vis_h_pt, vis_w_pt) if quarter else (vis_w_pt, vis_h_pt)
        x0, x1 = _shrink_to(x0, x1, page_vis_w)
        y0, y1 = _shrink_to(y0, y1, page_vis_h)

    largest = max(x1 - x0, y1 - y0) * px_per_pt
    if largest > _MAX_RASTER_DIM:  # only reachable for extreme stretch aspect ratios
        px_per_pt *= _MAX_RASTER_DIM / largest

    crop_tuple = (
        max(0.0, x0),
        max(0.0, y0),
        max(0.0, width_pt - x1),
        max(0.0, height_pt - y1),
    )
    try:
        bmp = page.render(scale=px_per_pt, crop=crop_tuple)
        raster = bmp.to_pil()
    except Exception as exc:  # pragma: no cover - pdfium raises many types
        raise RenderError("could not render PDF page: %s" % exc) from exc

    raster_opts = replace(
        opts,
        rotate=str(rotate_deg),
        crop="none",
        scale=None,
        fit="stretch" if stretch else "actual",
    )
    return raster, raster_opts


# ---------------------------------------------------------------------------
# Image path
# ---------------------------------------------------------------------------


def _open_via_sips(data: bytes, filename: Optional[str]) -> Optional[Image.Image]:
    """Fall back to macOS `sips` for formats Pillow cannot open (e.g. HEIC)."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            suffix = Path(filename).suffix if filename else ""
            in_path = Path(tmp) / ("input" + (suffix or ".bin"))
            in_path.write_bytes(data)
            out_path = Path(tmp) / "converted.png"
            subprocess.run(
                ["sips", "-s", "format", "png", str(in_path), "--out", str(out_path)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            img = Image.open(out_path)
            img.load()
            return img
    except Exception:
        return None


def _load_frames(data: bytes, filename: Optional[str]) -> List[Image.Image]:
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        im = _open_via_sips(data, filename)
        if im is None:
            raise RenderError(
                "unsupported or unreadable file"
                + (" (%s)" % filename if filename else "")
                + ": not a PDF, and neither Pillow nor sips could open it"
            )

    fmt = (im.format or "").upper()
    try:
        n_frames = int(getattr(im, "n_frames", 1))
    except Exception:
        n_frames = 1

    if fmt == "GIF":
        # Animated GIF: first frame only.
        im.seek(0)
        return [im.convert(im.mode)]
    if n_frames > 1:
        frames = []
        for i in range(n_frames):
            im.seek(i)
            frames.append(im.copy())
        return frames
    return [im]


def _render_raster(
    data: bytes, size: LabelSize, opts: RenderOptions, filename: Optional[str]
) -> List[Image.Image]:
    frames = _load_frames(data, filename)
    try:
        indexes = parse_pages(opts.pages, len(frames))
    except ValueError as exc:
        raise RenderError(str(exc)) from exc
    return [render_image(frames[i], size, opts) for i in indexes]


# ---------------------------------------------------------------------------
# Shared image -> label pipeline (used for images and rasterised PDF pages)
# ---------------------------------------------------------------------------


def _flatten_to_grayscale(img: Image.Image) -> Image.Image:
    mode = img.mode
    if mode == "P":
        img = img.convert("RGBA") if "transparency" in img.info else img.convert("RGB")
        mode = img.mode
    if mode == "LA":
        img = img.convert("RGBA")
        mode = img.mode
    if mode == "RGBA":
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img).convert("RGB")
    if img.mode != "L":
        img = img.convert("L")
    return img


def _resolve_rotation(img_size, label_dots, rotate_opt: str) -> int:
    if rotate_opt in ("0", "90", "180", "270"):
        return int(rotate_opt)
    if rotate_opt != "auto":
        raise RenderError("invalid rotate option: %r" % rotate_opt)

    img_w, img_h = img_size
    label_w, label_h = label_dots
    if not img_w or not img_h or not label_w or not label_h:
        return 0
    if img_w == img_h or label_w == label_h:
        return 0
    content_landscape = img_w > img_h
    label_landscape = label_w > label_h
    return 90 if content_landscape != label_landscape else 0


_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,  # PIL rotates counter-clockwise; 90 = clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def _available_dots(size: LabelSize, opts: RenderOptions):
    margin_px = mm_to_dots(max(opts.margin_mm or 0.0, 0.0))
    return (
        max(1, size.width_dots - 2 * margin_px),
        max(1, size.height_dots - 2 * margin_px),
    )


def _image_dpi(img: Image.Image) -> Optional[float]:
    """Horizontal DPI from metadata, ignoring the ubiquitous 72-dpi placeholder
    (JFIF/PNG writers stamp 72 whether or not anyone chose a resolution)."""
    dpi_info = img.info.get("dpi") if hasattr(img, "info") else None
    if not dpi_info:
        return None
    try:
        dpi_x = float(dpi_info[0])
    except Exception:
        return None
    if dpi_x <= 0 or round(dpi_x) == 72:
        return None
    return dpi_x


def _trim_white(img: Image.Image) -> Image.Image:
    """Crop an "L" image to its non-white content plus a small pad."""
    bbox = img.point(_dark_lut()).getbbox()
    if bbox is None:
        return img  # blank: keep the whole thing
    pad = max(2, round(0.005 * max(img.size)))
    left, upper, right, lower = bbox
    box = (
        max(0, left - pad),
        max(0, upper - pad),
        min(img.width, right + pad),
        min(img.height, lower + pad),
    )
    return img if box == (0, 0, img.width, img.height) else img.crop(box)


def _resize(img: Image.Image, width: int, height: int) -> Image.Image:
    width = max(1, width)
    height = max(1, height)
    if (width, height) == img.size:
        return img
    return img.resize((width, height), Image.Resampling.LANCZOS)


def _paste_aligned(canvas: Image.Image, content: Image.Image, align: str, margin_px: int) -> None:
    cw, ch = canvas.size
    avail_w = max(1, cw - 2 * margin_px)
    avail_h = max(1, ch - 2 * margin_px)

    # Content bigger than the printable area (fill mode, or actual/scale
    # enlargements) is center-cropped down to it.
    rw, rh = content.size
    if rw > avail_w:
        left = (rw - avail_w) // 2
        content = content.crop((left, 0, left + avail_w, rh))
        rw = avail_w
    if rh > avail_h:
        top = (rh - avail_h) // 2
        content = content.crop((0, top, rw, top + avail_h))
        rh = avail_h

    if align == "top-left":
        x, y = margin_px, margin_px
    elif align == "top":
        x, y = margin_px + (avail_w - rw) // 2, margin_px
    else:  # center
        x = margin_px + (avail_w - rw) // 2
        y = margin_px + (avail_h - rh) // 2
    canvas.paste(content, (x, y))


def _validate(opts: RenderOptions) -> None:
    if opts.fit not in _FITS:
        raise RenderError("invalid fit %r (use one of: %s)" % (opts.fit, ", ".join(_FITS)))
    if opts.crop not in ("auto", "none"):
        raise RenderError("invalid crop %r (use auto or none)" % opts.crop)
    if opts.align not in _ALIGNS:
        raise RenderError("invalid align %r (use one of: %s)" % (opts.align, ", ".join(_ALIGNS)))
    if opts.dither not in ("threshold", "floyd"):
        raise RenderError("invalid dither %r (use threshold or floyd)" % opts.dither)
    if opts.scale is not None and not (0 < opts.scale <= 5000):
        raise RenderError("scale must be a percentage between 0 and 5000, not %r" % opts.scale)
    if not 0 <= int(opts.threshold) <= 255:
        raise RenderError("threshold must be 0..255, not %r" % opts.threshold)
    _resolve_rotation((1, 1), (1, 1), opts.rotate)  # raises on junk


def render_image(img: Image.Image, size: LabelSize, opts: RenderOptions) -> Image.Image:
    """Turn a PIL image into a 1-bit label bitmap, exactly `size` dots.

    Used both for directly-supplied images and for a rasterised PDF page.
    """
    _validate(opts)
    dpi = _image_dpi(img)  # read before any transform drops .info
    img = ImageOps.exif_transpose(img)
    img = _flatten_to_grayscale(img)
    if opts.crop == "auto":
        img = _trim_white(img)

    label_w_dots = size.width_dots
    label_h_dots = size.height_dots
    avail_w, avail_h = _available_dots(size, opts)

    rotate_deg = _resolve_rotation(img.size, (label_w_dots, label_h_dots), opts.rotate)
    if rotate_deg % 360:
        img = img.transpose(_TRANSPOSE[rotate_deg % 360])  # lossless

    img_w, img_h = img.size
    # "Physical size": honour real DPI metadata, else one pixel = one dot.
    physical = DPI / dpi if dpi else 1.0

    if opts.scale is not None:
        factor = physical * opts.scale / 100.0
        content = _resize(img, round(img_w * factor), round(img_h * factor))
    elif opts.fit == "stretch":
        content = _resize(img, avail_w, avail_h)
    elif opts.fit == "actual":
        content = _resize(img, round(img_w * physical), round(img_h * physical))
    elif opts.fit == "fill":
        ratio = max(avail_w / img_w, avail_h / img_h)
        content = _resize(img, round(img_w * ratio), round(img_h * ratio))
    else:  # "fit" (default)
        ratio = min(avail_w / img_w, avail_h / img_h)
        content = _resize(img, round(img_w * ratio), round(img_h * ratio))

    canvas = Image.new("L", (label_w_dots, label_h_dots), 255)
    margin_px = mm_to_dots(max(opts.margin_mm or 0.0, 0.0))
    _paste_aligned(canvas, content, opts.align, margin_px)

    if opts.invert:
        canvas = ImageOps.invert(canvas)

    if opts.dither == "floyd":
        return canvas.convert("1")  # Pillow's default convert("1") is Floyd-Steinberg
    threshold = int(opts.threshold)
    lut = [255 if i >= threshold else 0 for i in range(256)]
    return canvas.point(lut).convert("1", dither=Image.Dither.NONE)
