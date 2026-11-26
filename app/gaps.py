"""Words one reader left out, taken from another.

A vision model writes out a page as it sees fit and can skip a heading in a coloured button or
the small print under a crest. The stored text of a PDF has no words that are part of a picture
on the page, such as a letterhead. The standard reader looks at the same page line by line, so
what it read clearly and the other left out is added to the text, near where it sits on the
page, and listed so an app can tell which lines came this way."""
from __future__ import annotations

import difflib
import re

from . import engine

SURE = 0.9  # only what the standard reader read clearly is added
MIN_LETTERS = 4  # shorter scraps are more often a mark than a word
CLOSE = 0.75  # how alike two words must be to count as the same word read slightly differently


def _key(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _words(text: str) -> list[str]:
    return [w for w in (_key(w) for w in re.split(r"\s+", text)) if len(w) >= 3]


def fill(text: str, lines: list[dict]) -> tuple[str, list[dict]]:
    """`text` with the clearly read `lines` it does not have put in, and those lines."""
    have = text.split("\n")
    keys = [_key(row) for row in have]
    whole = "".join(keys)
    known = sorted({w for row in have for w in _words(row)})
    after: dict = {}  # row of the text -> lines to put in below it (-1: above the first)
    added: list[dict] = []
    at = -1
    for row in engine._rows(lines):
        for line in row:
            key = _key(line["text"])
            if not key:
                continue
            if key in whole:
                at = next((n for n, k in enumerate(keys) if key in k), at)
                continue
            words = _words(line["text"])
            alike = [w for w in words if w in known or difflib.get_close_matches(w, known, n=1, cutoff=CLOSE)]
            if words and 2 * len(alike) > len(words):  # the same words, read a little differently
                continue
            if (line.get("confidence") or 0) < SURE or sum(ch.isalpha() for ch in key) < MIN_LETTERS - 1 \
                    or len(key) < MIN_LETTERS:
                continue
            after.setdefault(at, []).append(line["text"])
            added.append({"text": line["text"], "confidence": line.get("confidence"), "box": line["box"]})
    if not added:
        return text, []
    out = list(after.get(-1, []))
    for n, row in enumerate(have):
        out.append(row)
        out.extend(after.get(n, []))
    return "\n".join(out).strip("\n"), added


def beside(text: str, lines: list[dict]) -> list[dict]:
    """The clearly read `lines` that are not in `text`, without changing it: what stands on the
    page outside a table the vision model wrote out."""
    plain = re.sub(r"<[^>]+>", "\n", text)
    return fill(plain, lines)[1]
