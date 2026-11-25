"""Reads Jawi (Malay in Arabic script) with a line model trained for it, on this machine.

The standard reader finds where the lines are; the lines it cannot read are read again here. The
model is budaya-lab/Jawi-OCR-Kraken-v1 (Apache-2.0), converted once to ONNX so it runs on the
runtime the service already has: see models/jawi/NOTICE and scripts/convert_jawi_model.py."""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("ocr.jawi")
NAME = "jawi-kraken-v1"
FOLDER = Path(__file__).resolve().parent.parent / "models" / "jawi"
HEIGHT = 120   # the model's line height in pixels
PAD = 16       # white added at both ends of a line, as in training
MARGIN = 0.15  # the standard reader's boxes cut close: dots above and below the letters need room
JOIN_GAP = 1.5  # pieces of one printed line closer than this many line heights are read as one
MIN_SHARE = 0.6  # a line is Jawi when this share of what the model read is Arabic-script letters
# Measured on 25 scanned English forms and 624 true Jawi lines: ticks, pen strokes and short words
# read as one or two Arabic letters, mostly below 0.8; true lines of three letters or more sit
# above 0.6 nineteen times in twenty.
MIN_CONFIDENCE = 0.6
MIN_LETTERS = 3
SHORT, SHORT_CONFIDENCE = 6, 0.8
_session = None
_labels: dict = {}
_failed = False
_lock = threading.Lock()
_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_MIRROR = str.maketrans("()[]", ")(][")


def _load():
    global _session, _labels, _failed
    with _lock:
        if _session is None and not _failed:
            try:
                import onnxruntime as ort
                _labels = {int(k): v for k, v in json.loads((FOLDER / "labels.json").read_text("utf-8")).items()}
                _session = ort.InferenceSession(str(FOLDER / "jawi.onnx"), providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001
                log.info("Jawi reading is off: %s", exc)
                _failed = True
    return _session


def available() -> bool:
    return _load() is not None


def _left_to_right(chars: list, i: int) -> int:
    """Where the run of Latin letters and digits that starts at i ends. A number stays whole
    ("1,200.50"), a word with digits stays whole ("A4"), words stay together ("Kuala Lumpur"); two
    numbers apart, and the small ٢ that doubles a Jawi word, are not joined. Whether "kg 250" on
    the paper was written "kg 250" or "250 kg" cannot be told from the paper: it is left as printed."""
    j = i
    while j < len(chars):
        c = chars[j]
        nxt = chars[j + 1] if j + 1 < len(chars) else ""
        if _LATIN.match(c) or _DIGIT.match(c):
            j += 1
        elif c in ".,/:-%" and j > i and (_DIGIT.match(nxt) or _LATIN.match(nxt)):
            j += 1
        elif c == " " and j > i and (_LATIN.match(nxt) or _DIGIT.match(nxt)) and (
                any(_LATIN.match(x) for x in chars[i:j]) or any(_LATIN.match(x) for x in "".join(chars[j + 1:]).split(" ")[0])):
            j += 1  # a word and the number beside it are one piece ("RM 1,250.50", "No. 12")
        else:
            break
    return j


def logical(visual: str) -> str:
    """The model gives the characters as they sit on the paper, left to right. Turn that into the
    order they are read: right to left, with numbers and Latin words kept left to right."""
    chars = [c for c in visual if c not in "\u200e\u200f"][::-1]
    out: list[str] = []
    i = 0
    while i < len(chars):
        j = _left_to_right(chars, i)
        if j == i:
            out.append(chars[i].translate(_MIRROR))
            i += 1
        else:
            out.extend(chars[i:j][::-1])
            i = j
    return "".join(out)


def read_line(gray: np.ndarray) -> tuple[str, float]:
    """One cropped line (grey, dark text on light) to its text in reading order and a confidence."""
    from PIL import Image
    session = _load()
    h, w = gray.shape[:2]
    if session is None or h < 4 or w < 4:
        return "", 0.0
    im = Image.fromarray(gray).resize((max(int(w * HEIGHT / h), 1), HEIGHT), Image.LANCZOS)
    x = np.full((HEIGHT, im.width + 2 * PAD), 255, np.uint8)
    x[:, PAD:PAD + im.width] = np.asarray(im)
    x = x.astype(np.float32) / 255
    x = x.max() - x
    with _lock:
        logits = session.run(None, {"line": x[None, None]})[0][0, :, 0, :]  # classes by steps
    e = np.exp(logits - logits.max(axis=0, keepdims=True))
    probs = e / e.sum(axis=0, keepdims=True)
    best = probs.argmax(axis=0)
    text, scores, last = [], [], 0
    for step, label in enumerate(best):
        if label != 0 and label != last:
            text.append(_labels.get(int(label), ""))
            scores.append(float(probs[label, step]))
        elif label != 0:
            scores[-1] = max(scores[-1], float(probs[label, step]))
        last = label
    if not text:
        return "", 0.0
    return logical("".join(text)).strip(), float(np.mean(scores))


def share(text: str) -> float:
    """How much of a text's letters are Arabic script."""
    letters = _LETTER.findall(text)
    return len(_ARABIC.findall(text)) / len(letters) if letters else 0.0


def is_jawi(text: str, confidence: float) -> bool:
    letters = len(_ARABIC.findall(text))
    return (share(text) >= MIN_SHARE and letters >= MIN_LETTERS
            and confidence >= (SHORT_CONFIDENCE if letters < SHORT else MIN_CONFIDENCE))


def _crop(gray: np.ndarray, box: list) -> np.ndarray:
    """Cut a line out along its own slant, with room above and below for the dots."""
    import cv2
    tl, tr, br, bl = (np.array(p, np.float32) for p in box)
    down_l, down_r = (bl - tl) * MARGIN, (br - tr) * MARGIN
    quad = np.array([tl - down_l, tr - down_r, br + down_r, bl + down_l], np.float32)
    w = int(round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3]))))
    h = int(round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1]))))
    if w < 4 or h < 4:
        return np.zeros((0, 0), np.uint8)
    to = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    return cv2.warpPerspective(gray, cv2.getPerspectiveTransform(quad, to), (w, h),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _span(box: list) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _join(a: list, b: list) -> list:
    """One box around two pieces of the same printed line, left piece first."""
    return [a[0], b[1], b[2], a[3]]


def reread(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """Take the standard reader's lines (every one it found, however unsure) and read the Jawi
    ones again. A Jawi line comes back with "script": "jawi"; the others are returned as they were."""
    import cv2
    if not lines or not available():
        return lines
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if gray.mean() < 110:
        gray = 255 - gray  # light text on dark
    kept: list[dict] = []
    jawi: list[dict] = []
    for line in lines:
        text, conf = read_line(_crop(gray, line["box"]))
        ours = is_jawi(text, conf)
        # The standard reader knows Latin letters, digits and Chinese. Where it is sure of what it
        # read and that is not Arabic script, the line is its own.
        theirs = line["confidence"] >= 0.9 and len(text) < 2 * len(line["text"]) and share(line["text"]) < 0.5
        if ours and not (theirs and conf < line["confidence"]):
            jawi.append({"text": text, "confidence": round(conf, 4), "box": line["box"], "script": "jawi"})
        else:
            kept.append(line)
    # The standard reader often cuts one printed line into pieces; the model reads better with the
    # whole line, and the pieces must be put together right to left.
    jawi.sort(key=lambda ln: (_span(ln["box"])[1] + _span(ln["box"])[3]) / 2)
    used = [False] * len(jawi)
    for i, first in enumerate(jawi):
        if used[i]:
            continue
        group, used[i], grown = [first], True, True
        while grown:
            grown = False
            spans = [_span(g["box"]) for g in group]
            gx0, gy0 = min(sp[0] for sp in spans), min(sp[1] for sp in spans)
            gx1, gy1 = max(sp[2] for sp in spans), max(sp[3] for sp in spans)
            height = max(gy1 - gy0, 1.0)
            for j, other in enumerate(jawi):
                if used[j]:
                    continue
                ox0, oy0, ox1, oy1 = _span(other["box"])
                same_row = abs((oy0 + oy1) / 2 - (gy0 + gy1) / 2) < 0.4 * height and 0.6 < (oy1 - oy0) / height < 1.6
                if same_row and max(ox0 - gx1, gx0 - ox1) < JOIN_GAP * height:
                    group.append(other)
                    used[j] = grown = True
                    break  # measure the group again before taking the next piece
        if len(group) == 1:
            kept.append(first)
            continue
        group.sort(key=lambda g: _span(g["box"])[0])
        box = group[0]["box"]
        for g in group[1:]:
            box = _join(box, g["box"])
        text, conf = read_line(_crop(gray, box))
        if is_jawi(text, conf):
            kept.append({"text": text, "confidence": round(conf, 4), "box": box, "script": "jawi"})
        else:  # together they read worse than apart: keep the pieces
            kept.extend(group)
    return kept
