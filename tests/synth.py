"""Made-up documents for the tests."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
         "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf")


def _font(size: int):
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def page_image(lines: list[str]) -> Image.Image:
    im = Image.new("RGB", (1100, 120 + 80 * len(lines)), "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((50, 50 + 80 * i), line, fill="black", font=_font(38))
    return im


def image_bytes(lines: list[str], fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    page_image(lines).save(buf, fmt)
    return buf.getvalue()


def scanned_pdf(pages: list[list[str]]) -> bytes:
    """A PDF of pictures, as a scanner makes: no text layer."""
    ims = [page_image(p) for p in pages]
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:])
    return buf.getvalue()


def text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer (Helvetica)."""
    stream = f"BT /F1 18 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
