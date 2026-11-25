"""Reads lines in Arabic, Tamil or Thai with the PP-OCRv5 model for that script (Apache-2.0,
8 MB each), on this machine.

The standard reader finds where the lines are; here each line is read again by the model of the
script that was asked for, and kept when it reads as that script. A model file is fetched once,
on first use or with scripts/get_models.sh, and checked against a known fingerprint."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np

from .config import get_settings

log = logging.getLogger("ocr.scripts")
_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/"
# name asked for -> the library's language code, the model's fingerprint, the letters of the script
SCRIPTS: dict = {
    "arabic": {"lang": "arabic", "sha256": "c1192e632d0baa9146ae5b756a0e635e3dc63c1733737ebfd1629e87144e9295",
               "letters": re.compile(r"[؀-ۿݐ-ݿ]"), "right_to_left": True},
    "tamil": {"lang": "ta", "sha256": "a42448808b7dea87597336f12438935f40353f1949e8360acd9e06b4da21bfe1",
              "letters": re.compile(r"[஀-௿]"), "right_to_left": False},
    "thai": {"lang": "th", "sha256": "de541dd83161c241ff426f7ecfd602a0ba77d686cf3ab9a6c255ea82fd08006e",
             "letters": re.compile(r"[฀-๿]"), "right_to_left": False},
}
MARGIN = 0.1  # the standard reader's boxes cut close: marks above and below the letters need room
MIN_SHARE = 0.6  # a line is of the script when this share of its letters are
MIN_LETTERS = 3
MIN_CONFIDENCE = 0.6
SHORT, SHORT_CONFIDENCE = 6, 0.8  # a few letters only: ticks and pen strokes read as letters too
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_MARK = re.compile(r"[ஂா-்ௗัิ-ฺ็-๎ً-ْ]")  # counted with their letter
_engines: dict = {}
_failed: set = set()
_lock = threading.Lock()


def name(script: str) -> str:
    return SCRIPTS[script]["lang"] + "-pp-ocrv5"


def model_path(script: str) -> Path:
    return Path(get_settings().data_dir) / "models" / (SCRIPTS[script]["lang"] + "_PP-OCRv5_rec_mobile.onnx")


def _fetch(script: str) -> Path:
    """The model file, fetched if it is not there. Raises when it cannot be had or is not the expected file."""
    path, want = model_path(script), SCRIPTS[script]["sha256"]
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == want:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(_BASE + path.name, timeout=60) as answer:  # noqa: S310 - a fixed https address
        data = answer.read(40_000_000)
    if hashlib.sha256(data).hexdigest() != want:
        raise ValueError("the downloaded model is not the expected file")
    part = path.with_suffix(".part")
    part.write_bytes(data)
    part.replace(path)
    return path


def _load(script: str):
    if script not in SCRIPTS:
        return None
    with _lock:
        if script not in _engines and script not in _failed:
            try:
                from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
                logging.getLogger("RapidOCR").setLevel(logging.ERROR)
                _engines[script] = RapidOCR(params={
                    "Rec.lang_type": LangRec(SCRIPTS[script]["lang"]), "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.model_type": ModelType.MOBILE, "Rec.model_path": str(_fetch(script)),
                    "Global.log_level": "error"})
            except Exception as exc:  # noqa: BLE001
                log.info("reading %s is off: %s", script, exc)
                _failed.add(script)
    return _engines.get(script)


def available(script: str) -> bool:
    return _load(script) is not None


def share(text: str, script: str) -> float:
    """How much of a text's letters belong to the script."""
    letters = _LETTER.findall(_MARK.sub("", text))
    return len(SCRIPTS[script]["letters"].findall(_MARK.sub("", text))) / len(letters) if letters else 0.0


def is_script(text: str, confidence: float, script: str) -> bool:
    letters = len(SCRIPTS[script]["letters"].findall(_MARK.sub("", text)))
    return (share(text, script) >= MIN_SHARE and letters >= MIN_LETTERS
            and confidence >= (SHORT_CONFIDENCE if letters < SHORT else MIN_CONFIDENCE))


def read_lines(crops: list, script: str) -> list:
    """Cropped lines (BGR pictures) to (text in reading order, confidence) each."""
    eng = _load(script)
    out = []
    for crop in crops:
        crop = _trim(crop)
        if eng is None or crop.size == 0 or min(crop.shape[:2]) < 4:
            out.append(("", 0.0))
            continue
        with _lock:
            got = eng(crop, use_det=False, use_cls=False, use_rec=True)
        if got is None or not got.txts:
            out.append(("", 0.0))
        else:
            out.append((str(got.txts[0]).strip(), float(got.scores[0])))
    return out


def _trim(crop: np.ndarray) -> np.ndarray:
    """Cut the empty edge off a line. Measured on 160 Arabic lines: with 2 pixels of white around
    the ink 5% of characters were wrong, with 12 pixels 15%, with 24 pixels 32%."""
    import cv2
    if crop.size == 0:
        return crop
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if 2 * int((ink == 0).sum()) > ink.size:
        ink = 255 - ink  # light text on dark: the ink is the smaller part
    ys, xs = np.where(ink == 0)
    if not len(ys):
        return crop
    pad = 2
    y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + 1 + pad, crop.shape[0])
    x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + 1 + pad, crop.shape[1])
    return crop[y0:y1, x0:x1] if (y1 - y0) >= 4 and (x1 - x0) >= 4 else crop


def _crop(img: np.ndarray, box: list) -> np.ndarray:
    """Cut a line out along its own slant, with a little room above and below."""
    import cv2
    tl, tr, br, bl = (np.array(p, np.float32) for p in box)
    down_l, down_r = (bl - tl) * MARGIN, (br - tr) * MARGIN
    quad = np.array([tl - down_l, tr - down_r, br + down_r, bl + down_l], np.float32)
    w = int(round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3]))))
    h = int(round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1]))))
    if w < 4 or h < 4:
        return np.zeros((0, 0, 3), np.uint8)
    to = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(quad, to), (w, h),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def reread(img: np.ndarray, lines: list, script: Optional[str]) -> list:
    """Take the standard reader's lines (every one it found, however unsure, the unread ones with
    empty text) and read again with the model of the script. A line that reads as that script comes
    back with "script": its name; the others are returned as they were."""
    if not lines or script not in SCRIPTS or not available(script):
        return lines
    if img.ndim == 2:
        import cv2
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    out = []
    for line, (text, conf) in zip(lines, read_lines([_crop(img, ln["box"]) for ln in lines], script)):
        ours = is_script(text, conf, script)
        # The standard reader knows Latin letters, digits and Chinese. Where it is sure of what it
        # read, and surer than this model, the line is its own.
        theirs = (line["confidence"] >= 0.9 and line["confidence"] > conf and share(line["text"], script) < 0.5
                  and len(text) < 2 * len(line["text"]))  # not a long line it read as a letter or two
        if ours and not theirs:
            out.append({"text": text, "confidence": round(conf, 4), "box": line["box"], "script": script})
        else:
            out.append(line)
    # The line finder sometimes gives a piece of a line a box of its own, and the standard reader
    # reads the piece as a Latin letter or two: drop what lies inside a line that was read here.
    read = [_span(ln["box"]) for ln in out if ln.get("script")]
    return [ln for ln in out if ln.get("script") or not any(_inside(_span(ln["box"]), big) for big in read)]


def _span(box: list) -> tuple:
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(small: tuple, big: tuple) -> bool:
    both = max(0.0, min(small[2], big[2]) - max(small[0], big[0])) * max(0.0, min(small[3], big[3]) - max(small[1], big[1]))
    area = (small[2] - small[0]) * (small[3] - small[1])
    return area > 0 and both > 0.7 * area and area < 0.5 * (big[2] - big[0]) * (big[3] - big[1])
