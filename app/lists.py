"""What a printed list of items carries beside its lines of text: the quantity standing alone in
front of an item, a pen stroke through it, and the empty [ ] printed at the end of a row.

The line finder misses a lone "1" set apart from the item's name, more so with a pen stroke over
it. Here the place in front of each line is looked at directly."""
from __future__ import annotations

import logging
import re

import numpy as np

log = logging.getLogger("ocr.lists")
REACH = 3.5      # how far left of a line its quantity may stand, in line heights
SURE = 0.6       # a recovered quantity is kept when the reader is at least this sure of the digit
PEN_COLOUR = 12  # how far from the print's own colour ink must be to count as a pen
ALIKE = 0.6      # a digit under a pen stroke is taken when it fits a clean one at least this well
NEAR_ROWS = 4    # a struck quantity is looked for this many line heights from a known one, no further
COLUMN = 0.7     # quantities of one list stand under each other: within this many line heights
_QUANTITY = re.compile(r"^\d{1,3}$")
_STARTS_WITH_QUANTITY = re.compile(r"^(\d{1,3})\s+\S")


def _span(box: list) -> tuple:
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _ink(img: np.ndarray, gray: np.ndarray, box: tuple, like: tuple) -> tuple:
    """In one place of the picture: the printed ink and the pen ink, as two masks. Pen ink is ink
    of another colour than the print of the line beside it (`like`, that line's box): thermal
    print photographs brownish and a blue pen does not, whatever the light on the paper."""
    import cv2
    x0, y0, x1, y1 = box
    part = gray[y0:y1, x0:x1].astype(np.int16)
    dark = part < float(np.percentile(part, 80)) - 45
    lx0, ly0, lx1, ly1 = (int(v) for v in like)
    text = gray[ly0:ly1, lx0:lx1].astype(np.int16)
    printed = text < float(np.percentile(text, 80)) - 45
    if printed.sum() < 20 or not dark.any():
        return dark, np.zeros(dark.shape, bool)
    lab = cv2.cvtColor(img[ly0:ly1, lx0:lx1], cv2.COLOR_BGR2LAB).astype(np.int16)
    a0, b0 = int(np.median(lab[..., 1][printed])), int(np.median(lab[..., 2][printed]))
    here = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2LAB).astype(np.int16)
    pen = dark & (np.hypot(here[..., 1] - a0, here[..., 2] - b0) > PEN_COLOUR)
    pen = cv2.morphologyEx(pen.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)) > 0  # not single specks
    near = cv2.dilate(pen.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return dark & ~near, pen


def _slanted_line(gray: np.ndarray, box: tuple, height: float) -> np.ndarray:
    """A pen stroke of the print's own colour: a straight slanted line longer than any stroke of a
    printed letter. Returns where it runs inside the box."""
    import cv2
    x0, y0, x1, y1 = box
    part = gray[y0:y1, x0:x1]
    mask = np.zeros(part.shape, np.uint8)
    if part.size == 0:
        return mask > 0
    dark = (part.astype(np.int16) < float(np.percentile(part, 80)) - 45).astype(np.uint8) * 255
    found = cv2.HoughLinesP(dark, 1, np.pi / 180, threshold=int(1.2 * height), minLineLength=int(1.5 * height),
                            maxLineGap=int(0.25 * height))
    for ax, ay, bx, by in (found.reshape(-1, 4) if found is not None else []):
        slant = abs(np.degrees(np.arctan2(by - ay, bx - ax))) % 180
        if 20 <= min(slant, 180 - slant) <= 75:  # not a ruled line, not the stem of a letter
            cv2.line(mask, (int(ax), int(ay)), (int(bx), int(by)), 255, max(int(0.14 * height), 2))
    return (mask > 0) & (cv2.dilate(dark, np.ones((3, 3), np.uint8)) > 0)


def _is_stroke(pen: np.ndarray, height: float) -> bool:
    """A pen stroke is long; a speck of colour at the edge of a letter is not."""
    if not pen.any():
        return False
    ys, xs = np.nonzero(pen)
    return float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min())) > 0.7 * height and len(xs) > 0.1 * height * height


def _text_height(ln: dict) -> float:
    """The height of the letters: the reader's boxes are drawn generously round a line."""
    x0, y0, x1, y1 = _span(ln["box"])
    return min(y1 - y0, 1.9 * (x1 - x0) / max(len(ln["text"]), 1))


def _read_digit(eng, crop: np.ndarray) -> tuple:  # noqa: ANN001
    from . import engine
    try:
        with engine._run_lock:
            got = eng(np.ascontiguousarray(crop), use_det=False, use_cls=False, use_rec=True, text_score=0.0)
        text, score = (got.txts[0].strip(), float(got.scores[0])) if got is not None and got.txts else ("", 0.0)
    except Exception:  # noqa: BLE001 - the reader could not take the crop
        log.debug("the place in front of a line could not be read", exc_info=True)
        return "", 0.0
    if len(text) == 1:
        text = text.replace("l", "1").replace("I", "1").replace("|", "1")
    return text, score


def _alike(cell: np.ndarray, hidden: np.ndarray, pattern: np.ndarray) -> float:
    """How well a digit seen cleanly elsewhere on the slip fits this place, judged only on the
    pixels the pen does not cover, at the best of a few small shifts. 1 is a perfect fit."""
    th, tw = pattern.shape
    best = 0.0
    for dy in range(cell.shape[0] - th + 1):
        for dx in range(cell.shape[1] - tw + 1):
            seen = ~hidden[dy:dy + th, dx:dx + tw]
            if seen.mean() < 0.5:
                continue
            a = cell[dy:dy + th, dx:dx + tw][seen].astype(np.float32)
            b = pattern[seen].astype(np.float32)
            a, b = a - a.mean(), b - b.mean()
            norm = float(np.sqrt((a * a).sum() * (b * b).sum()))
            if norm > 0:
                best = max(best, float((a * b).sum()) / norm)
    return best


def leading_quantities(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """Put back the quantity that stands alone in front of an item ("1  Siu Mai King") where the
    reader gave only the name, and mark a line "struck" when a coloured pen stroke crosses its
    quantity. Nothing changes unless a quantity column shows: a digit in front of a line under the
    quantity another line was read with, or under the digit in front of another line."""
    import cv2

    from . import engine
    if len(lines) < 2 or img.ndim != 3:
        return lines
    eng = engine._engine()
    if eng is None:
        return lines
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    spans = [_span(ln["box"]) for ln in lines]
    heights = [_text_height(ln) for ln in lines]
    mids = [(sp[1] + sp[3]) / 2 for sp in spans]

    def bare(i: int) -> bool:  # a line that may have lost its quantity
        ln = lines[i]
        return heights[i] >= 8 and len(ln["text"]) >= 3 and not ln["text"][0].isdigit() and not ln.get("script")

    def text_of(i: int) -> tuple:  # where the line's own print is, for its colour
        return spans[i][0], mids[i] - 0.5 * heights[i], spans[i][2], mids[i] + 0.5 * heights[i]

    # 1. a digit standing clear in front of a line
    found: dict = {}  # line number -> (digit, score, left, right)
    for i, (x0, y0, x1, y1) in enumerate(spans):
        if not bare(i):
            continue
        h, mid = heights[i], mids[i]
        wx0, wx1 = int(max(x0 - REACH * h, 0)), int(x0 - 0.1 * h)
        wy0, wy1 = int(max(mid - 0.6 * h, 0)), int(min(mid + 0.6 * h, H))
        if wx1 - wx0 < 0.5 * h:
            continue
        if any(j != i and sx1 > wx0 + 0.3 * h and sx0 < wx1 and abs(mids[j] - mid) < 0.5 * h
               for j, (sx0, _, sx1, _) in enumerate(spans)):
            continue  # something was read there already
        # the paper may end inside the window: stop where the table or the board behind it begins
        solid = (gray[wy0:wy1, wx0:wx1] < 110).mean(axis=0) > 0.6
        if solid.any():
            wx0 += int(np.nonzero(solid)[0][-1]) + 3
            if wx1 - wx0 < 0.5 * h:
                continue
        printed, _ = _ink(img, gray, (wx0, wy0, wx1, wy1), text_of(i))
        count, _, stats, _ = cv2.connectedComponentsWithStats(printed.astype(np.uint8), connectivity=8)
        # as tall as a letter and no taller: a ruled line of a form runs on past the top and bottom
        digits = [(int(st[0]), int(st[1]), int(st[2]), int(st[3])) for st in stats[1:count]
                  if 0.5 * h <= st[3] <= 1.05 * h and st[2] <= 0.9 * h and st[1] > 0 and st[1] + st[3] < printed.shape[0]]
        if not digits:
            continue
        bx, by, bw, bh = max(digits, key=lambda d: d[0])  # the one nearest the line
        pad = int(0.25 * h)
        cx0, cx1 = max(wx0 + bx - pad, 0), min(wx0 + bx + bw + pad, W)
        cy0, cy1 = max(wy0 + by - pad, 0), min(wy0 + by + bh + pad, H)
        _, pen = _ink(img, gray, (cx0, cy0, cx1, cy1), text_of(i))
        on_digit = pen[by + pad // 2:by + bh + pad, bx - (cx0 - wx0):bx - (cx0 - wx0) + bw + 2 * pad] if cy0 == wy0 + by - pad else pen
        if on_digit.sum() > 0.08 * bw * bh:
            continue  # a pen is over it: step 2 reads it against the clean digits
        crop = img[cy0:cy1, cx0:cx1]
        if pen.any():  # the end of a stroke beside it: painted out before reading
            crop = cv2.inpaint(crop, cv2.dilate(pen.astype(np.uint8), np.ones((5, 5), np.uint8)), 3, cv2.INPAINT_TELEA)
        text, score = _read_digit(eng, crop)
        log.debug("in front of %r: %r at %.2f", lines[i]["text"][:20], text, score)
        if _QUANTITY.match(text) and score >= SURE:
            found[i] = (text, score, wx0 + bx, wx0 + bx + bw)
    # a quantity column: the digits of at least two lines under each other, or under the quantity
    # that another line was read with ("2 Filtered Water")
    anchors = {}  # line number -> (digit, left, right) of a one-digit quantity the reader got itself
    for i, ln in enumerate(lines):
        m = _STARTS_WITH_QUANTITY.match(ln["text"])
        if m and heights[i] >= 8:
            wide = (spans[i][2] - spans[i][0]) / max(len(ln["text"]), 1)
            anchors[i] = (m.group(1), spans[i][0], spans[i][0] + wide * len(m.group(1)) + 0.25 * wide)
    for j, ln in enumerate(lines):  # a quantity read as a piece of its own: it belongs to the line at its right
        if _QUANTITY.match(ln["text"].strip()):
            beside = [i for i in range(len(lines)) if i != j and bare(i) and spans[i][0] >= spans[j][2] - 2
                      and spans[i][0] - spans[j][2] < REACH * heights[i] and abs(mids[i] - mids[j]) < 0.6 * heights[i]]
            if beside:
                i = min(beside, key=lambda k: spans[k][0])
                anchors[i] = (ln["text"].strip(), spans[j][0], spans[j][2])

    def centre(i: int) -> float:
        return (found[i][2] + found[i][3]) / 2
    keep = {i for i in found
            if any(j != i and abs(centre(j) - centre(i)) < COLUMN * heights[i] for j in found)
            or any(abs((a[1] + a[2]) / 2 - centre(i)) < COLUMN * heights[i] for a in anchors.values())}
    found = {i: f for i, f in found.items() if i in keep}
    struck: set = set()
    if found or len(anchors) >= 2:
        # 2. the column is known now: look at its place in front of every other line. A digit with a
        # pen stroke over it is compared with the digits seen cleanly on this slip.
        lefts = [f[2] for f in found.values()] + [a[1] for a in anchors.values()]
        rights = [f[3] for f in found.values()] + [a[2] for a in anchors.values()]
        col0, col1 = float(np.median(lefts)), float(np.median(rights))
        patterns = []  # (digit, picture of it)
        for i, (digit, _, left, right) in found.items():
            patterns.append((digit, gray[int(mids[i] - 0.5 * heights[i]):int(mids[i] + 0.5 * heights[i]), int(left) - 2:int(right) + 2]))
        for i, (digit, left, right) in anchors.items():
            if len(digit) == 1:
                patterns.append((digit, gray[int(mids[i] - 0.5 * heights[i]):int(mids[i] + 0.5 * heights[i]), int(left):int(right)]))
        # only beside the list: within a few rows of a line whose quantity is known, working outwards
        known = [mids[i] for i in list(found) + list(anchors)]
        order = sorted(range(len(lines)), key=lambda i: mids[i])
        for i in order + order[::-1]:
            h, mid = heights[i], mids[i]
            if not any(abs(mid - k) <= NEAR_ROWS * h for k in known):
                continue
            if i in found or not (bare(i) or i in anchors) or (i not in anchors and spans[i][0] < col1) \
                    or _QUANTITY.match(lines[i]["text"].strip()):
                continue
            cx0, cx1 = int(max(col0 - 0.35 * h, 0)), int(min(col1 + 0.35 * h, spans[i][0] if i not in anchors else W))
            cy0, cy1 = int(max(mid - 0.65 * h, 0)), int(min(mid + 0.65 * h, H))
            if cx1 - cx0 < 4 or cy1 - cy0 < 4:
                continue
            reach = int(0.4 * h)
            wide = (max(cx0 - reach, 0), max(cy0 - reach, 0), min(cx1 + reach, W), min(cy1 + reach, H))
            # a stroke through the quantity: long, and on the digit itself, not passing beside it
            core = (int(col0), int(mid - 0.45 * h), int(col1) + 1, int(mid + 0.45 * h))
            reach2 = int(1.2 * h)  # a dark pen is known by its length: look further out for it
            far = (max(cx0 - reach2, 0), max(cy0 - reach2, 0), min(cx1 + reach2, W), min(cy1 + reach2, H))
            line = _slanted_line(gray, far, h)

            def pen_in(box: tuple) -> np.ndarray:  # coloured ink, or a long slanted line of any colour
                part = line[box[1] - far[1]:box[3] - far[1], box[0] - far[0]:box[2] - far[0]]
                coloured = _ink(img, gray, box, text_of(i))[1]
                return coloured | part if part.shape == coloured.shape else coloured
            dark_pen = line.any() and (i not in anchors or len(anchors[i][0]) <= 2)  # "205 River Ridge" is no quantity
            if pen_in(core).sum() >= 0.03 * h * h and (_is_stroke(_ink(img, gray, wide, text_of(i))[1], h) or dark_pen):
                struck.add(i)
            if i in anchors:
                continue
            if any(j != i and sx1 > col0 and sx0 < col1 and abs(mids[j] - mid) < 0.6 * h
                   for j, (sx0, _, sx1, _) in enumerate(spans)):
                if not any(j != i and sx1 > col0 and sx0 < col1 and abs(mids[j] - mid) < 0.6 * h and _QUANTITY.match(lines[j]["text"])
                           for j, (sx0, _, sx1, _) in enumerate(spans)):
                    struck.discard(i)  # other text stands there ("Date" before ": 25/11/2026"), not a quantity
                continue
            printed = _ink(img, gray, (cx0, cy0, cx1, cy1), text_of(i))[0]
            pen = pen_in((cx0, cy0, cx1, cy1))
            if printed.sum() + pen.sum() < 0.05 * h * h:
                continue  # nothing stands there
            if i not in struck and not _is_stroke(pen, 0.6 * h):
                continue  # no pen over it: a digit standing clear was read in step 1, or it is not a digit
            hidden = cv2.dilate(pen.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
            fits = sorted(((_alike(gray[cy0:cy1, cx0:cx1], hidden, pat), digit) for digit, pat in patterns
                           if pat.size and pat.shape[0] <= cy1 - cy0 and pat.shape[1] <= cx1 - cx0), reverse=True)
            log.debug("at the quantity column of %r: %s", lines[i]["text"][:20], [(d, round(f, 2)) for f, d in fits])
            if fits and fits[0][0] >= ALIKE and (len(fits) == 1 or fits[0][1] == fits[1][1] or fits[0][0] - max(
                    f for f, d in fits if d != fits[0][1]) >= 0.04 if any(d != fits[0][1] for f, d in fits) else True):
                found[i] = (fits[0][1], round(fits[0][0], 4), col0, col1)
                known.append(mid)
    out = []
    for i, ln in enumerate(lines):
        if i in found:
            digit, score, left, _ = found[i]
            ln = {**ln, "text": digit + " " + ln["text"], "confidence": round(min(ln["confidence"], score), 4),
                  "box": [[float(left), ln["box"][0][1]], ln["box"][1], ln["box"][2], [float(left), ln["box"][3][1]]]}
        if i in struck and (i in found or i in anchors):  # a stroke through a quantity, not colour beside a heading
            ln = {**ln, "struck": True}
        out.append(ln)
    return out


def brackets(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """The [ ] printed at the end of the rows of a list, top to bottom, and whether something is
    written inside each. The reader does not see them as text."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    H, W = gray.shape
    heights = sorted(_span(ln["box"])[3] - _span(ln["box"])[1] for ln in lines)
    if not heights:
        return []
    h = heights[len(heights) // 2]
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    # a square bracket: a tall, narrow piece of ink about as high as a line of text
    sides = []
    for k in range(1, count):
        x, y, bw, bh, area = (int(v) for v in stats[k])
        if 0.6 * h <= bh <= 1.6 * h and 0.08 * h <= bw <= 0.45 * bh and area >= 0.25 * bw * bh \
                and not any(sx0 + 0.3 * h <= x and x + bw <= sx1 - 0.3 * h and sy0 - 2 <= y + bh / 2 <= sy1 + 2
                            for sx0, sy0, sx1, sy1 in (_span(ln["box"]) for ln in lines if len(ln["text"]) > 3)):
            sides.append((x, y, bw, bh))
    sides.sort(key=lambda s: (s[1], s[0]))
    out, used = [], set()
    for a, (x, y, bw, bh) in enumerate(sides):
        if a in used:
            continue
        for b, (x2, y2, bw2, bh2) in enumerate(sides):
            if b in used or b == a or not (0.8 * h < x2 - x < 5 * h and abs(y2 - y) < 0.3 * h and abs(bh2 - bh) < 0.3 * h):
                continue
            used.update((a, b))
            inside = ink[y + int(0.1 * bh):y + int(0.9 * bh), x + bw + 2:x2 - 2]
            filled = float(inside.mean() / 255) if inside.size else 0.0
            out.append({"box": [[x, y], [x2 + bw2, y], [x2 + bw2, y + bh], [x, y + bh]], "checked": filled > 0.05,
                        "fill": round(filled, 3), "kind": "brackets"})
            break
    if len(out) < 2:
        return []  # one pair alone is as likely a bracketed word
    middle = sorted((b["box"][0][0] + b["box"][1][0]) / 2 for b in out)[len(out) // 2]
    out = [b for b in out if abs((b["box"][0][0] + b["box"][1][0]) / 2 - middle) < 2 * h]  # one column of them
    # The row each stands at the end of. On a curled slip the column of [ ] sits a little higher or
    # lower than the rows of text, by the same amount all the way down: find that shift first.
    rows = [((sp[1] + sp[3]) / 2, ln["text"]) for ln, sp in ((ln, _span(ln["box"])) for ln in lines)
            if sp[0] < out[0]["box"][0][0] and len(ln["text"].strip()) > 2]
    ats = [(b["box"][0][1] + b["box"][2][1]) / 2 for b in out]
    shifts = [mid - at for at in ats for mid, _ in rows if abs(mid - at) < 0.9 * h] or [0.0]
    shift = min(shifts, key=lambda d: sum(min((abs(mid - at - d) for mid, _ in rows), default=h) for at in ats))
    for b, at in zip(out, ats):
        near = sorted((abs(mid - at - shift), text) for mid, text in rows)
        b["label"] = max((text for gap, text in near if gap < near[0][0] + 0.25 * h), key=len) if near and near[0][0] < 0.6 * h else None
    return out


def without_bracket_scraps(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """The reader sometimes takes a printed [ or ] for a letter ("L", "1"). Where a column of [ ]
    is found, the short lines read from its ink are dropped."""
    short = [ln for ln in lines if len(ln["text"].strip()) <= 2]
    if len(short) < 2:
        return lines
    boxes = [(_span(b["box"])) for b in brackets(img, lines)]
    if not boxes:
        return lines

    def scrap(ln: dict) -> bool:
        x0, y0, x1, y1 = _span(ln["box"])
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return len(ln["text"].strip()) <= 2 and any(bx0 - 4 <= cx <= bx1 + 4 and by0 - 4 <= cy <= by1 + 4 for bx0, by0, bx1, by1 in boxes)
    return [ln for ln in lines if not scrap(ln)]
