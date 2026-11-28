"""Telling a document's own heading from a label or a sentence that holds the same words.

Every reading is given a name, and the name is kept and shown. So it cannot rest on a phrase found
somewhere on a page: the readers that go by position ask whether the phrase stands as the page's
heading, a line of its own, whole or nearly. "No. Lesen Memandu" and "Driving Licence No." are
labels on a form, "holds a valid driving licence" is part of a sentence; neither is a heading."""
from __future__ import annotations

import difflib
import math
import re
from typing import Optional

# a label, not a heading: it starts or ends with "No.", or carries its value after a colon
_LABEL = re.compile(r"^\W*n[o0]\b\.?|\bn[o0]\b\.?\W*$|:\s*\S", re.I)


def key(text: str) -> str:
    """The letters of a line, for comparing it with a title whatever its spacing and punctuation."""
    return re.sub(r"[^a-z]", "", text.lower())


def _alike(a: str, b: str) -> float:
    if not a or not b or 2.0 * min(len(a), len(b)) / (len(a) + len(b)) < 0.8:  # their lengths alone rule it out
        return 0.0
    m = difflib.SequenceMatcher(None, a, b)
    return m.ratio() if m.quick_ratio() >= 0.8 else 0.0


def is_title(text: str, titles, part: float = 0.75, alike: float = 0.85) -> bool:  # noqa: ANN001
    """Whether a line is one of `titles` (each given as its letters only): whole, with a letter or
    two misread, or with its start or end cut off by the edge of a photo or standing on another
    line, as long as `part` of it is there. A label and a line that holds numbers are no title."""
    text = text.strip()
    k = key(text)
    fits = [t for t in titles if part * len(t) <= len(k) <= len(t) + 3]  # most lines of a page are ruled out here
    if not fits or _LABEL.search(text) or sum(ch.isdigit() for ch in text) > 1:
        return False
    for title in fits:
        if abs(len(k) - len(title)) <= 3 and _alike(k, title) >= alike:
            return True
        if any(_alike(k, piece) >= alike + 0.03 for piece in (title[:len(k) + 1], title[-len(k) - 1:])):
            return True
    return False


def lean(line: dict) -> Optional[float]:
    """By how many degrees a line slopes, from the first two corners of its box; None when it is too short
    to tell. A line read on a page that was turned over runs the other way and slopes the same."""
    (x0, y0), (x1, y1), _, (x3, y3) = line["box"]
    length, height = math.hypot(x1 - x0, y1 - y0), math.hypot(x3 - x0, y3 - y0)
    if len(line.get("text") or "") < 4 or length < 3 * max(height, 1.0):
        return None
    slope = math.degrees(math.atan2(y1 - y0, x1 - x0))
    return slope - 180 if slope > 90 else slope + 180 if slope <= -90 else slope


def level(lines: list) -> list:
    """A page's own print: its lines without the few laid across it at another angle. An app's
    watermark stamped over a photo of a card ("GET RIDE / Registration", at fifteen degrees), a rubber
    stamp, a signature: each is read as a line, and a reader that goes by position would take it for
    a value. Only a few lines against a page of level ones are left out. Where a picture holds two
    slopes in numbers (a road tax disc lying level in a holder that is tilted), they are two things
    and nothing is left out; a page that leans as a whole, or curls, loses nothing either."""
    slopes = [(ln, lean(ln)) for ln in lines]
    told = sorted(s for _, s in slopes if s is not None)
    if len(told) < 5:
        return lines
    usual = told[len(told) // 2]
    across = {id(ln) for ln, s in slopes if s is not None and abs(s - usual) >= 10.0}
    if not across or len(across) > 4 or 5 * len(across) > len(told):
        return lines
    return [ln for ln in lines if id(ln) not in across]


def top(line: dict) -> float:
    return min(p[1] for p in line["box"])


def under(lines: list, titles) -> tuple:  # noqa: ANN001
    """The lines that are one of `titles`, and the lines that stand below the first of them."""
    heads = [ln for ln in lines if is_title(ln["text"], titles)]
    if not heads:
        return [], []
    first = min(top(h) for h in heads)
    return heads, [ln for ln in lines if top(ln) > first and not any(ln is h for h in heads)]
