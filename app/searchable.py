"""Makes a scan searchable: a PDF of the same page pictures with the text that was read laid
invisibly over each line, so the words can be found, selected and copied in any PDF viewer."""
from __future__ import annotations

import ctypes
import io
from typing import Optional

from . import engine
from .config import get_settings
from .documents import Document, parse_pages
from .errors import ApiError


def build(data: bytes, pages: Optional[str], max_pages: int) -> tuple[bytes, int]:
    """The searchable PDF and how many pages it has. A PDF whose pages all carry text already is
    returned as it is. Latin script only: the invisible text uses a standard PDF font."""
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
        for n in numbers:
            source = doc.page(n, use_text_layer=False)
            lines, image, _ = engine.read_upright(source.image)
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
                text = ln["text"].encode("latin-1", "ignore").decode("latin-1")  # what the standard font can carry
                if not text.strip():
                    continue
                obj = raw.FPDFPageObj_CreateTextObj(out, font, ctypes.c_float(max(max(ys) - min(ys), 1.0)))
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
