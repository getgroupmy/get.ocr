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
MIXED_LETTERS, MIXED_SHARE = 8, 0.25  # a line of Jawi with Latin names in it
LATIN_DOUBT = 0.7   # a word the Jawi model read below this is tried with the standard reader
LATIN_SURE = 0.85   # and the standard reader's word is taken when it is at least this sure
_session = None
_labels: dict = {}
_failed = False
_lock = threading.Lock()
_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_LATIN_WORD = re.compile(r"^[A-Za-z0-9 .,:;/@&'’()%+\-]+$")
_PUNCT = ".,/:-%&@'’;"
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
    ("1,250.50"), a word with digits stays whole ("A4"), words and what is between them stay
    together ("Kuala Lumpur", "KPDNHEP: Ghost Smoke", "http://a.my"); two numbers apart, and the
    small ٢ that doubles a Jawi word, are not joined. Whether "kg 250" on the paper was written
    "kg 250" or "250 kg" cannot be told from the paper: it is left as printed."""
    def strong(c: str) -> bool:
        return bool(_LATIN.match(c) or _DIGIT.match(c))

    j = i
    while j < len(chars):
        c = chars[j]
        if strong(c):
            j += 1
            continue
        if j == i:
            break
        k = j  # what follows the punctuation and at most one space on each side of it
        while k < len(chars) and k - j < 4 and (chars[k] in _PUNCT or chars[k] == " "):
            k += 1
        between = "".join(chars[j:k])
        if k >= len(chars) or not strong(chars[k]) or between.count(" ") > 2:
            break
        ahead = "".join(chars[k:]).split(" ")[0]
        lettered = any(_LATIN.match(x) for x in chars[i:j]) or any(_LATIN.match(x) for x in ahead)
        if " " in between and not lettered:
            break  # "20 9": two numbers
        j = k
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


def read_line(gray: np.ndarray, latin: bool = True) -> tuple[str, float]:
    """One cropped line (grey, dark text on light) to its text in reading order and a confidence.
    latin: read the words in Latin letters inside the line with the standard reader."""
    return _read(gray, latin)[:2]


def _read(gray: np.ndarray, latin: bool = True) -> tuple[str, float, int]:
    """read_line, and how many Arabic-script letters the model was sure of."""
    from PIL import Image
    session = _load()
    h, w = gray.shape[:2]
    if session is None or h < 4 or w < 4:
        return "", 0.0, 0
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
    text, scores, places, last = [], [], [], 0
    per_step = x.shape[1] / max(len(best), 1) * h / HEIGHT  # one step of the model, in pixels of the crop
    for step, label in enumerate(best):
        if label != 0 and label != last:
            text.append(_labels.get(int(label), ""))
            scores.append(float(probs[label, step]))
            places.append((step + 0.5) * per_step - PAD * h / HEIGHT)
        elif label != 0:
            scores[-1] = max(scores[-1], float(probs[label, step]))
        last = label
    if not text:
        return "", 0.0, 0
    if latin:
        text, scores = _with_latin_words(gray, text, scores, places)
    sure = sum(1 for c, score in zip(text, scores) if score >= 0.85 and _ARABIC.match(c))
    return logical("".join(text)).strip(), float(np.mean(scores)), sure


def _gap_near(ink: np.ndarray, x: float, h: int) -> int:
    """The middle of the widest white gap near x: the model's places for letters are rough, and a
    word must be cut out between words, not through one."""
    lo, hi = max(int(x - 0.6 * h), 0), min(int(x + 0.6 * h), len(ink))
    best, start = (0, int(min(max(x, 0), len(ink)))), None
    for col in range(lo, hi + 1):
        if col < hi and not ink[col]:
            start = col if start is None else start
        elif start is not None:
            if col - start > best[0]:
                best = (col - start, (start + col) // 2)
            start = None
    return best[1]


def _with_latin_words(gray: np.ndarray, text: list, scores: list, places: list) -> tuple[list, list]:
    """The Jawi model knows a few capital letters only: "Utem" comes out as "C11". Where it read
    Latin letters or was unsure, the word on the paper is read by the standard reader, and that
    reading is taken when it is sure and in Latin letters."""
    h, w = gray.shape[:2]
    # The words are where the model itself put the spaces; each reaches halfway to its neighbours.
    tokens: list[list[int]] = []
    for k, c in enumerate(text):
        if c == " ":
            continue
        if tokens and tokens[-1][-1] == k - 1:
            tokens[-1].append(k)
        else:
            tokens.append([k])
    doubt: list[list] = []  # [first character, last character, the model read Latin letters here, token number]
    for number, inside in enumerate(tokens):
        inside = list(inside)
        # A Jawi word the model is sure of at either end stays: it does not always see the space before a Latin word.
        while inside and _ARABIC.match(text[inside[0]]) and scores[inside[0]] >= 0.8:
            inside.pop(0)
        while inside and _ARABIC.match(text[inside[-1]]) and scores[inside[-1]] >= 0.8:
            inside.pop()
        if not inside or all(_DIGIT.match(text[k]) or text[k] in _PUNCT for k in inside):
            continue  # a number is the Jawi model's own
        lettered = any(_LATIN.match(text[k]) for k in inside)
        if lettered or (len(inside) >= 2 and sum(scores[k] for k in inside) < LATIN_DOUBT * len(inside)):
            if doubt and lettered and doubt[-1][2] and doubt[-1][3] == number - 1 and doubt[-1][1] == tokens[number - 1][-1]:
                doubt[-1][1], doubt[-1][3] = inside[-1], number  # words side by side are read together: "Kuala Lumpur"
            else:
                doubt.append([inside[0], inside[-1], lettered, number])
    if not doubt:
        return text, scores
    from . import engine
    eng = engine._engine()
    if eng is None:
        return text, scores
    ink = (gray < (int(gray.min()) + int(gray.max())) / 2).any(axis=0)
    spans = []
    for first, last, lettered, _ in doubt[:6]:
        reach = 0.45 * h  # where no neighbour says otherwise
        x0 = (places[first - 1] + places[first]) / 2 if first else places[first] - reach
        x1 = (places[last] + places[last + 1]) / 2 if last + 1 < len(places) else places[last] + reach
        if first and text[first - 1] == " " and first >= 2:
            x0 = (places[first - 2] + places[first]) / 2 if places[first] - places[first - 2] < 3 * h else places[first] - reach
        if last + 1 < len(text) and text[last + 1] == " " and last + 2 < len(text):
            x1 = (places[last] + places[last + 2]) / 2 if places[last + 2] - places[last] < 3 * h else places[last] + reach
        spans.append((_gap_near(ink, x0, h), _gap_near(ink, x1, h), lettered, places[first], places[last]))
    for x0, x1, lettered, left, right in spans:
        if x1 - x0 < 0.5 * h:
            continue
        crop = np.ascontiguousarray(gray[:, x0:x1])
        try:
            with engine._run_lock:
                got = eng(np.stack([crop] * 3, axis=-1), use_det=False, use_cls=False, use_rec=True, text_score=0.0)
            word, sure = (got.txts[0].strip(), float(got.scores[0])) if got is not None and got.txts else ("", 0.0)
        except Exception:  # noqa: BLE001 - the standard reader could not take the crop: keep the Jawi reading
            log.debug("a Latin word could not be read again", exc_info=True)
            continue
        word = " ".join(word.split())
        log.debug("a doubtful word read again: %r at %.2f", word, sure)
        # Where the Jawi model saw no Latin letter and was only unsure, the standard reader must be
        # very sure: it read an unsure Jawi word as "aslo" at 0.9.
        if sure < (LATIN_SURE if lettered else 0.97) or len(_LATIN.findall(word)) < 2 or not _LATIN_WORD.match(word):
            continue
        if len(word) < 0.4 * (x1 - x0) / (0.55 * h):
            continue  # far too few letters for the width: it read a part and would drop the rest
        inside = [k for k, at in enumerate(places) if left - 1e-6 <= at <= right + 1e-6]
        if not inside:
            continue
        first, end = inside[0], inside[-1] + 1
        new = ([" "] if first and text[first - 1] != " " else []) + list(word) + ([" "] if end < len(text) and text[end] != " " else [])
        at = list(np.linspace(left, right, len(new)))
        text[first:end], scores[first:end], places[first:end] = new, [sure] * len(new), at
    return text, scores


def share(text: str) -> float:
    """How much of a text's letters are Arabic script."""
    letters = _LETTER.findall(text)
    return len(_ARABIC.findall(text)) / len(letters) if letters else 0.0


def is_jawi(text: str, confidence: float, sure: int = 0) -> bool:
    """sure: Arabic-script letters the model read with confidence. A line that is half names in
    Latin letters is still a Jawi line when enough of the rest was read surely."""
    letters = len(_ARABIC.findall(text))
    if sure >= MIXED_LETTERS and share(text) >= MIXED_SHARE and confidence >= MIN_CONFIDENCE:
        return True
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
        text, conf, sure = _read(_crop(gray, line["box"]))
        ours = is_jawi(text, conf, sure)
        mixed = sure >= MIXED_LETTERS
        # The standard reader knows Latin letters, digits and Chinese. Where it is sure of what it
        # read and that is not Arabic script, the line is its own.
        theirs = line["confidence"] >= 0.9 and len(text) < 2 * len(line["text"]) and share(line["text"]) < 0.5
        if ours and not (theirs and conf < line["confidence"] and not mixed):
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
        text, conf, sure = _read(_crop(gray, box))
        if is_jawi(text, conf, sure):
            kept.append({"text": text, "confidence": round(conf, 4), "box": box, "script": "jawi"})
        else:  # together they read worse than apart: keep the pieces
            kept.extend(group)
    return kept
