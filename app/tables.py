"""Reads any table of rows and columns without a language model: the column
headings give the columns, and each piece of text below goes to the heading it
sits under. Exact for a clean table; the totals row, when there is one, proves it."""
from __future__ import annotations

import re
from typing import Optional

from . import engine

_NUMBER = re.compile(r"^[(\-+]?(?:RM|MYR|\$)?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?%?$|^[(\-+]?\d+(?:\.\d+)?\)?%?$")
_TOTAL = re.compile(r"^\W*(?:grand |sub ?)?(?:total|jumlah)\b", re.I)
TOLERANCE = 0.05


def number(text: str) -> Optional[float]:
    t = text.strip()
    if not _NUMBER.match(t) or t.endswith("%"):
        return None
    value = float(re.sub(r"[^\d.]", "", t))
    return -value if t.startswith(("-", "(")) else value


def _pieces(row: list[dict]) -> list[dict]:
    out = []
    for ln in row:
        xs, ys = [p[0] for p in ln["box"]], [p[1] for p in ln["box"]]
        out.append({"text": ln["text"].strip(), "x0": min(xs), "x1": max(xs), "top": min(ys), "bottom": max(ys)})
    return sorted(out, key=lambda p: p["x0"])


def _name(text: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "column"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


_NOT_HEADING = re.compile(r"^\W*$|:\s*$|^\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}$")


def _heading_score(row: list[dict], below: list[list[dict]]) -> int:
    """How much a row looks like column headings: three or more short pieces of
    words (no numbers, dates or labels ending in a colon), scored by how many of
    the rows under it carry a number."""
    if len(row) < 3 or any(number(p["text"]) is not None or _NOT_HEADING.search(p["text"])
                           or len(p["text"].split()) > 5 for p in row):
        return 0
    return sum(len(r) >= 2 and any(number(p["text"]) is not None for p in r) for r in below[:6])


def _find_heading(printed: list[list[dict]]) -> Optional[int]:
    scores = [(_heading_score(r, printed[i + 1:]), len(r), -i) for i, r in enumerate(printed)]
    best = max(scores, default=(0, 0, 0))
    return -best[2] if best[0] >= 1 else None


def _column(piece: dict, heads: list[dict]) -> int:
    def distance(h: dict) -> float:
        return min(abs(piece["x1"] - h["x1"]), abs(piece["x0"] - h["x0"]),
                   abs((piece["x0"] + piece["x1"]) - (h["x0"] + h["x1"])) / 2)
    return min(range(len(heads)), key=lambda i: distance(heads[i]))


def parse(pages: list[dict]) -> dict:
    """`pages` as service.recognise returns them, with lines. Returns
    {'columns': [...], 'rows': [{column: value}], 'totals': {column: value} or None}.
    A table that continues on the next page under the same headings is one table."""
    columns: list[str] = []
    rows: list[dict] = []
    totals: Optional[dict] = None
    for p in pages:
        printed = [_pieces(r) for r in engine._rows(p.get("lines") or [])]
        start = _find_heading(printed)
        if start is None:
            continue
        heads = printed[start]
        taken: set = set()
        names = [_name(h["text"], taken) for h in heads]
        if columns and names != columns:
            continue  # another table: only the first one is read
        columns = names
        last: Optional[dict] = None
        last_bottom = heads[-1]["bottom"]
        height = max(heads[0]["bottom"] - heads[0]["top"], 1)
        for r in printed[start + 1:]:
            if r[0]["top"] - last_bottom > 4 * height:
                break  # a wide gap: the table has ended
            last_bottom = max(x["bottom"] for x in r)
            cells: dict = {}
            for piece in r:
                c = names[_column(piece, heads)]
                cells[c] = (cells[c] + " " + piece["text"]) if c in cells else piece["text"]
            if _TOTAL.match(r[0]["text"]):
                totals = {c: number(v) for c, v in cells.items() if number(v) is not None}
                last = None
                continue
            if last is None and not rows and len(r) >= 3 and not any(number(v) is not None for v in cells.values()):
                continue  # the headings again in a second language
            if last is not None and not any(number(v) is not None for v in cells.values()):
                for c, v in cells.items():  # a second line of text belongs to the row above
                    last[c] = (str(last[c]) + " " + v) if last.get(c) not in (None, "") else v
                continue
            row = {c: None for c in names}
            row.update({c: number(v) if number(v) is not None else v for c, v in cells.items()})
            rows.append(row)
            last = row
    return {"columns": columns, "rows": rows, "totals": totals}


def checks(table: dict) -> list[dict]:
    totals = table["totals"]
    if not totals:
        return [{"name": "column_totals", "passed": None, "detail": "the table prints no totals row"}]
    wrong, right = [], 0
    for column, want in totals.items():
        values = [r[column] for r in table["rows"] if isinstance(r.get(column), (int, float))]
        got = round(sum(values), 2)
        if abs(got - want) <= TOLERANCE + 1e-9:
            right += 1
        else:
            wrong.append(f"{column} adds up to {got:.2f}, the table prints {want:.2f}")
    return [{"name": "column_totals", "passed": not wrong,
             "detail": "; ".join(wrong) if wrong else f"{right} column(s) add up to the totals the table prints"}]
