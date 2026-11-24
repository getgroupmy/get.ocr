"""Turns an upload (an image or a PDF) into pages the reader can take."""
from __future__ import annotations
from typing import Optional

import io
import re
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

from .config import get_settings
from .errors import ApiError

IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "MPO", "TIFF", "BMP", "GIF"}
Image.MAX_IMAGE_PIXELS = 80_000_000  # a larger picture is refused (decompression bombs)
_EDITORS = re.compile(r"photoshop|gimp|canva|illustrator|pixelmator|affinity|picsart|snapseed|lightroom|paint\.net|"
                      r"ilovepdf|smallpdf|sejda|pdfescape|pdf-?xchange|phantompdf|foxit pdf editor|nitro|pdfelement|"
                      r"inkscape|libreoffice draw", re.I)
_pdf_lock = threading.Lock()  # pdfium is not thread-safe


@dataclass
class Page:
    number: int  # 1-based, as in the document
    image: Optional[np.ndarray]  # BGR; None when the text layer was used
    width: int
    height: int
    text_layer: Optional[str] = None
    text_lines: Optional[list] = None  # the text layer as positioned pieces, in the shape engine.read gives


def _layer_lines(tp, height: float) -> list[dict]:  # noqa: ANN001
    """The pieces of text in a PDF's text layer with their place on the page
    (points, from the top left). Words that follow each other closely are one
    piece; a wider gap, as between the columns of a table, starts a new one."""
    n = tp.count_chars()
    whole = tp.get_text_range()
    if len(whole) != n:
        whole = None
    pieces: list[list] = []  # [text, left, bottom, right, top]
    cur: Optional[list] = None
    spaced = False
    for i in range(n):
        ch = whole[i] if whole is not None else tp.get_text_range(i, 1)
        if not ch or ch.isspace() or ch in "\x00\ufffe\uffff":
            spaced = True
            continue
        left, bottom, right, top = tp.get_charbox(i)
        if right <= left and top <= bottom:
            continue
        if cur is not None:
            line_h = max(cur[4] - cur[2], top - bottom, 1.0)
            same_line = bottom < cur[4] and top > cur[2]
            gap = left - cur[3]
            if same_line and -0.5 * line_h <= gap < 0.9 * line_h:
                cur[0] += (" " if spaced else "") + ch
                cur[1:] = [min(cur[1], left), min(cur[2], bottom), max(cur[3], right), max(cur[4], top)]
                spaced = False
                continue
        cur = [ch, left, bottom, right, top]
        pieces.append(cur)
        spaced = False
    out = []
    for text, left, bottom, right, top in pieces:
        y0, y1 = round(height - top, 1), round(height - bottom, 1)
        x0, x1 = round(left, 1), round(right, 1)
        out.append({"text": text, "confidence": None, "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]})
    return out


def _bgr(im: Image.Image) -> np.ndarray:
    side = get_settings().max_image_side
    im = im.convert("RGB")
    if max(im.size) > side:
        im.thumbnail((side, side))
    return np.ascontiguousarray(np.asarray(im)[:, :, ::-1])


def parse_pages(spec: Optional[str], count: int) -> list[int]:
    """'1-3,7' -> [1, 2, 3, 7]. Empty means every page."""
    if not spec or not spec.strip():
        return list(range(1, count + 1))
    chosen: set[int] = set()
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise ApiError(422, "bad_pages", "pages must look like 1-3,7")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if first < 1 or last < first:
            raise ApiError(422, "bad_pages", "pages must look like 1-3,7")
        if first > count:
            raise ApiError(422, "bad_pages", f"the document has {count} page(s)")
        chosen.update(range(first, min(last, count) + 1))
    return sorted(chosen)


class Document:
    """An opened upload. Use as a context manager."""

    def __init__(self, data: bytes):
        self.kind = "pdf" if data[:1024].lstrip().startswith(b"%PDF") else "image"
        self._pdf = None
        self._exif: dict = {}
        self._im: Optional[Image.Image] = None  # the open image; frames are decoded one at a time
        self._im_data = b""  # keeps the buffer the image reads from alive
        if self.kind == "pdf":
            self._open_pdf(data)
        else:
            self._open_image(data)

    def _open_pdf(self, data: bytes) -> None:
        import pypdfium2 as pdfium
        try:
            with _pdf_lock:
                self._pdf = pdfium.PdfDocument(data)
                self.page_count = len(self._pdf)
        except Exception as exc:  # noqa: BLE001
            protected = "password" in str(exc).lower()
            raise ApiError(422, "pdf_protected" if protected else "bad_file",
                           "the PDF is password protected" if protected else "not a readable PDF") from exc
        if self.page_count == 0:
            raise ApiError(422, "bad_file", "the PDF has no pages")

    def _open_image(self, data: bytes) -> None:
        try:
            im = Image.open(io.BytesIO(data))
            if im.format not in IMAGE_FORMATS:
                raise ApiError(415, "unsupported_format", f"unsupported file format {im.format}")
            self._exif = {k: (v.strip() if isinstance(v, str) else v) for k, v in dict(im.getexif()).items()
                          if k in (305, 306)}
            n = getattr(im, "n_frames", 1) if im.format == "TIFF" else 1  # multi-page TIFF scans
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiError(415, "unsupported_format", "not a readable image or PDF "
                           "(JPEG, PNG, WEBP, TIFF, BMP, GIF, PDF)") from exc
        cap = get_settings().max_pages
        if n > cap:  # refuse before decoding: a tiny many-frame TIFF would otherwise exhaust memory
            raise ApiError(422, "too_many_pages", f"the document has {n} pages; the limit is {cap}")
        # Frames are decoded one at a time in page(), not all at once.
        self._im = im
        self._im_data = data
        self.page_count = n

    def _frame(self, number: int) -> np.ndarray:
        """Decode one image frame (a page of a multi-frame TIFF) to BGR, on demand."""
        assert self._im is not None
        try:
            if self.page_count > 1:
                self._im.seek(number - 1)
            return _bgr(ImageOps.exif_transpose(self._im))
        except Image.DecompressionBombError as exc:
            raise ApiError(413, "too_large", "the image has too many pixels") from exc

    def info(self) -> dict:
        """What the file says about how it was made. `edited_with` names an editing program when the
        file carries one's mark. These are hints: their absence proves nothing, and a scan app or a
        bank's own system also leaves a name here."""
        out = {"producer": None, "creator": None, "created": None, "modified": None, "edited_with": None}
        try:
            if self._pdf is not None:
                with _pdf_lock:
                    meta = self._pdf.get_metadata_dict()
                out.update(producer=meta.get("Producer") or None, creator=meta.get("Creator") or None,
                           created=meta.get("CreationDate") or None, modified=meta.get("ModDate") or None)
            elif self._im is not None:
                exif = self._exif or {}
                out.update(producer=exif.get(305) or None, created=exif.get(306) or None)  # Software, DateTime
        except Exception:  # noqa: BLE001 - damaged metadata is not worth failing a reading for
            return out
        named = " ".join(str(out[k] or "") for k in ("producer", "creator"))
        hit = _EDITORS.search(named)
        out["edited_with"] = hit.group(0) if hit else None
        return out

    def page(self, number: int, use_text_layer: bool = True, with_lines: bool = False) -> Page:
        if self.kind == "image":
            img = self._frame(number)
            return Page(number, img, img.shape[1], img.shape[0])
        s = get_settings()
        with _pdf_lock:
            p = self._pdf[number - 1]
            try:
                w, h = p.get_size()
                if use_text_layer:
                    tp = p.get_textpage()
                    try:
                        text = tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n").strip()
                        real = len(re.sub(r"\s", "", text)) >= 20
                        lines = _layer_lines(tp, h) if real and with_lines else None
                    finally:
                        tp.close()
                    # A real text layer: use it. A few stray characters on a scan: read the picture.
                    if real:
                        return Page(number, None, round(w), round(h), text, lines)
                scale = min(s.pdf_dpi / 72, s.max_image_side / max(w, h, 1))
                img = _bgr(p.render(scale=scale).to_pil())
            finally:
                p.close()
        return Page(number, img, img.shape[1], img.shape[0])

    def close(self) -> None:
        if self._pdf is not None:
            with _pdf_lock:
                self._pdf.close()
            self._pdf = None
        if self._im is not None:
            self._im.close()
            self._im = None

    def __enter__(self) -> "Document":
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.close()
