"""Telling a document's own heading from a label or a sentence that holds the same words.

Every reading is given a name, and the name is kept and shown. So it cannot rest on a phrase found
somewhere on a page: the readers that go by position ask whether the phrase stands as the page's
heading, a line of its own, whole or nearly. "No. Lesen Memandu" and "Driving Licence No." are
labels on a form, "holds a valid driving licence" is part of a sentence; neither is a heading."""
from __future__ import annotations

import difflib
import re

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


def top(line: dict) -> float:
    return min(p[1] for p in line["box"])


def under(lines: list, titles) -> tuple:  # noqa: ANN001
    """The lines that are one of `titles`, and the lines that stand below the first of them."""
    heads = [ln for ln in lines if is_title(ln["text"], titles)]
    if not heads:
        return [], []
    first = min(top(h) for h in heads)
    return heads, [ln for ln in lines if top(ln) > first and not any(ln is h for h in heads)]
