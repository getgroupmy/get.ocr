"""Makes a scan searchable: a PDF of the same page pictures with the text that was read laid
invisibly over each line, so the words can be found, selected and copied in any PDF viewer."""
from __future__ import annotations

import ctypes
import io
from pathlib import Path
from typing import Optional

from . import engine, jawi
from .config import get_settings
from .documents import Document, parse_pages
from .errors import ApiError


_FONTS = Path(__file__).resolve().parent.parent / "models" / "fonts"
# the font that carries the hidden text of each script (Latin script uses a standard PDF font)
HIDDEN_FONTS = {"jawi": _FONTS / "hidden-text.ttf", "arabic": _FONTS / "hidden-text.ttf",
                "tamil": _FONTS / "hidden-text-tamil.ttf", "thai": _FONTS / "hidden-text-thai.ttf"}


def build(data: bytes, pages: Optional[str], max_pages: int, script: Optional[str] = None) -> tuple[bytes, int]:
    """The searchable PDF and how many pages it has. A PDF whose pages all carry text already is
    returned as it is. The invisible text uses a standard PDF font for Latin script; with
    a script ("jawi", "arabic", "tamil", "thai") those lines are read and carried by a small font
    put into the file."""
    import cv2
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    if not engine.available():
        raise ApiError(503, "engine_unavailable", "the OCR engine is not loaded on this server")
    with Document(data) as doc:
        numbers = parse_pages(pages, doc.page_count)
        if len(numbers) > max_pages:
            raise ApiError(422, "too_many_pages", f"{len(numbers)} pages asked for, the limit here is {max_pages}")
        if doc.kind == "pdf" and len(numbers) == doc.page_count and \
                all(doc.page(n).text_layer is not None for n in numbers):
            return data, doc.page_count
        out = pdfium.PdfDocument.new()
        font = raw.FPDFText_LoadStandardFont(out, b"Helvetica")
        loaded: dict = {}  # a script's font is put into the file when its first line needs it: 20 to 55 KB each
        for n in numbers:
            source = doc.page(n, use_text_layer=False)
            lines, image, _ = engine.read_upright(source.image, script)
            px_h, px_w = image.shape[:2]
            # a PDF page keeps its size (it was drawn at pdf_dpi); a picture is taken to be a 150 dpi scan
            scale = 72 / (get_settings().pdf_dpi if doc.kind == "pdf" else 150)
            w, h = px_w * scale, px_h * scale
            page = out.new_page(w, h)
            ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                raise ApiError(422, "bad_file", "a page could not be written")
            picture = pdfium.PdfImage.new(out)
            picture.load_jpeg(io.BytesIO(jpeg.tobytes()), inline=True)
            picture.set_matrix(pdfium.PdfMatrix().scale(w, h))
            page.insert_obj(picture)
            for ln in lines:
                xs, ys = [p[0] * scale for p in ln["box"]], [p[1] * scale for p in ln["box"]]
                use = font
                path = HIDDEN_FONTS.get(ln.get("script") or "")
                if path is not None:
                    if path not in loaded:
                        glyphs = path.read_bytes()
                        loaded[path] = raw.FPDFText_LoadFont(out, (ctypes.c_uint8 * len(glyphs)).from_buffer_copy(glyphs),
                                                             len(glyphs), raw.FPDF_FONT_TRUETYPE, 1)
                    # A PDF holds the letters where they sit on the paper, left to right; a reader of
                    # the file turns right-to-left script back into reading order. logical() undoes itself.
                    use = loaded[path]
                    text = jawi.logical(ln["text"]) if ln["script"] in ("jawi", "arabic") else ln["text"]
                else:
                    text = ln["text"].encode("latin-1", "ignore").decode("latin-1")  # what the standard font can carry
                if not text.strip() or not use:
                    continue
                obj = raw.FPDFPageObj_CreateTextObj(out, use, ctypes.c_float(max(max(ys) - min(ys), 1.0)))
                encoded = (text + "\x00").encode("utf-16-le")
                raw.FPDFText_SetText(obj, ctypes.cast(ctypes.c_char_p(encoded), ctypes.POINTER(ctypes.c_ushort)))
                raw.FPDFTextObj_SetTextRenderMode(obj, raw.FPDF_TEXTRENDERMODE_INVISIBLE)
                left, bottom, right, top = (ctypes.c_float() for _ in range(4))
                raw.FPDFPageObj_GetBounds(obj, left, bottom, right, top)
                stretch = (max(xs) - min(xs)) / max(right.value - left.value, 1.0)  # as wide as the printed line
                raw.FPDFPageObj_Transform(obj, stretch, 0, 0, 1, min(xs), h - max(ys))
                raw.FPDFPage_InsertObject(page, obj)
            page.gen_content()
        buf = io.BytesIO()
        out.save(buf)
        out.close()
        return buf.getvalue(), len(numbers)
