"""The reader: PP-OCR (PaddleOCR models) through RapidOCR, on the CPU.

Engine: onnxruntime (Mac, x86 with AVX) or openvino (Celeron NAS without AVX),
set with OCR_ENGINE. One page is read at a time; each reading already uses
every core."""
from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

from .config import get_settings

NAME = "pp-ocr"
LONE_CHARACTER = 0.75  # a line of one character is kept only when the model is this sure of it
_rapid = None
_failed = False
_load_lock = threading.Lock()
_run_lock = threading.Lock()
log = logging.getLogger("ocr.engine")


def _engine():
    global _rapid, _failed
    with _load_lock:
        if _rapid is None and not _failed:
            choice = get_settings().engine.lower()
            if choice == "off":
                _failed = True
                return None
            try:
                from rapidocr import EngineType, RapidOCR
                logging.getLogger("RapidOCR").setLevel(logging.ERROR)
                e = EngineType.OPENVINO if choice == "openvino" else EngineType.ONNXRUNTIME
                # box_thresh below the library's 0.5: a lone small character (the quantity on a receipt
                # line) is otherwise not found at all
                _rapid = RapidOCR(params={"Det.engine_type": e, "Cls.engine_type": e, "Rec.engine_type": e,
                                          "Det.box_thresh": 0.3,
                                          "Global.log_level": "error"})
            except Exception:  # noqa: BLE001
                log.exception("OCR engine could not be loaded")
                _failed = True
    return _rapid


def available() -> bool:
    return _engine() is not None


def even_lighting(img: np.ndarray) -> Optional[np.ndarray]:
    """A photo taken under uneven light (a shadow across the paper, a lamp on one
    side) with the light evened out, or None when the picture is evenly lit
    already, as a scan is. Text in a shadow is otherwise read worse or missed."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    scale = 256 / max(h, w, 256)
    small = cv2.resize(gray, (max(int(w * scale), 8), max(int(h * scale), 8)), interpolation=cv2.INTER_AREA)
    # how bright the paper is around each point
    paper = cv2.GaussianBlur(small, (0, 0), max(small.shape) / 50)
    lit = paper[paper > 80]  # the sheet itself; a dark table around it is not uneven light
    if lit.size < paper.size * 0.2:
        return None
    lo, hi = np.percentile(lit, (5, 95))
    if lo / max(hi, 1) > 0.8:
        return None
    paper = cv2.resize(paper, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    flat = np.clip(gray.astype(np.float32) / np.maximum(paper, 60) * 230, 0, 255).astype(np.uint8)
    return cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR)


def read(img: np.ndarray) -> list[dict]:
    """Text lines on a BGR image in reading order. Each line has its text, the
    model's confidence (0 to 1) and its four corners in image pixels, clockwise
    from the top left."""
    eng = _engine()
    if eng is None:
        raise RuntimeError("OCR engine is not available")
    if get_settings().even_lighting:
        evened = even_lighting(img)
        img = img if evened is None else evened
    with _run_lock:
        r = eng(img)
    if r is None or r.txts is None:
        return []
    floor = get_settings().min_confidence
    lines = []
    for box, txt, score in zip(r.boxes, r.txts, r.scores):
        if score < floor or not txt.strip():
            continue
        if len(txt.strip()) == 1 and score < LONE_CHARACTER:
            continue  # a speck or half a bracket read as a character
        lines.append({"text": txt, "confidence": round(float(score), 4),
                      "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box]})
    return [line for row in _rows(lines) for line in row]


def _rows(lines: list[dict]) -> list[list[dict]]:
    """Group lines that sit on the same row, rows top to bottom, each left to right."""
    def mid_height(line: dict) -> tuple[float, float]:
        ys = [p[1] for p in line["box"]]
        return (min(ys) + max(ys)) / 2, max(ys) - min(ys)

    rows: list[tuple[float, float, list[dict]]] = []
    for line in sorted(lines, key=lambda ln: mid_height(ln)[0]):
        cy, h = mid_height(line)
        if rows and abs(cy - rows[-1][0]) < 0.5 * max(h, rows[-1][1]):
            rows[-1][2].append(line)
        else:
            rows.append((cy, h, [line]))
    return [sorted(row, key=lambda ln: min(p[0] for p in ln["box"])) for _, _, row in rows]


def layout_text(lines: list[dict]) -> str:
    """Plain text: one printed row per line of text."""
    return "\n".join(" ".join(ln["text"] for ln in row) for row in _rows(lines))


def layout_columns(lines: list[dict]) -> str:
    """Plain text that keeps the columns: each piece of a row starts at the
    character position that matches where it sits on the page, so the amounts of
    a table stay under each other. This is what the field extraction reads."""
    widths = sorted((max(p[0] for p in ln["box"]) - min(p[0] for p in ln["box"])) / max(len(ln["text"]), 1)
                    for ln in lines if ln["text"])
    if not widths:
        return ""
    char_w = max(widths[len(widths) // 2], 1.0)  # median width of one character
    out = []
    for row in _rows(lines):
        text = ""
        for ln in row:
            col = int(min(p[0] for p in ln["box"]) / char_w)
            gap = col - len(text)
            text += (" " * max(gap, 2 if text else 0)) + ln["text"]
        out.append(text.rstrip())
    return "\n".join(out)
