"""Tick boxes and ink stamps on a page. Experimental: found by shape and colour, not by a trained
model, and tried only on made-up pages. Treat what it reports as a pointer for a person to check."""
from __future__ import annotations

import numpy as np


def _label(box: tuple, lines: list[dict]) -> str:
    """The text beside a tick box: the nearest line on the same row, to the right first, else the left."""
    x0, y0, x1, y1 = box
    mid, height = (y0 + y1) / 2, y1 - y0
    best, best_gap = "", None
    for ln in lines:
        xs, ys = [p[0] for p in ln["box"]], [p[1] for p in ln["box"]]
        if abs((min(ys) + max(ys)) / 2 - mid) > 1.2 * max(height, max(ys) - min(ys)):
            continue
        gap = min(xs) - x1 if min(xs) >= x1 - 2 else (x0 - max(xs)) * 3 if max(xs) <= x0 + 2 else None
        if gap is not None and (best_gap is None or gap < best_gap):
            best, best_gap = ln["text"], gap
    return best


def checkboxes(img: np.ndarray, lines: list[dict]) -> list[dict]:
    """Small printed squares and whether something is drawn inside each."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    contours, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    low, high = max(10, 0.008 * w), 0.05 * w
    found: list[tuple] = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if not (low <= bw <= high and low <= bh <= high and 0.8 <= bw / bh <= 1.25):
            continue
        outline = cv2.approxPolyDP(c, 0.06 * cv2.arcLength(c, True), True)
        if len(outline) != 4 or cv2.contourArea(c) < 0.6 * bw * bh:
            continue  # not a square outline
        if any(abs(x - fx) < bw / 2 and abs(y - fy) < bh / 2 for fx, fy, _, _ in found):
            continue  # the inner edge of a box already found
        found.append((x, y, bw, bh))
    def in_text(x: int, y: int, bw: int, bh: int) -> bool:
        cx, cy = x + bw / 2, y + bh / 2  # a letter such as D or O is a small closed shape too
        for ln in lines:  # a letter is shorter than its line; a tick box is about as tall, or taller
            xs, ys = [p[0] for p in ln["box"]], [p[1] for p in ln["box"]]
            if min(xs) < cx < max(xs) and min(ys) < cy < max(ys) and bh < 0.85 * (max(ys) - min(ys)):
                return True
        return False

    out = []
    for x, y, bw, bh in sorted(found, key=lambda b: (b[1], b[0])):
        if in_text(x, y, bw, bh):
            continue
        pad = max(2, int(0.22 * min(bw, bh)))  # look inside the frame, not at it
        inside = ink[y + pad:y + bh - pad, x + pad:x + bw - pad]
        filled = float(inside.mean() / 255) if inside.size else 0.0
        label = _label((x, y, x + bw, y + bh), lines)
        if label:  # a box with nothing written beside it is not a question on a form
            out.append({"box": [[x, y], [x + bw, y], [x + bw, y + bh], [x, y + bh]], "checked": filled > 0.06,
                        "fill": round(filled, 3), "label": label})
    return out


def stamps(img: np.ndarray) -> list[dict]:
    """Patches of red, blue or purple ink large enough to be a rubber stamp or a chop."""
    import cv2
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    strong = (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 60)
    hue = hsv[:, :, 0]
    out = []
    for colour, mask in (("red", strong & ((hue < 10) | (hue > 168))), ("blue", strong & (hue > 100) & (hue < 128)),
                         ("purple", strong & (hue >= 128) & (hue < 160))):
        blob = cv2.dilate(mask.astype(np.uint8), np.ones((max(3, w // 60), max(3, w // 60)), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(blob)
        for i in range(1, n):
            x, y, bw, bh, _ = stats[i]
            ink_share = float(mask[y:y + bh, x:x + bw].mean())
            if bw * bh >= 0.004 * w * h and min(bw, bh) >= 0.04 * w and 0.03 < ink_share < 0.7:
                out.append({"colour": colour, "box": [[int(x), int(y)], [int(x + bw), int(y)],
                                                      [int(x + bw), int(y + bh)], [int(x), int(y + bh)]]})
    return out
