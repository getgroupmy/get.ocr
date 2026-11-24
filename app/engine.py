"""The reader: PP-OCR (PaddleOCR models) through RapidOCR, on the CPU.

Engine: onnxruntime (Mac, x86 with AVX) or openvino (Celeron NAS without AVX),
set with OCR_ENGINE. One page is read at a time; each reading already uses
every core."""
from __future__ import annotations

import logging
import threading

import numpy as np

from .config import get_settings

NAME = "pp-ocr"
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
                _rapid = RapidOCR(params={"Det.engine_type": e, "Cls.engine_type": e, "Rec.engine_type": e,
                                          "Global.log_level": "error"})
            except Exception:  # noqa: BLE001
                log.exception("OCR engine could not be loaded")
                _failed = True
    return _rapid


def available() -> bool:
    return _engine() is not None


def read(img: np.ndarray) -> list[dict]:
    """Text lines on a BGR image in reading order. Each line has its text, the
    model's confidence (0 to 1) and its four corners in image pixels, clockwise
    from the top left."""
    eng = _engine()
    if eng is None:
        raise RuntimeError("OCR engine is not available")
    with _run_lock:
        r = eng(img)
    if r is None or r.txts is None:
        return []
    floor = get_settings().min_confidence
    lines = []
    for box, txt, score in zip(r.boxes, r.txts, r.scores):
        if score < floor or not txt.strip():
            continue
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
