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
_pdf_lock = threading.Lock()  # pdfium is not thread-safe


@dataclass
class Page:
    number: int  # 1-based, as in the document
    image: Optional[np.ndarray]  # BGR; None when the text layer was used
    width: int
    height: int
    text_layer: Optional[str] = None


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
        self._frames: list[Image.Image] = []
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
            n = getattr(im, "n_frames", 1) if im.format == "TIFF" else 1  # multi-page TIFF scans
            for i in range(n):
                im.seek(i)
                self._frames.append(ImageOps.exif_transpose(im).copy())
        except ApiError:
            raise
        except Image.DecompressionBombError as exc:
            raise ApiError(413, "too_large", "the image has too many pixels") from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiError(415, "unsupported_format", "not a readable image or PDF "
                           "(JPEG, PNG, WEBP, TIFF, BMP, GIF, PDF)") from exc
        self.page_count = len(self._frames)

    def page(self, number: int, use_text_layer: bool = True) -> Page:
        if self.kind == "image":
            img = _bgr(self._frames[number - 1])
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
                    finally:
                        tp.close()
                    # A real text layer: use it. A few stray characters on a scan: read the picture.
                    if len(re.sub(r"\s", "", text)) >= 20:
                        return Page(number, None, round(w), round(h), text)
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
        self._frames.clear()

    def __enter__(self) -> "Document":
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.close()
