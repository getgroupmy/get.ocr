"""Reads the transaction table of a bank statement without a language model.

A statement is a regular table: a date, a description, an amount in a deposit
or a withdrawal column, and sometimes a running balance. The rows are read from
where the words sit on the page. Which column is money in and which is money
out is taken from the column headings and proved against the printed balances."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import date
from itertools import product
from typing import Optional

from . import engine

# "1,250.00", "1.250,00", "1 250,00", "1'250.00": the last two digits are always the cents
_MONEY = re.compile(r"^(?:RM|MYR|\$|€|£)?([(\-−+]?)(\d{1,3}(?:[,.' \u00a0]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(\)?)(-|−|\+|DR|CR)?$", re.I)
_GROUP_START = re.compile(r"^[(\-−+]?\d{1,3}$")           # "10" of "10 750,00"
_GROUP_END = re.compile(r"^\d{3}(?: \d{3})*,\d{2}\)?$")  # "750,00": only where a comma marks the cents
_MONTHS = {m: i for i, names in enumerate(
    ("jan janv", "feb fev fevr", "mar mac mrt maart mars marz maerz", "apr avr", "may mei mai", "jun juni juin",
     "jul juli juil juillet", "aug ogo ogos aout", "sep sept", "oct okt", "nov", "dec dis dez"), 1)
    for m in names.split()}  # English, Malay, Dutch, French, German
_NUMERIC_DATE = re.compile(r"^(\d{1,4})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?$")
_NAMED_DATE = re.compile(r"^(\d{1,2})[-/ ]?([A-Za-z]{3,9})\.?[-/ ,]*(\d{2,4})?$")  # after _plain: "02 janv. 25"
_YEAR = re.compile(r"\b\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]((?:19|20)?\d{2})\b|\b[A-Za-z]{3,9} ((?:19|20)\d{2})\b|"
                   r"\b\d{1,2} [A-Za-z]{3,9} (\d{2})\b(?![.,/]\d)")  # not "15 mei 15.05": that 15 starts the next date
_ID_LIKE = re.compile(r"^[A-Z0-9][A-Z0-9/-]{0,13}$")  # a serial number or transaction id printed before the date
_SIDE = {"dr": "-", "debit": "-", "cr": "+", "credit": "+"}  # a column that says which way each line goes
_CURRENCY = re.compile(r"^(?:rs\.?|rm|myr|sgd|s\$|usd|hkd|inr|eur|cad|chf|gbp|\$|€|£)$", re.I)

_OPENING = re.compile(r"balance from previous|previous statement|opening balance|beginning balance|balance b/?f\b|"
                      r"brought forward|previous bal(?:ance)?\b|baki terdahulu|baki lepas|baki awal|baki bawa|baki mula|baki permulaan|baki penyata lepas|"
                      r"solde d'ouverture|solde precedent|solde reporte|ancien solde|beginsaldo|vorig saldo|oud saldo|anfangssaldo|alter saldo", re.I)
_CLOSING = re.compile(r"closing balance|ending balance|balance c/?f\b|carried forward|new balance|statement balance|current balance|baki semasa|baki baru|baki akhir|baki penutup|"
                      r"baki hantar|solde de fermeture|solde de cloture|nouveau solde|solde final|eindsaldo|nieuw saldo|endsaldo|neuer saldo", re.I)
_AS_OF = re.compile(r"\b(?:balance|saldo|solde)\s+(?:as of|as at|per|op|au|am|zum)\b", re.I)  # "Balance as of 01.05.2025: 15.320,00"
_SUMMARY = re.compile(r"\btotal\b|\btotaal\b|\bsumme\b|\bjumlah\b|\bsub-?total\b|minimum payment|credit limit|had kredit|end of statement|penyata tamat|\bpage\b|muka ?surat", re.I)
_HEAD = {"debit": ("withdrawal", "withdrawals", "debit", "debits", "dr", "pengeluaran", "keluar", "payments",
                   "retraits", "retrait", "af", "afschrijvingen", "debet", "soll", "lastschrift"),
         "credit": ("deposit", "deposits", "credit", "credits", "cr", "simpanan", "masuk", "receipts",
                    "depots", "depot", "bij", "bijschrijvingen", "haben", "gutschrift"),
         "balance": ("balance", "baki", "solde", "saldo"),
         "amount": ("amount", "amaun", "montant", "bedrag", "betrag")}
_TOTAL_OUT = re.compile(r"(?:total|totaal|jumlah)\s+(?:des\s+)?(?:withdrawals?|debits?|pengeluaran|debit|outgoing|retraits|af)\b", re.I)
_TOTAL_IN = re.compile(r"(?:total|totaal|jumlah)\s+(?:des\s+)?(?:deposits?|credits?|simpanan|kredit|incoming|depots|bij)\b", re.I)
_ACCOUNT_NO = re.compile(r"\b\d{2,}(?:-\d{2,}){1,4}\b|\b\d{9,}\b")  # 817-890692-838
_GAP = 0.03       # amounts whose right edges are closer than this share of the page width are one column
_NEAR_HEAD = 0.10  # a column belongs to a heading no further away than this
TOLERANCE = 0.05


def _plain(text: str) -> str:
    """Without accents: "Dépôts" is matched as "Depots", "févr." as "fevr."."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _ends_amount(words: list[dict], at: int) -> bool:
    """Is the currency sign at this place the end of an amount's column, not a word in a sentence
    ("PAID 12.50 USD TO SHOP ... 60.00")? It is when it ends the row, or when the row itself ends
    with an amount and its sign, which is how a statement that prints the sign prints every amount."""
    if at + 1 >= len(words):
        return True
    w, nxt = words[at], words[at + 1]
    styled = len(words) >= 2 and _CURRENCY.match(words[-1]["text"]) and (
        _MONEY.match(words[-2]["text"]) or _GROUP_END.match(words[-2]["text"]))
    return bool(styled) and nxt["x0"] - w["x1"] > 3 * (w["x1"] - w["x0"]) / max(len(w["text"]), 1)


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
    for at, w in enumerate(out):
        last = joined[-1] if joined else None
        near = last is not None and w["x0"] - last["x1"] <= 2.5 * (last["x1"] - last["x0"]) / max(len(last["text"]), 1)
        if last and w["text"].upper() in ("DR", "CR") and _MONEY.match(last["text"]) \
                and not last["text"].upper().endswith(("DR", "CR")):
            joined[-1] = {**last, "text": last["text"] + w["text"], "x1": last["x1"]}
        elif last and near and _GROUP_END.match(w["text"]) and _GROUP_START.match(last["text"].split(" ")[-1]) \
                and re.match(r"^[(\-−+]?\d{1,3}(?: \d{3})*$", last["text"]):
            # "10 750,00": thousands set apart by a space, as French and Swiss statements print them
            joined[-1] = {**last, "text": last["text"] + " " + w["text"], "x1": w["x1"]}
        elif last and near and _CURRENCY.match(w["text"]) and _MONEY.match(last["text"]) and _ends_amount(out, at):
            continue  # "114,83 €": the sign after an amount is not a word of the description
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
    mark = "-" if lead in ("-", "−", "(") or close or tail in ("-", "−", "DR") else "+" if lead == "+" or tail in ("+", "CR") else ""
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
        m = _NAMED_DATE.match(_plain(text))
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
                if not more and len(words) > n and re.match(r"^\d{2}\.\d{2}$", words[n]["text"]):
                    # "2 mei  02.05": the value date as day.month. Alone it would read as an amount.
                    day, month = words[n]["text"].split(".")
                    more = 1 if 1 <= int(month) <= 12 and 1 <= int(day) <= 31 else 0
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
        last = None  # the heading the word before belonged to
        for w in r["words"]:
            key = re.sub(r"[^a-z]", "", _plain(w["text"]).lower())
            if last and (not key or _CURRENCY.match(w["text"].strip("()"))) and w["x1"] - found[last] < 0.08:
                found[last] = w["x1"]  # "Solde ($)": the unit is part of the heading
                continue
            last = next((name for name, forms in _HEAD.items() if key in forms), None)
            for name, forms in _HEAD.items():
                # "Dépôts et crédits": the heading ends at its last word, where the amounts line up
                if key in forms and (name not in found or w["x1"] - found[name] < 0.15):
                    found[name] = w["x1"]
        if "balance" in found and len(found) >= 2:
            return found
    return {}


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE + 1e-9


def _follows(txns: list[dict], start: Optional[float], owed: bool = False) -> int:
    """How many printed balances follow from the one before. owed: a charge (debit)
    raises the balance and a payment (credit) lowers it, as on a credit card."""
    hits, prev, pending = 0, start, 0.0
    for t in txns:
        move = (t["debit"] or 0) - (t["credit"] or 0) if owed else (t["credit"] or 0) - (t["debit"] or 0)
        pending += move
        if t["balance"] is None:
            continue
        if prev is not None and _near(prev + pending, t["balance"]):
            hits += 1
        prev, pending = t["balance"], 0.0
    return hits


def _reaches(txns: list[dict], opening: Optional[float], closing: Optional[float], owed: bool = False) -> bool:
    """Do the amounts take the opening balance to the closing one? Tells money in from money out
    on a statement that prints no balance on each line."""
    if opening is None or closing is None or not txns:
        return False
    move = sum((t["credit"] or 0) - (t["debit"] or 0) for t in txns)
    return _near(opening + (-move if owed else move), closing)


def _printed_total(rows: list[dict], label: re.Pattern) -> tuple[Optional[float], Optional[int]]:
    """The total a statement prints for one side, and the number of items when it prints that too."""
    for r in reversed(rows):
        m = label.search(_plain(r["text"]))
        if not m:
            continue
        count = None
        for w in r["words"]:
            if _money(w["text"]):
                return _money(w["text"])[0], count
            word = w["text"].strip("()")
            if word.isdigit() and count is None and int(word) < 1000:  # a small count, never a year
                count = int(word)
    return None, None


def parse(pages: list[dict], order: str = "dmy", owed: bool = False) -> dict:
    """`pages` as service.recognise returns them, with lines. Returns
    the transactions, the opening and closing balance, and the totals the
    statement prints (None when it prints none); the list is empty when the
    pages do not look like a table of dated amounts."""
    whole = "\n".join(p.get("text") or "" for p in pages)
    years = Counter(_year(a or b or c, None) for a, b, c in _YEAR.findall(whole))
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
    # The column headings are the ones just above the first transaction: a statement often has a
    # summary box higher up with its own "balance", "debit" and "credit" headings.
    first = next((i for i, r in enumerate(rows) if _lead_date(r["words"], order, default_year)[0]
                  and any(_money(w["text"]) for w in r["words"])), len(rows))
    heads = _headings(rows[max(first - 6, 0):first][::-1]) if first < len(rows) else _headings(rows)
    if "balance" in heads:
        # Anything printed to the right of the balance column (a barcode or a reference
        # running up the margin) is not part of the table and would break up its rows.
        rows = rows_of(heads["balance"] + 0.02)

    # first pass: what each row is
    for r in rows:
        r["date"], r["date_words"] = _lead_date(r["words"], order, default_year)
        for skip in (1, 2):  # a serial number or an id before the date
            if r["date"] is None and len(r["words"]) > skip and all(_ID_LIKE.match(w["text"]) for w in r["words"][:skip]):
                found, used = _lead_date(r["words"][skip:], order, default_year)
                if found:
                    r["date"], r["date_words"] = found, skip + used
        rest = r["words"][r["date_words"]:]
        # "DR 475,617.41": a word that says which way the line goes marks the amount after it
        side = next((_SIDE[w["text"].lower()] for w in rest if w["text"].lower() in _SIDE), "")
        r["money"] = [(w, *_money(w["text"])) for w in rest if _money(w["text"])]
        if side and r["money"] and r["money"][0][2] == "":
            first = r["money"][0]
            r["money"][0] = (first[0], first[1], side)
        plain = _plain(r["text"])
        r["kind"] = ("opening" if _OPENING.search(plain) and r["money"] else
                     "closing" if _CLOSING.search(plain) and r["money"] else
                     "summary" if _SUMMARY.search(plain) else
                     "dated" if r["date"] and r["money"] else "other")

    # "Balance as of 01.05.2025" and "Balance as of 31.05.2025": the earlier is the opening balance
    dated_balances = []
    for r in rows:
        if r["kind"] in ("dated", "other") and r["money"] and _AS_OF.search(_plain(r["text"])):
            day = next((d for d in (_date(w["text"].strip(":;,"), order, default_year) for w in r["words"]) if d), None)
            if day:
                dated_balances.append((day, r))
    if len({day for day, _ in dated_balances}) >= 2 and not any(r["kind"] in ("opening", "closing") for r in rows):
        dated_balances.sort(key=lambda pair: pair[0])
        dated_balances[0][1]["kind"], dated_balances[-1][1]["kind"] = "opening", "closing"
        for _, r in dated_balances[1:-1]:
            r["kind"] = "summary"

    # A watermark or stamp printed across the page can land in front of a row's date. Where the dates
    # of the other rows line up, a date at that place further into a row is that row's date.
    starts = sorted(r["words"][0]["x0"] for r in rows if r["kind"] == "dated" and _date(r["words"][0]["text"], order, default_year))
    if len(starts) >= 3:
        column = starts[len(starts) // 2]
        for r in rows:
            if r["date"] is not None or r["kind"] != "other" or not r["money"]:
                continue
            for i, w in enumerate(r["words"][:12]):
                if i and abs(w["x0"] - column) <= 0.03:
                    found, used = _lead_date(r["words"][i:], order, default_year)
                    if found:
                        r["date"], r["date_words"], r["kind"] = found, i + used, "dated"
                        r["money"] = [m for m in r["money"] if m[0]["x0"] > w["x0"]]
                    break

    dated = [r for r in rows if r["kind"] == "dated"]
    if not dated:
        return {"transactions": [], "opening_balance": None, "closing_balance": None, "printed": {}, "accounts": 1}

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
        if not columns:
            return None
        best = min(range(len(columns)), key=lambda i: abs(columns[i] - w["x1"]))
        return best if best in keep and abs(columns[best] - w["x1"]) <= _GAP * 2 else None

    opening = closing = None
    openings = 0  # more than one opening balance: several accounts in one statement
    for r in rows:
        if r["kind"] == "opening":
            openings += 1
        if r["kind"] == "opening" and opening is None:
            value, mark = r["money"][-1][1:]
            opening = -value if mark == "-" else value
        elif r["kind"] == "closing":
            value, mark = r["money"][-1][1:]
            closing = -value if mark == "-" else value

    def build(signs: dict[int, int]) -> list[dict]:
        txns: list[dict] = []
        open_row: Optional[dict] = None  # the transaction a following line of description belongs to
        for at, r in enumerate(rows):
            placed = [(column_of(w), w, v, mark) for w, v, mark in r["money"]] if r["kind"] in ("dated", "other") else []
            amounts = [(c, v, mark) for c, _, v, mark in placed if c in amount_cols]
            balance = next(((v, mark) for c, _, v, mark in placed if c == balance_col and c is not None), None)
            same_day = (r["kind"] == "other" and amounts and txns and txns[-1]["_page"] == r["page"]
                        and len(r["words"]) > len(r["money"]))  # a date printed once for several lines
            if (r["kind"] == "dated" and (amounts or balance)) or same_day:
                used = {id(w) for c, w, _, _ in placed if c is not None}
                rest = r["words"][r["date_words"]:]
                # a currency sign printed in front of a column's amount, or the DR/CR that marks it, is not description
                drop = {id(w) for w, nxt in zip(rest, rest[1:]) if id(nxt) in used
                        and ((_CURRENCY.match(w["text"]) and nxt["x0"] - w["x1"] < 0.03) or w["text"].lower() in _SIDE)}
                desc = " ".join(w["text"] for w in rest if id(w) not in used and id(w) not in drop)
                t = {"date": r["date"] or txns[-1]["date"], "description": desc or None, "reference": None,
                     "debit": None, "credit": None, "balance": None, "_page": r["page"], "_bottom": r["bottom"], "_row": at,
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
    # One column of amounts where money out carries a minus: an amount without one is money in.
    signed = {c for r in rows if r["kind"] == "dated" for c, _, _, mark in ((column_of(w), w, v, mark) for w, v, mark in r["money"])
              if mark == "-" and c is not None}
    prior = {i: (-1 if role.get(i) == "debit" else 1 if role.get(i) == "credit" or (i in signed and len(amount_cols) == 1) else -1)
             for i in amount_cols}
    best, best_score = build(prior), None
    for combo in product((-1, 1), repeat=len(amount_cols)) if len(amount_cols) <= 4 else []:
        signs = dict(zip(amount_cols, combo))
        txns = build(signs)
        score = (_follows(txns, opening, owed), _reaches(txns, opening, closing, owed), signs == prior)
        if best_score is None or score > best_score:
            best, best_score = txns, score

    # Several accounts in one statement: each starts at its own opening balance. Say which
    # transactions belong to which, so each can be checked against its own balances.
    by_account = []
    starts = [i for i, r in enumerate(rows) if r["kind"] == "opening"]
    if len(starts) > 1:
        for n, first in enumerate(starts):
            last = starts[n + 1] if n + 1 < len(starts) else len(rows)
            mine = [i for i, t in enumerate(best) if first < t["_row"] < last]
            value, mark = rows[first]["money"][-1][1:]
            ends = [r for r in rows[first:last] if r["kind"] == "closing"]
            end = ends[-1]["money"][-1][1:] if ends else None
            by_account.append({"opening_balance": -value if mark == "-" else value,
                               "closing_balance": None if end is None else (-end[0] if end[1] == "-" else end[0]),
                               "first_transaction": mine[0] + 1 if mine else None, "transactions": len(mine)})
    if not by_account:
        # No opening balance per account, but each account's table has its own title with the
        # account number above its column headings ("USD Savings Account — 817-890692-002").
        titles: list[tuple[int, str, str]] = []
        for i, r in enumerate(rows):
            named = {name for w in r["words"] for name, forms in _HEAD.items()
                     if re.sub(r"[^a-z]", "", _plain(w["text"]).lower()) in forms}
            if "balance" not in named or len(named) < 2:
                continue
            for above in reversed(rows[max(i - 2, 0):i]):
                number = _ACCOUNT_NO.search(above["text"])
                if number and not above.get("money") and len(above["words"]) <= 12:
                    name = re.sub(r"\(continued\)|[—–:-]+\s*$", "", above["text"].replace(number.group(), ""), flags=re.I)
                    titles.append((i, number.group(), " ".join(name.split()).strip(" —–-:") or None))
                    break
        if len({number for _, number, _ in titles}) > 1:
            for n, (at, number, name) in enumerate(titles):
                if by_account and by_account[-1]["account_number"] == number:
                    continue  # the same account, continued on the next page
                later = [row for row, other, _ in titles[n + 1:] if other != number]
                last = later[0] if later else len(rows)
                mine = [i for i, t in enumerate(best) if at < t["_row"] < last]
                by_account.append({"account_number": number, "name": name, "opening_balance": None, "closing_balance": None,
                                   "first_transaction": mine[0] + 1 if mine else None, "transactions": len(mine)})
            if sum(a["transactions"] for a in by_account) != len(best):
                by_account = []  # a transaction outside every titled table: the titles are not the whole story
    for t in best:
        for k in [k for k in t if k.startswith("_")]:
            del t[k]
    out_total, out_count = _printed_total(rows, _TOTAL_OUT)
    in_total, in_count = _printed_total(rows, _TOTAL_IN)
    return {"transactions": best, "opening_balance": opening, "closing_balance": closing,
            "accounts": max(openings, len(by_account), 1),
            "by_account": by_account,
            "printed": {"total_debits": out_total, "debit_count": out_count,
                        "total_credits": in_total, "credit_count": in_count}}
