"""Shared fixtures for munbyn/render.py tests.

Everything here is generated at test time -- no binaries are committed.
PDFs are built with a tiny hand-rolled writer (catalog/pages/content-stream
objects + a classic xref table); images are built with Pillow.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageOps

# ---------------------------------------------------------------------------
# Minimal PDF writer (single Helvetica font resource shared by all pages)
# ---------------------------------------------------------------------------


def build_pdf(pages):
    """Build a PDF from a list of (width_pt, height_pt, content_bytes[, rotate])."""
    n_pages = len(pages)
    page_obj_nums = []
    content_obj_nums = []
    next_num = 3
    for _ in range(n_pages):
        page_obj_nums.append(next_num)
        next_num += 1
        content_obj_nums.append(next_num)
        next_num += 1
    font_obj_num = next_num

    bodies = {}
    kids = " ".join("%d 0 R" % n for n in page_obj_nums)
    bodies[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    bodies[2] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n_pages)).encode("ascii")
    for i, page in enumerate(pages):
        width, height, content = page[:3]
        rotate = ("/Rotate %d " % page[3]) if len(page) > 3 else ""
        page_obj = page_obj_nums[i]
        content_obj = content_obj_nums[i]
        bodies[page_obj] = (
            "<< /Type /Page /Parent 2 0 R " + rotate + "/MediaBox [0 0 %g %g] "
            "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (width, height, font_obj_num, content_obj)
        ).encode("ascii")
        bodies[content_obj] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
    bodies[font_obj_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    total_objs = font_obj_num
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (total_objs + 1)
    for n in range(1, total_objs + 1):
        offsets[n] = len(out)
        out += ("%d 0 obj\n" % n).encode("ascii")
        out += bodies[n]
        out += b"\nendobj\n"
    xref_offset = len(out)
    out += ("xref\n0 %d\n" % (total_objs + 1)).encode("ascii")
    out += b"0000000000 65535 f \n"
    for n in range(1, total_objs + 1):
        out += ("%010d 00000 n \n" % offsets[n]).encode("ascii")
    out += (
        "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (total_objs + 1, xref_offset)
    ).encode("ascii")
    return bytes(out)


def pdf_rect_stroke(x, y, w, h, line_width=2.0):
    return ("%g w\n0 0 0 RG\n%g %g %g %g re S\n" % (line_width, x, y, w, h)).encode("ascii")


def pdf_rect_fill(x, y, w, h):
    return ("0 0 0 rg\n%g %g %g %g re f\n" % (x, y, w, h)).encode("ascii")


def pdf_text(x, y, size, text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return ("BT /F1 %g Tf %g %g Td (%s) Tj ET\n" % (size, x, y, escaped)).encode("ascii")


def pdf_barcode(x, y, height, bar_widths, gap=2.0):
    ops = bytearray()
    cursor = float(x)
    for width in bar_widths:
        ops += pdf_rect_fill(cursor, y, width, height)
        cursor += width + gap
    return bytes(ops)


# Shared bar widths (points) for the barcode-crispness test: 15 bars, 1-3pt wide.
BARCODE_BAR_WIDTHS = [1, 2, 3, 1, 3, 2, 1, 1, 2, 3, 1, 2, 3, 2, 1]


def dark_bbox(img):
    """Bounding box of originally-black content in a mode "1" image, or None."""
    inverted = ImageOps.invert(img.convert("L"))
    return inverted.getbbox()


# ---------------------------------------------------------------------------
# (a) Letter-size vector PDF with a 4x6in shipping label in the top-left
# ---------------------------------------------------------------------------


@pytest.fixture
def letter_shipping_label_pdf():
    return make_letter_shipping_label_pdf()


def make_letter_shipping_label_pdf():
    page_w, page_h = 612.0, 792.0  # US Letter, points
    box_w, box_h = 288.0, 432.0  # 4in x 6in, points
    x0, y0 = 18.0, page_h - 18.0 - box_h  # 0.25in margin from the top-left corner
    x1, y1 = x0 + box_w, y0 + box_h

    content = bytearray()
    content += pdf_rect_stroke(x0, y0, box_w, box_h, line_width=2.0)
    content += pdf_text(x0 + 10, y1 - 30, 16, "SHIP TO:")
    content += pdf_text(x0 + 10, y1 - 50, 11, "123 MAIN ST")
    content += pdf_text(x0 + 10, y1 - 65, 11, "ANYTOWN ST 00000")
    content += pdf_barcode(x0 + 10, y0 + 15, 60, BARCODE_BAR_WIDTHS, gap=2.0)
    return build_pdf([(page_w, page_h, bytes(content))])


# ---------------------------------------------------------------------------
# (b) 2-page PDF
# ---------------------------------------------------------------------------


@pytest.fixture
def two_page_pdf():
    return make_two_page_pdf()


def make_two_page_pdf():
    content1 = pdf_rect_fill(20, 20, 100, 100) + pdf_text(20, 140, 12, "PAGE ONE")
    content2 = pdf_rect_fill(20, 20, 50, 50) + pdf_text(20, 90, 12, "PAGE TWO")
    return build_pdf([(200.0, 200.0, content1), (200.0, 200.0, content2)])


# ---------------------------------------------------------------------------
# (c) Landscape PNG with alpha
# ---------------------------------------------------------------------------


@pytest.fixture
def landscape_alpha_png():
    img = Image.new("RGBA", (300, 150), (0, 0, 0, 0))  # fully transparent background
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 259, 109], fill=(0, 0, 0, 255))  # opaque black block
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# (d) JPEG with EXIF orientation 6
# ---------------------------------------------------------------------------


@pytest.fixture
def exif_orientation_jpeg():
    width, height = 200, 100  # stored (raw) landscape pixels
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Marker in the raw top-right corner; exif_transpose(orientation=6) should
    # move it to a different corner of the corrected, portrait-shaped image.
    draw.rectangle([width - 20, 0, width - 1, 19], fill=(0, 0, 0))
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation
    img.save(buf, format="JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# (e) Blank page PDF
# ---------------------------------------------------------------------------


@pytest.fixture
def blank_page_pdf():
    return build_pdf([(200.0, 300.0, b"")])


# ---------------------------------------------------------------------------
# (f) 16-bit / palette PNG edge cases
# ---------------------------------------------------------------------------


@pytest.fixture
def sixteen_bit_png():
    """A 16-bit ("I;16") grayscale PNG: left half white, right half black."""
    width, height = 40, 30
    img = Image.new("I", (width, height), 0)
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = 65535 if x < width // 2 else 0
    img16 = img.convert("I;16")
    buf = io.BytesIO()
    img16.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def palette_png_with_transparency():
    """A palette ("P") mode PNG with a per-index transparency table."""
    img = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 49, 29], fill=(20, 20, 20, 255))
    quantized = img.quantize(colors=8, method=Image.FASTOCTREE)
    buf = io.BytesIO()
    quantized.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Extra fixtures used by specific tests (rotation, fit modes, barcode)
# ---------------------------------------------------------------------------


@pytest.fixture
def barcode_only_pdf():
    """A tiny page containing only vertical bars -- nothing else dark."""
    quiet, height, gap = 10.0, 60.0, 2.0
    total_w = quiet * 2 + sum(BARCODE_BAR_WIDTHS) + gap * (len(BARCODE_BAR_WIDTHS) - 1)
    total_h = quiet * 2 + height
    content = pdf_barcode(quiet, quiet, height, BARCODE_BAR_WIDTHS, gap=gap)
    return build_pdf([(total_w, total_h, content)])


@pytest.fixture
def landscape_solid_png():
    return make_landscape_solid_png()


def make_landscape_solid_png():
    """A clearly-landscape image: a thick black frame just inside the edges."""
    img = Image.new("RGB", (400, 200), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([5, 5, 394, 194], outline=(0, 0, 0), width=15)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def full_bleed_square_png():
    """A solid black square image, edge to edge (for fill/actual/scale tests)."""
    img = Image.new("RGB", (120, 120), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def circle_png():
    """A filled circle touching all four edges of its (square) canvas."""
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, 199, 199], fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
