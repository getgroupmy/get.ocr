"""The reader: PP-OCR (PaddleOCR models) through RapidOCR, on the CPU.

Engine: onnxruntime (Mac, x86 with AVX) or openvino (Celeron NAS without AVX),
set with OCR_ENGINE. One page is read at a time; each reading already uses
every core."""
from __future__ import annotations

import difflib
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


def ground_colour(img: np.ndarray, lines: list[dict]) -> Optional[int]:
    """Which of a picture's three colours the ground under its text is brightest in (0 blue, 1 green,
    2 red), where it is clearly one of them; None for a white or grey ground. It is measured where
    the lines stand, not over the whole picture: a blue card photographed on a brown table is blue."""
    weight = np.zeros(3)
    for ln in lines:
        xs, ys = [int(p[0]) for p in ln["box"]], [int(p[1]) for p in ln["box"]]
        crop = img[max(min(ys), 0):max(ys), max(min(xs), 0):max(xs)]
        if crop.size < 30:
            continue
        dots = crop.reshape(-1, 3).astype(float)
        light = dots[dots.sum(axis=1) >= np.median(dots.sum(axis=1))]  # the ground: the lighter half of the line's box
        weight += np.median(light, axis=0) * len(ln["text"])
    if not weight.any():
        return None
    weight /= weight.max()
    first, second = np.argsort(weight)[-1], np.argsort(weight)[-2]
    return int(first) if weight[first] - weight[second] >= 0.05 else None


def _same_line(a: list, b: list) -> bool:
    """Does box b cover most of box a without being much larger: the same line of the picture, read twice?"""
    ax0, ay0, ax1, ay1 = min(p[0] for p in a), min(p[1] for p in a), max(p[0] for p in a), max(p[1] for p in a)
    bx0, by0, bx1, by1 = min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b)
    w, h = min(ax1, bx1) - max(ax0, bx0), min(ay1, by1) - max(ay0, by0)
    size = max((ax1 - ax0) * (ay1 - ay0), 1)
    return w > 0 and h > 0 and w * h >= 0.6 * size and (bx1 - bx0) * (by1 - by0) <= 1.8 * size


def surer(first: list[dict], second: list[dict]) -> list[dict]:
    """The first reading of a picture, with a line replaced by a second reading's where that is the
    same line read more surely. The second reading may add to a line, never take from it: it must
    hold at least as many characters, be like the first, and be clearly surer, or longer by two
    characters and as sure. Where the two differ only in their spaces, the one with more of them is
    taken (a reader loses spaces, it does not make them). No line is added or removed."""
    out = []
    for a in first:
        same = [b for b in second if _same_line(a["box"], b["box"])]
        if len(same) == 1:
            b = same[0]
            ta, tb, ca, cb = a["text"].strip(), b["text"].strip(), a["confidence"], b["confidence"]
            bare_a, bare_b = ta.replace(" ", ""), tb.replace(" ", "")
            if bare_a == bare_b:
                better = len(tb) > len(ta) and cb >= 0.9
            else:
                better = len(bare_b) >= len(bare_a) and difflib.SequenceMatcher(None, ta.upper(), tb.upper()).ratio() >= 0.6 \
                    and (cb >= ca + 0.03 or (len(bare_b) >= len(bare_a) + 2 and cb >= max(ca - 0.02, 0.9)))
            if better:
                out.append({**a, "text": tb, "confidence": cb, "box": b["box"]})
                continue
        out.append(a)
    return out


# A stamp or a watermark laid across a page: told by its slope and its ink together. Either alone is print of
# the page itself: a red serial number runs level, a second paper under the first lies at an angle in black.
STAMP_SLOPE = 3.0  # degrees off the page's own lines, from which a line in another ink is laid across the page
STAMP_INK = 20.0  # degrees between what two inks take from the ground: black print and a red stamp are 30 to 40 apart
STAMP_TOUCH = 0.03  # the share of a line's box in the stamp's ink, from which the stamp runs into the line


def _inside(img: np.ndarray, line: dict) -> Optional[np.ndarray]:
    """The picture's dots inside a line's box, as rows of three colours; None for a box too small to tell by."""
    box = np.array(line["box"], dtype=np.float32)
    x0, y0 = np.maximum(np.floor(box.min(axis=0)).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(box.max(axis=0)).astype(int), [img.shape[1], img.shape[0]])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    import cv2
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillPoly(mask, [np.round(box - [x0, y0]).astype(np.int32)], 1)
    dots = img[y0:y1, x0:x1].reshape(-1, 3)[mask.reshape(-1) > 0].astype(np.float32)
    return dots if len(dots) >= 30 else None


def inks(img: np.ndarray, line: dict) -> Optional[tuple]:
    """(the ground under a line, the ink it is written in), each a colour: the middle of the lighter half of
    its box, and the middle of the fifth of its box furthest from that."""
    dots = _inside(img, line)
    if dots is None:
        return None
    ground = np.median(dots[dots.sum(axis=1) >= np.median(dots.sum(axis=1))], axis=0)
    far = np.linalg.norm(dots - ground, axis=1)
    return ground, np.median(dots[far >= np.percentile(far, 80)], axis=0)


def _takes(ground: np.ndarray, ink: np.ndarray) -> Optional[np.ndarray]:
    """What an ink takes from the ground it stands on, as a direction among the three colours: any dark print
    takes about evenly from all three, a red ink leaves the red and takes the others. None for an ink hardly
    other than its ground."""
    took = ground - ink
    size = float(np.linalg.norm(took))
    return took / size if size > 25 else None


def _share(dots: Optional[np.ndarray], ink: np.ndarray, ground: np.ndarray) -> float:
    """The share of a box's dots that are in an ink: near it, and nearer to it than to the ground."""
    if dots is None:
        return 0.0
    near = np.linalg.norm(dots - ink, axis=1)
    return float(((near < 0.5 * np.linalg.norm(ink - ground)) & (near < np.linalg.norm(dots - ground, axis=1))).mean())


def _mid_in(inner: list, outer: list) -> bool:
    """Does the middle of box `inner` lie inside box `outer`?"""
    import cv2
    x, y = np.array(inner, dtype=np.float32).mean(axis=0)
    return cv2.pointPolygonTest(np.array(outer, dtype=np.float32), (float(x), float(y)), False) >= 0


def _in_place(a: dict, b: dict) -> bool:
    """Two lines of two readings that stand in one place: the same line, or one inside the other."""
    return _same_line(a["box"], b["box"]) or _same_line(b["box"], a["box"]) or _mid_in(a["box"], b["box"]) or _mid_in(b["box"], a["box"])


def without_ink(img: np.ndarray, ground: np.ndarray, stamp: np.ndarray, print_: np.ndarray) -> Optional[np.ndarray]:
    """The picture seen in the one mix of its three colours in which the stamp's ink is as light as the ground
    under it, whatever its strength, while the print stays dark. None where there is no such mix: the stamp's
    ink lies between the ground and the print (a grey stamp on a black and white copy)."""
    across, down = stamp - ground, ground - print_
    mix = down - (down @ across) / max(float(across @ across), 1e-6) * across
    if mix @ down <= 0.2 * np.linalg.norm(mix) * np.linalg.norm(down):
        return None
    seen = (img.astype(np.float32) @ mix - print_ @ mix) / (ground @ mix - print_ @ mix)
    return np.clip(20 + 215 * seen, 0, 255).astype(np.uint8)


def stamp_lines(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """The lines laid across a page in another ink than its print: off the direction the page's own lines have
    at their place by STAMP_SLOPE degrees or more, and written in an ink that takes from the ground another
    way than the page's print does."""
    from . import headings
    offs = headings.off_trend(lines)
    laid = [ln for ln in lines if STAMP_SLOPE <= offs.get(id(ln), 0.0) < headings.UPRIGHT]
    if not laid:
        return []
    own = sorted((ln for ln in lines if offs.get(id(ln), 90.0) < headings.AGREE), key=lambda ln: -len(ln["text"]))[:12]
    own = [found for found in (inks(img, ln) for ln in own) if found is not None and _takes(*found) is not None]
    if len(own) < 3:
        return []
    paper = np.median(np.array([ground for ground, _ in own]), axis=0)
    usual = np.median(np.array([_takes(*found) for found in own]), axis=0)
    usual /= max(float(np.linalg.norm(usual)), 1e-6)
    out = []
    for ln in laid:
        found = inks(img, ln)
        took = _takes(*found) if found is not None else None
        # an ink on the page's own ground: not print on another thing in the picture (a pack beside the paper, the
        # maker's mark etched in a windscreen), which stands on a ground of its own or is lighter than it
        if took is not None and np.linalg.norm(found[0] - paper) <= 70 and float((found[0] - found[1]).sum()) > 0 \
                and np.degrees(np.arccos(np.clip(float(took @ usual), -1.0, 1.0))) >= STAMP_INK:
            out.append(ln)
    return out


def under_a_stamp(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """The lines of a picture with what a stamp or a watermark across it did to them put right as far as it can be.

    A stamp is laid over a page after it was printed: an app's "GET RIDE / Registration" in red across
    the photo of a card it keeps, a rubber stamp, "for this use only" written across a copy. Its lines
    are marked `across` (a reader by position leaves them out; the text keeps them). Where it runs
    into the print, the reader gives print and stamp as one line ("LIM AH Kowstration"), which no rule
    on whole lines can take apart: the picture is read once more with the stamp's ink taken out, and
    that reading is taken wherever the stamp's ink touches print. Such a line is marked `under_stamp`:
    what stood under a faint stamp reads well, what stood under solid ink is gone where the ink
    was, and the caller is told which lines those are. A stamp's line that holds print which could
    not be read apart from it is marked `over_print` as well."""
    from . import headings
    laid = stamp_lines(img, lines)
    if not laid:
        return lines
    marked = {id(ln) for ln in laid}
    found = [inks(img, ln) for ln in laid]
    ground, stamp = np.median([f[0] for f in found], axis=0), np.median([f[1] for f in found], axis=0)
    offs = headings.off_trend(lines)
    own = [inks(img, ln) for ln in sorted((ln for ln in lines if offs.get(id(ln), 90.0) < headings.AGREE), key=lambda ln: -len(ln["text"]))[:12]]
    print_ = np.median([f[1] for f in own if f is not None], axis=0)

    def touched(ln: dict) -> float:
        return _share(_inside(img, ln), stamp, ground)

    def printed(ln: dict) -> bool:  # it holds print of its own, not only a piece of the stamp
        return _share(_inside(img, ln), print_, ground) >= 0.04

    # a short piece of the stamp, read on its own beside its other lines: too short to tell a slope by, in the
    # stamp's ink and with no print of its own
    xs, ys = [p[0] for ln in laid for p in ln["box"]], [p[1] for ln in laid for p in ln["box"]]
    tall = max(min(float(np.linalg.norm(np.subtract(ln["box"][3], ln["box"][0]))), float(np.linalg.norm(np.subtract(ln["box"][1], ln["box"][0])))) for ln in laid)
    near = [[min(xs) - tall, min(ys) - tall], [max(xs) + tall, min(ys) - tall], [max(xs) + tall, max(ys) + tall], [min(xs) - tall, max(ys) + tall]]
    for ln in lines:
        if id(ln) not in marked and id(ln) not in offs and _mid_in(ln["box"], near) and touched(ln) >= 0.1 and not printed(ln):
            marked.add(id(ln))
    view = without_ink(img, ground, stamp, print_)
    if view is None:
        return [{**ln, "across": True} if id(ln) in marked else ln for ln in lines]
    again = read(np.stack([view] * 3, axis=2))
    run_into = [ln for ln in lines if id(ln) not in marked and touched(ln) >= STAMP_TOUCH]  # the stamp's ink touches them
    stand = {id(ln) for ln in run_into} | marked
    sound = [ln for ln in lines if id(ln) not in stand]  # these stand as they were read
    level = headings.off_trend(sound + again)
    # the print read with the stamp's ink taken out: sure, level with the page, holding print of its own, and not a
    # line that stands as it was read. Each belongs to the line it was read again as, else to the stamp's line it
    # lies under, else it is print the first reading did not find under the stamp at all.
    under: dict = {}
    for c in again:
        if c["confidence"] < 0.9 or level.get(id(c), 0.0) >= STAMP_SLOPE or not printed(c) or any(_in_place(ln, c) for ln in sound):
            continue
        home = next((ln for ln in run_into if _in_place(ln, c)), None) or next((ln for ln in lines if id(ln) in marked and _mid_in(c["box"], ln["box"])), None)
        if home is not None or touched(c) >= STAMP_TOUCH:
            under.setdefault(id(home), []).append(c)
    out = []
    for ln in lines:
        found = under.get(id(ln)) or []
        if id(ln) in marked:
            # the stamp's own line, or print and stamp read as one: the print under it, if any was read; else the
            # line as the stamp's, with a word on whether it holds print that could not be read apart from it
            out += [{**c, "under_stamp": True} for c in found] or [{**ln, "across": True, **({"over_print": True} if printed(ln) else {})}]
        elif id(ln) not in stand:
            out.append(ln)
        elif not found or (len(found) == 1 and found[0]["confidence"] < ln["confidence"]) \
                or "".join(c["text"] for c in found).replace(" ", "") == ln["text"].replace(" ", ""):
            out.append({**ln, "under_stamp": True})  # nothing surer was read in its place: it stands, and is told of
        else:
            out += [{**c, "under_stamp": True} for c in found]
    out += [{**c, "under_stamp": True} for c in under.get(id(None), [])]
    return [ln for row in _rows(out) for ln in row]


def read_upright(img: np.ndarray, script: Optional[str] = None) -> tuple[list[dict], np.ndarray, float]:
    """Read a picture, turned upright first when it lies on its side or its head, and straightened
    when it is tilted. Returns the lines, the picture as it was read, and how far it was turned
    clockwise: 0, 90, 180 or 270 degrees, or a number in between for a tilted page (356.5 is a
    page that leaned 3.5 degrees clockwise).

    Where the text stands on a coloured ground and a line was read unsurely, the picture is read
    once more in that colour alone and the surer reading of each line is kept. A card's hologram
    lies over its print in colours; in the colour of the card itself (blue for an identity card) the
    hologram is as light as the card and the dark print stands clear of it. A real card's name read
    "SATHISKUNAR AR" in full colour reads "SATHISKUMAR AL" in blue."""
    lines, upright, turned = _read_upright(img, script)
    if not script and any(ln["confidence"] < 0.97 and len(ln["text"]) >= 3 for ln in lines):
        colour = ground_colour(upright, lines)
        if colour is not None:
            single = np.ascontiguousarray(upright[:, :, colour])
            lines = surer(lines, read(np.stack([single] * 3, axis=2)))
    if not script:
        lines = under_a_stamp(upright, lines)
    return lines, upright, turned


def _read_upright(img: np.ndarray, script: Optional[str] = None) -> tuple[list[dict], np.ndarray, float]:
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
