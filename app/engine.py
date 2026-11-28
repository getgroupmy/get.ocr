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
BLURRY, DARK = 40.0, 50.0  # below these a picture is reported as blurry or dark; first guesses, not tuned
_STROKES = "一丨丿乙亅｜|—–-_~^`'‘’,.·"  # alone on a line: the edge of a box or a bracket, a rule, a speck
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


def sideways(lines: list[dict]) -> bool:
    """Do the lines run up or down the picture, as on a page photographed on its side?"""
    tall = wide = 0
    for ln in lines:
        xs, ys = [p[0] for p in ln["box"]], [p[1] for p in ln["box"]]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if len(ln["text"]) >= 4:
            tall, wide = tall + (h > 1.5 * w), wide + (w > 1.5 * h)
    return tall >= 1 and tall > 2 * wide


def on_their_head(img: np.ndarray, lines: list[dict]) -> Optional[float]:
    """The share of the longest lines that stand on their head, asked of the reader's own line
    classifier; None when it cannot be asked (too few lines, or the classifier is not there)."""
    eng = _engine()
    longest = sorted((ln for ln in lines if len(ln["text"]) >= 4), key=lambda ln: -len(ln["text"]))[:12]
    if eng is None or len(longest) < 2:
        return None
    crops = []
    for ln in longest:
        xs, ys = [int(p[0]) for p in ln["box"]], [int(p[1]) for p in ln["box"]]
        crop = img[max(min(ys), 0):max(ys), max(min(xs), 0):max(xs)]
        if crop.size:
            crops.append(np.ascontiguousarray(crop))
    try:
        with _run_lock:
            labels = [label for label, _ in eng._load_cls_model()(crops).cls_res]
    except Exception:  # noqa: BLE001 - an internal of the library: without it, pages are read as they come
        log.debug("line orientation could not be checked", exc_info=True)
        return None
    return sum(label == "180" for label in labels) / len(labels) if labels else None


def upside_down(img: np.ndarray, lines: list[dict]) -> bool:
    """Do most lines stand on their head?"""
    share = on_their_head(img, lines)
    # On 50 real scanned forms the classifier called up to 4 lines in 10 upside down on an upright
    # page, and at least 7 in 10 on a turned one: so ask for a clear majority.
    return share is not None and share >= 0.7


def _weight(lines: list[dict]) -> float:
    return sum(ln["confidence"] * len(ln["text"]) for ln in lines)


def _may_be_on_its_head(lines: list[dict], jawi: int) -> bool:
    """Asked for Jawi and the page did not read as sure Jawi: is it worth reading it turned? Yes
    when some Jawi was read, when next to nothing was, or when what was read is unsure."""
    return bool(jawi) or len(lines) < 3 or sum(ln["confidence"] for ln in lines) < 0.85 * len(lines)


TILT_FROM, TILT_TO = 1.5, 40.0  # degrees: less is left alone, more is not a tilted page but something else


def tilt(lines: list[dict]) -> float:
    """By how many degrees the lines of a page slope, clockwise positive; 0 when they are level or
    do not agree. Taken from the long lines: a short one says little about its slope."""
    slopes = []
    for ln in lines:
        (x0, y0), (x1, y1), _, (x3, y3) = ln["box"]
        if np.hypot(x1 - x0, y1 - y0) > 4 * max(np.hypot(x3 - x0, y3 - y0), 1.0) and len(ln["text"]) >= 5:
            slopes.append(float(np.degrees(np.arctan2(y1 - y0, x1 - x0))))
    if len(slopes) < 3:
        return 0.0
    middle = float(np.median(slopes))
    spread = float(np.percentile(slopes, 75) - np.percentile(slopes, 25))
    return middle if TILT_FROM <= abs(middle) <= TILT_TO and spread <= 3.0 else 0.0


def straighten(img: np.ndarray, degrees: float) -> np.ndarray:
    """The picture turned back by its tilt, on a canvas large enough to keep its corners."""
    import cv2
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def read_upright(img: np.ndarray, script: Optional[str] = None) -> tuple[list[dict], np.ndarray, float]:
    """Read a picture, turned upright first when it lies on its side or its head, and straightened
    when it is tilted. Returns the lines, the picture as it was read, and how far it was turned
    clockwise: 0, 90, 180 or 270 degrees, or a number in between for a tilted page (356.5 is a
    page that leaned 3.5 degrees clockwise)."""
    first = read(img, script)
    lean = tilt(first) if not sideways(first) else 0.0
    if lean:
        # The lines are read well enough at a slope, but rows that slope run into each other when
        # they are put in order, a table's columns no longer stand under each other, and the check
        # for a page on its head is thrown. So the page is made level before anything else.
        level = straighten(img, lean)
        lines, upright, turned = _read_quarters(level, script)
        if lines and _weight(lines) >= 0.9 * _weight(first):
            return lines, upright, round((turned - lean) % 360, 1)
    lines, upright, turned = _read_quarters(img, script, first)
    lean = tilt(lines) if turned in (90, 270) else 0.0  # a page on its side that also leans
    if lean:
        level = straighten(upright, lean)
        again = read(level, script)
        if again and _weight(again) >= 0.9 * _weight(lines):
            return again, level, round((turned - lean) % 360, 1)
    return lines, upright, turned


def _read_quarters(img: np.ndarray, script: Optional[str] = None, lines: Optional[list] = None) -> tuple[list[dict], np.ndarray, int]:
    """Read a picture, turning it first when it lies on its side or its head. `lines`: the picture
    as already read, to save reading it again."""
    lines = read(img, script) if lines is None else lines
    if not sideways(lines):
        # An upside-down page is read line by line all the same, but the lines come out bottom first.
        ours = [ln for ln in lines if ln.get("script")]
        jawi = len(ours)
        if 2 * jawi > len(lines) and sum(ln["confidence"] for ln in ours) >= 0.8 * jawi:
            # The line classifier was trained on Latin and Chinese print and called a real upright
            # Jawi newspaper page upside down. A page that reads as sure Jawi is upright.
            return lines, img, 0
        if not script or not _may_be_on_its_head(lines, jawi):
            if upside_down(img, lines):
                turned = np.ascontiguousarray(np.rot90(img, 2))
                got = read(turned, script)
                if not upside_down(turned, got):  # turned, it must read as upright, or the first call was wrong
                    return got, turned, 180
            return lines, img, 0
        # Measured on 8 real Jawi newspaper pages and 12 made ones: on its head a page gave a few
        # unsure Jawi lines and unsure scraps of Latin; upright, 5 to 30 times the sure text.
        turned = np.ascontiguousarray(np.rot90(img, 2))
        got = read(turned, script)
        if sum(1 for ln in got if ln.get("script")) > jawi and _weight(got) > 1.3 * _weight(lines):
            return got, turned, 180
        return lines, img, 0
    tried = []
    for quarter, angle in ((1, 270), (3, 90)):  # np.rot90 turns anticlockwise
        turned = np.ascontiguousarray(np.rot90(img, quarter))
        got = read(turned, script)
        score = sum(ln["confidence"] * len(ln["text"]) for ln in got) if not sideways(got) else 0
        tried.append((score, got, turned, angle))
    # Each line is read the right way round whichever quarter the picture is turned by, so both turns read
    # about as well: one of them leaves the page on its head, its lines in the wrong places. The turn whose
    # lines the classifier calls upright is the page's, where the two differ clearly and both were read.
    (a, b) = tried
    if min(a[0], b[0]) > 0.8 * max(a[0], b[0]) > 0:
        shares = [on_their_head(t[2], t[1]) for t in tried]
        if None not in shares and abs(shares[0] - shares[1]) >= 0.3:
            upright = a if shares[0] < shares[1] else b
            return upright[1], upright[2], upright[3]
    best = max([(0.0, lines, img, 0)] + tried, key=lambda t: t[0])  # the picture as it came, if neither turn reads as a page
    return best[1], best[2], best[3]


def quality(img: np.ndarray) -> dict:
    """How fit a picture is for reading: its sharpness (variance of the edges,
    higher is sharper), its brightness (0 to 255) and what is wrong with it."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    scale = 1000 / max(h, w)
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else gray
    sharpness = float(cv2.Laplacian(small, cv2.CV_64F).var())
    brightness = float(small.mean())
    issues = [name for name, bad in (("blurry", sharpness < BLURRY), ("dark", brightness < DARK),
                                     ("low_resolution", min(h, w) < 500)) if bad]
    return {"sharpness": round(sharpness, 1), "brightness": round(brightness, 1), "issues": issues}


def read(img: np.ndarray, script: Optional[str] = None) -> list[dict]:
    """Text lines on a BGR image in reading order. Each line has its text, the
    model's confidence (0 to 1) and its four corners in image pixels, clockwise
    from the top left. With a script ("jawi", "arabic", "tamil", "thai") the lines in that script
    are read by the model for it."""
    eng = _engine()
    if eng is None:
        raise RuntimeError("OCR engine is not available")
    original = img
    if get_settings().even_lighting:
        evened = even_lighting(img)
        img = img if evened is None else evened
    with _run_lock:
        # The library remembers the switches of its last call, so every call names them all.
        r = eng(img, use_det=True, use_cls=True, use_rec=True, text_score=0.5)
        # It gives no line at all where it could read nothing, which is every Jawi line: ask
        # again for only where the lines are.
        where = eng(img, use_det=True, use_cls=False, use_rec=False).boxes if script else None
    if (r is None or r.txts is None) and (where is None or not len(where)):
        return []
    boxes, txts, scores = (r.boxes, r.txts, r.scores) if r is not None and r.txts is not None else ([], [], [])
    floor = get_settings().min_confidence
    found = [{"text": txt, "confidence": round(float(score), 4),
              "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box]}
             for box, txt, score in zip(boxes, txts, scores)]
    if script:
        from . import jawi, scripts
        unread = [[[round(float(x), 1), round(float(y), 1)] for x, y in box] for box in (where if where is not None else [])]
        unread = [box for box in unread if not any(_same_place(box, ln["box"]) for ln in found)]
        everything = found + [{"text": "", "confidence": 0.0, "box": box} for box in unread]
        if script == "detect":  # not told which script: ask the lines the standard reader could not read
            script = scripts.detect(original, everything)
        found = jawi.reread(img, everything) if script == "jawi" else scripts.reread(img, everything, script) if script else found
        # a scrap the standard reader made of a piece of a line that was now read whole
        whole = [scripts._span(ln["box"]) for ln in found if ln.get("script")]
        found = [ln for ln in found if ln.get("script") or not any(scripts._inside(scripts._span(ln["box"]), big) for big in whole)]
    lines = []
    for line in found:
        txt, score = line["text"], line["confidence"]
        if line.get("script"):
            lines.append(line)
            continue
        if score < floor or not txt.strip():
            continue
        if len(txt.strip()) == 1 and (score < LONE_CHARACTER or txt.strip() in _STROKES):
            continue  # a speck or half a bracket read as a character
        lines.append(line)
    from . import lists
    lines = lists.without_bracket_scraps(original, lines)  # a printed [ ] read as "L" and "1"
    lines = lists.leading_quantities(original, lines)  # "1  Siu Mai King": the lone 1 the line finder missed
    return [line for row in _rows(lines) for line in row]


def _same_place(a: list, b: list) -> bool:
    """Is the line in box a the one already read in box b? Measured against a's own size: a scrap
    that was read inside a long unread line does not make that line read."""
    ax0, ay0, ax1, ay1 = min(p[0] for p in a), min(p[1] for p in a), max(p[0] for p in a), max(p[1] for p in a)
    bx0, by0, bx1, by1 = min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b)
    both = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))
    return both > 0.7 * max((ax1 - ax0) * (ay1 - ay0), 1e-6)


def _rows(lines: list[dict]) -> list[list[dict]]:
    """Group lines that sit on the same row, rows top to bottom, each left to right (a row
    that is mostly Jawi: right to left)."""
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

    def in_order(row: list[dict]) -> list[dict]:
        row = sorted(row, key=lambda ln: min(p[0] for p in ln["box"]))
        return row[::-1] if 2 * sum(1 for ln in row if ln.get("script") in ("jawi", "arabic")) > len(row) else row
    return [in_order(row) for _, _, row in rows]


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
