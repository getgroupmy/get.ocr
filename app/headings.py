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

import numpy as np

# a label, not a heading: it starts or ends with "No.", or carries its value after a colon
_LABEL = re.compile(r"^\W*n[o0]\b\.?|\bn[o0]\b\.?\W*$|:\s*\S", re.I)


def key(text: str) -> str:
    """The letters of a line, for comparing it with a title whatever its spacing and punctuation."""
    return re.sub(r"[^a-z]", "", text.lower())


def is_label(text: str) -> bool:
    """A label of a form, not a heading: it starts or ends with "No.", or carries its value after a colon."""
    return bool(_LABEL.search(text))


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


def _axes(lines: list) -> tuple:
    """For each line the two ends of its own axis, which runs along the longer side of its box, with that side's
    length and the shorter one's: a line that runs steeply up the picture is given with its short edge first."""
    box = np.array([ln["box"] for ln in lines], dtype=float).reshape(-1, 4, 2)
    first, second = np.linalg.norm(box[:, 1] - box[:, 0], axis=1), np.linalg.norm(box[:, 3] - box[:, 0], axis=1)
    along = (first >= second)[:, None]
    start = np.where(along, (box[:, 0] + box[:, 3]) / 2, (box[:, 0] + box[:, 1]) / 2)
    end = np.where(along, (box[:, 1] + box[:, 2]) / 2, (box[:, 3] + box[:, 2]) / 2)
    return start, end, np.maximum(first, second), np.minimum(first, second)


def _told(lines: list, length: np.ndarray, breadth: np.ndarray) -> np.ndarray:
    """Which lines are long enough to tell a slope by."""
    return np.array([len(ln.get("text") or "") >= 4 for ln in lines], dtype=bool) & (length >= 3 * np.maximum(breadth, 1.0))


def lean(line: dict) -> Optional[float]:
    """By how many degrees a line slopes, along the longer side of its box; None when it is too short to tell.
    A line read on a page that was turned over runs the other way and slopes the same."""
    start, end, length, breadth = _axes([line])
    if not _told([line], length, breadth)[0]:
        return None
    slope = math.degrees(math.atan2(end[0, 1] - start[0, 1], end[0, 0] - start[0, 0]))
    return slope - 180 if slope > 90 else slope + 180 if slope <= -90 else slope


AGREE = 2.0  # degrees: lines within this of one direction agree on it
ACROSS, UPRIGHT = 10.0, 70.0  # degrees off the page's own lines: from the first a line is laid across the page, from the
#                               second it is the page's own print set upright (up a margin, over a table's narrow column)


def off_trend(lines: list) -> dict:
    """For each line whose slope can be told (by its id): by how many degrees it stands off the direction the
    page's own lines have at its place. {} where fewer than five lines tell.

    A flat page's lines all point at one place: far off to the side where the camera was held square to
    it, nearer where it was held to one side, so that the lines run towards one another and the slope
    changes steadily down the page (seven degrees one way at the top of a card, seven the other at its
    foot). That place is found as the one most of the long lines agree on, and each line is held against
    the direction towards it from where the line stands, not against one slope for the whole page."""
    if len(lines) < 5:
        return {}
    start, end, length, breadth = _axes(lines)
    told = _told(lines, length, breadth)
    if told.sum() < 5:
        return {}
    start, end, length = start[told], end[told], length[told]
    ends = np.concatenate([start, end])
    centre, scale = ends.mean(axis=0), max(float(np.ptp(ends, axis=0).max()), 1.0)
    start, end = (start - centre) / scale, (end - centre) / scale
    ones = np.ones((len(start), 1))
    through = np.cross(np.hstack([start, ones]), np.hstack([end, ones]))  # each line, as the three numbers of its equation
    through /= np.maximum(np.linalg.norm(through[:, :2], axis=1, keepdims=True), 1e-12)
    mid, way = (start + end) / 2, end - start
    way /= np.maximum(np.linalg.norm(way, axis=1, keepdims=True), 1e-12)

    def off(places: np.ndarray) -> np.ndarray:  # for each place (a row of three numbers) and each line, in degrees
        to = places[:, None, :2] - places[:, None, 2:3] * mid[None, :, :]
        cross = to[..., 0] * way[None, :, 1] - to[..., 1] * way[None, :, 0]
        dot = to[..., 0] * way[None, :, 0] + to[..., 1] * way[None, :, 1]
        return np.degrees(np.arctan2(np.abs(cross), np.abs(dot)))

    longest = np.argsort(-length)[:40]  # the long lines say most about a slope, and any two of them name a place
    i, j = np.triu_indices(len(longest), 1)
    places = np.vstack([np.cross(through[longest[i]], through[longest[j]]), [[1.0, 0.0, 0.0]]])  # and the level page
    size = np.linalg.norm(places, axis=1)
    places = places[size > 1e-9] / size[size > 1e-9, None]
    each = off(places)
    best = int(np.argmax(((each < AGREE) * length[None, :]).sum(axis=1)))
    found, agreeing = each[best], each[best] < AGREE
    if agreeing.sum() >= 3:  # fitted to all the lines that agree, each by its length
        weight = length[agreeing] / length[agreeing].max()
        fitted = off(np.linalg.eigh((through[agreeing] * weight[:, None]).T @ through[agreeing])[1][:, :1].T)[0]
        if (fitted < AGREE).sum() >= agreeing.sum():
            found = fitted
    # A page that curls or was folded has no one such place: its lines then agree best on being parallel, about
    # their middle slope. That is taken unless the place found explains clearly more lines and leaves no more of
    # them far off.
    slopes = (np.degrees(np.arctan2(way[:, 1], way[:, 0])) + 90.0) % 180.0 - 90.0
    parallel = np.abs((slopes - np.median(slopes) + 90.0) % 180.0 - 90.0)
    if (found < AGREE).sum() < (parallel < AGREE).sum() + 2 or (found >= ACROSS).sum() > (parallel >= ACROSS).sum():
        found = parallel
    return {id(ln): float(d) for ln, d in zip((ln for ln, t in zip(lines, told) if t), found)}


def laid_across(lines: list, offs: Optional[dict] = None, least: float = ACROSS) -> list:
    """The lines that stand `least` degrees or more off the page's own, and are not set upright."""
    offs = off_trend(lines) if offs is None else offs
    return [ln for ln in lines if least <= offs.get(id(ln), 0.0) < UPRIGHT]


def level(lines: list) -> list:
    """A page's own print: its lines without the few laid across it at another angle. An app's
    watermark stamped over a photo of a card ("GET RIDE / Registration", at fifteen degrees), a rubber
    stamp, a signature: each is read as a line, and a reader that goes by position would take it for
    a value. A line the reader of the picture itself marked as laid across the page (`across`: it stands
    off the page's lines and is written in another ink) is left out. Of the others, only a few lines
    against a page of level ones are: where a picture holds two slopes in numbers (a road tax disc
    lying level in a holder that is tilted), they are two things and nothing is left out. A page that
    leans as a whole loses nothing, nor does one photographed from one side, whose lines run towards
    one another."""
    lines = [ln for ln in lines if not ln.get("across")] if any(ln.get("across") for ln in lines) else lines
    offs = off_trend(lines)
    across = {id(ln) for ln in laid_across(lines, offs)}
    if not across or len(across) > 4 or 5 * len(across) > len(offs):
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
