"""Reads the transaction table of a bank statement without a language model.

A statement is a regular table: a date, a description, an amount in a deposit
or a withdrawal column, and sometimes a running balance. The rows are read from
where the words sit on the page. Which column is money in and which is money
out is taken from the column headings and proved against the printed balances."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date
from itertools import product
from typing import Optional

from . import engine

_MONEY = re.compile(r"^(?:RM|MYR|\$)?([(\-+]?)(\d{1,3}(?:[,.]\d{3})*[.,]\d{2}|\d+\.\d{2})(\)?)(-|\+|DR|CR)?$", re.I)
_MONTHS = {m: i for i, names in enumerate(
    ("jan", "feb", "mar mac", "apr", "may mei", "jun", "jul", "aug ogo ogos", "sep sept", "oct okt", "nov", "dec dis"), 1)
    for m in names.split()}
_NUMERIC_DATE = re.compile(r"^(\d{1,4})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?$")
_NAMED_DATE = re.compile(r"^(\d{1,2})[-/ ]?([A-Za-z]{3,9})[-/ ,]*(\d{2,4})?$")
_YEAR = re.compile(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]((?:19|20)?\d{2})\b|\b[A-Za-z]{3,9} ((?:19|20)\d{2})\b")

_OPENING = re.compile(r"balance from previous|previous statement|opening balance|beginning balance|balance b/?f\b|"
                      r"brought forward|previous balance|baki terdahulu|baki lepas|baki awal|baki bawa|baki mula|baki permulaan|baki penyata lepas", re.I)
_CLOSING = re.compile(r"closing balance|ending balance|balance c/?f\b|carried forward|new balance|statement balance|current balance|baki semasa|baki baru|baki akhir|baki penutup|"
                      r"baki hantar", re.I)
_SUMMARY = re.compile(r"\btotal\b|\bjumlah\b|\bsubtotal\b|end of statement|penyata tamat|\bpage\b|muka ?surat", re.I)
_HEAD = {"debit": ("withdrawal", "withdrawals", "debit", "debits", "dr", "pengeluaran", "keluar", "payments"),
         "credit": ("deposit", "deposits", "credit", "credits", "cr", "simpanan", "masuk", "receipts"),
         "balance": ("balance", "baki"),
         "amount": ("amount", "amaun")}
_TOTAL_OUT = re.compile(r"(?:total|jumlah)\s+(?:withdrawals?|debits?|pengeluaran|debit)\b", re.I)
_TOTAL_IN = re.compile(r"(?:total|jumlah)\s+(?:deposits?|credits?|simpanan|kredit)\b", re.I)
_GAP = 0.03       # amounts whose right edges are closer than this share of the page width are one column
_NEAR_HEAD = 0.10  # a column belongs to a heading no further away than this
TOLERANCE = 0.05


def _words(row: list[dict], width: float) -> list[dict]:
    """The words of a printed row, left to right, each with its left and right
    edge as a share of the page width. A piece of several words is divided in
    proportion to the length of each word."""
    out = []
    for piece in row:
        xs = [p[0] for p in piece["box"]]
        ys = [p[1] for p in piece["box"]]
        x0, x1, text = min(xs), max(xs), piece["text"]
        per = (x1 - x0) / max(len(text), 1)
        for m in re.finditer(r"\S+", text):
            out.append({"text": m.group(), "x0": (x0 + m.start() * per) / width, "x1": (x0 + m.end() * per) / width,
                        "top": min(ys), "bottom": max(ys)})
    out.sort(key=lambda w: w["x0"])
    # "1,250.00 CR" and "RM 1,250.00": the marker belongs to the amount
    joined: list[dict] = []
    for w in out:
        if joined and w["text"].upper() in ("DR", "CR") and _MONEY.match(joined[-1]["text"]) \
                and not joined[-1]["text"].upper().endswith(("DR", "CR")):
            joined[-1] = {**joined[-1], "text": joined[-1]["text"] + w["text"], "x1": joined[-1]["x1"]}
        else:
            joined.append(w)
    return joined


def _money(text: str) -> Optional[tuple[float, str]]:
    """(value, marker). marker is '-' for a debit or overdrawn amount, '+' for a credit, '' when unmarked."""
    m = _MONEY.match(text)
    if not m:
        return None
    lead, digits, close, tail = m.group(1), m.group(2), m.group(3), (m.group(4) or "").upper()
    value = int(re.sub(r"\D", "", digits)) / 100
    mark = "-" if lead in ("-", "(") or close or tail in ("-", "DR") else "+" if lead == "+" or tail in ("+", "CR") else ""
    return value, mark


def _year(y: Optional[str], default: Optional[int]) -> Optional[int]:
    if not y:
        return default
    return int(y) + 2000 if len(y) <= 2 else int(y)


def _date(text: str, order: str, default_year: Optional[int]) -> Optional[str]:
    """ISO date for one printed date, or None when the text is not a date."""
    m = _NUMERIC_DATE.match(text)
    try:
        if m:
            a, b, c = m.group(1), m.group(2), m.group(3)
            if len(a) == 4:  # 2026-01-31
                return date(int(a), int(b), int(c)).isoformat() if c else None
            if len(a) > 2 or (c is None and "." in text):
                return None  # 668.55 and 12.05 are amounts, not dates
            day, month = (int(a), int(b)) if order == "dmy" else (int(b), int(a))
            year = _year(c, default_year)
            return date(year, month, day).isoformat() if year else None
        m = _NAMED_DATE.match(text)
        name = m.group(2).lower() if m else ""
        month = _MONTHS.get(name) or (_MONTHS.get(name[:3]) if len(name) > 4 else None)  # January, Januari
        if month:
            year = _year(m.group(3), default_year)
            return date(year, month, int(m.group(1))).isoformat() if year else None
    except ValueError:
        return None
    return None


def _lead_date(words: list[dict], order: str, default_year: Optional[int]) -> tuple[Optional[str], int]:
    """The date a row starts with and how many words it takes. A second date
    straight after it (the value date) is taken with it."""
    for n in (3, 2, 1):
        if len(words) >= n:
            text = " ".join(w["text"] for w in words[:n])
            if n > 1 and not re.search(r"[A-Za-z]", text):
                continue  # only a date with a month name is spread over several words
            iso = _date(text, order, default_year)
            if iso:
                _, more = _lead_date(words[n:], order, default_year)
                return iso, n + more
    return None, 0


def _clusters(edges: list[float]) -> list[float]:
    """Right edges of amounts -> the right edge of each column."""
    groups: list[list[float]] = []
    for e in sorted(edges):
        if groups and e - groups[-1][-1] <= _GAP:
            groups[-1].append(e)
        else:
            groups.append([e])
    return [sum(g) / len(g) for g in groups]


def _headings(rows: list[dict]) -> dict:
    """Where the column headings are: {'debit': x, 'credit': x, 'balance': x, 'amount': x} (right edges)."""
    for r in rows:
        found: dict = {}
        for w in r["words"]:
            key = re.sub(r"[^a-z]", "", w["text"].lower())
            for name, forms in _HEAD.items():
                if key in forms and name not in found:
                    found[name] = w["x1"]
        if "balance" in found and len(found) >= 2:
            return found
    return {}


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE + 1e-9


def _follows(txns: list[dict], start: Optional[float]) -> int:
    """How many printed balances follow from the one before."""
    hits, prev, pending = 0, start, 0.0
    for t in txns:
        pending += (t["credit"] or 0) - (t["debit"] or 0)
        if t["balance"] is None:
            continue
        if prev is not None and _near(prev + pending, t["balance"]):
            hits += 1
        prev, pending = t["balance"], 0.0
    return hits


def _printed_total(rows: list[dict], label: re.Pattern) -> tuple[Optional[float], Optional[int]]:
    """The total a statement prints for one side, and the number of items when it prints that too."""
    for r in reversed(rows):
        m = label.search(r["text"])
        if not m:
            continue
        count = None
        for word in r["text"][m.end():].split():
            if _money(word):
                return _money(word)[0], count
            if word.isdigit() and count is None:
                count = int(word)
    return None, None


def parse(pages: list[dict], order: str = "dmy") -> dict:
    """`pages` as service.recognise returns them, with lines. Returns
    the transactions, the opening and closing balance, and the totals the
    statement prints (None when it prints none); the list is empty when the
    pages do not look like a table of dated amounts."""
    whole = "\n".join(p.get("text") or "" for p in pages)
    years = Counter(_year(a or b, None) for a, b in _YEAR.findall(whole))
    default_year = years.most_common(1)[0][0] if years else None

    def rows_of(right_limit: Optional[float]) -> list[dict]:
        found: list[dict] = []
        for p in pages:
            width = float(p.get("width") or 1) or 1.0
            lines = [ln for ln in p.get("lines") or []
                     if right_limit is None or min(c[0] for c in ln["box"]) / width <= right_limit]
            for row in engine._rows(lines):
                words = _words(row, width)
                if words:
                    found.append({"page": p["page"], "words": words, "text": " ".join(w["text"] for w in words),
                                  "top": min(w["top"] for w in words), "bottom": max(w["bottom"] for w in words)})
        return found

    rows = rows_of(None)
    heads = _headings(rows)
    if "balance" in heads:
        # Anything printed to the right of the balance column (a barcode or a reference
        # running up the margin) is not part of the table and would break up its rows.
        rows = rows_of(heads["balance"] + 0.02)

    # first pass: what each row is
    for r in rows:
        r["date"], r["date_words"] = _lead_date(r["words"], order, default_year)
        r["money"] = [(w, *_money(w["text"])) for w in r["words"][r["date_words"]:] if _money(w["text"])]
        r["kind"] = ("opening" if _OPENING.search(r["text"]) and r["money"] else
                     "closing" if _CLOSING.search(r["text"]) and r["money"] else
                     "summary" if _SUMMARY.search(r["text"]) else
                     "dated" if r["date"] and r["money"] else "other")

    dated = [r for r in rows if r["kind"] == "dated"]
    if not dated:
        return {"transactions": [], "opening_balance": None, "closing_balance": None, "printed": {}}

    # the columns: where amounts end, named by the headings above them
    columns = _clusters([w["x1"] for r in dated for w, _, _ in r["money"]])
    role: dict[int, str] = {}
    if heads:
        for name, x in heads.items():
            best = min(range(len(columns)), key=lambda i: abs(columns[i] - x))
            if abs(columns[best] - x) <= _NEAR_HEAD and best not in role:
                role[best] = name
    if heads:
        keep = sorted(role)  # an amount under no heading is part of a description
    else:
        # no headings to go by: the columns are the amounts a row ends with ("PAYMENT 12.50 USD" is description)
        for r in dated:
            tail = []
            for w in reversed(r["words"][r["date_words"]:]):
                if not _money(w["text"]):
                    break
                tail.append(id(w))
            r["money"] = [m for m in r["money"] if id(m[0]) in tail]
        columns = _clusters([w["x1"] for r in dated for w, _, _ in r["money"]])
        role = {len(columns) - 1: "balance"} if len(columns) >= 2 else {}
        keep = list(range(len(columns)))
    amount_cols = [i for i in keep if role.get(i) != "balance"]
    balance_col = next((i for i in keep if role.get(i) == "balance"), None)

    def column_of(w: dict) -> Optional[int]:
        best = min(range(len(columns)), key=lambda i: abs(columns[i] - w["x1"]))
        return best if best in keep and abs(columns[best] - w["x1"]) <= _GAP * 2 else None

    opening = closing = None
    for r in rows:
        if r["kind"] == "opening" and opening is None:
            value, mark = r["money"][-1][1:]
            opening = -value if mark == "-" else value
        elif r["kind"] == "closing":
            value, mark = r["money"][-1][1:]
            closing = -value if mark == "-" else value

    def build(signs: dict[int, int]) -> list[dict]:
        txns: list[dict] = []
        open_row: Optional[dict] = None  # the transaction a following line of description belongs to
        for r in rows:
            placed = [(column_of(w), w, v, mark) for w, v, mark in r["money"]] if r["kind"] in ("dated", "other") else []
            amounts = [(c, v, mark) for c, _, v, mark in placed if c in amount_cols]
            balance = next(((v, mark) for c, _, v, mark in placed if c == balance_col and c is not None), None)
            same_day = (r["kind"] == "other" and amounts and txns and txns[-1]["_page"] == r["page"]
                        and len(r["words"]) > len(r["money"]))  # a date printed once for several lines
            if (r["kind"] == "dated" and (amounts or balance)) or same_day:
                used = {id(w) for c, w, _, _ in placed if c is not None}
                desc = " ".join(w["text"] for w in r["words"][r["date_words"]:] if id(w) not in used)
                t = {"date": r["date"] or txns[-1]["date"], "description": desc or None, "reference": None,
                     "debit": None, "credit": None, "balance": None, "_page": r["page"], "_bottom": r["bottom"],
                     "_height": r["bottom"] - r["top"], "_lines": 0,
                     "_x": next((w["x0"] for w in r["words"][r["date_words"]:] if id(w) not in used), 0.0)}
                for c, v, mark in amounts:
                    side = "debit" if (mark == "-" or (mark == "" and signs.get(c, -1) < 0)) else "credit"
                    t[side] = round((t[side] or 0) + v, 2)
                if balance:
                    t["balance"] = -balance[0] if balance[1] == "-" else balance[0]
                txns.append(t)
                open_row = t
            elif (r["kind"] == "other" and open_row is not None and not r["money"] and open_row["_page"] == r["page"]
                  and open_row["_lines"] < 6 and r["words"][0]["x0"] >= open_row["_x"] - 0.02
                  and r["top"] - open_row["_bottom"] <= 1.5 * max(open_row["_height"], 1)):
                open_row["description"] = ((open_row["description"] or "") + " " + r["text"]).strip()
                open_row["_bottom"], open_row["_lines"] = r["bottom"], open_row["_lines"] + 1
            else:
                open_row = None
        return txns

    # which amount column is money out: the headings say, and the printed balances prove it
    prior = {i: (-1 if role.get(i) == "debit" else 1 if role.get(i) == "credit" else -1) for i in amount_cols}
    best, best_score = build(prior), None
    for combo in product((-1, 1), repeat=len(amount_cols)) if len(amount_cols) <= 4 else []:
        signs = dict(zip(amount_cols, combo))
        txns = build(signs)
        score = (_follows(txns, opening), signs == prior)
        if best_score is None or score > best_score:
            best, best_score = txns, score

    for t in best:
        for k in [k for k in t if k.startswith("_")]:
            del t[k]
    out_total, out_count = _printed_total(rows, _TOTAL_OUT)
    in_total, in_count = _printed_total(rows, _TOTAL_IN)
    return {"transactions": best, "opening_balance": opening, "closing_balance": closing,
            "printed": {"total_debits": out_total, "debit_count": out_count,
                        "total_credits": in_total, "credit_count": in_count}}
