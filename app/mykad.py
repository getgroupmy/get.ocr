"""The front of a Malaysian identity card (MyKad), read by where its values stand.

The card has no labels: the number at the top left, the name below the chip, the address under
the name, citizenship and gender at the lower right. The date and place of birth are worked out
from the number itself, and the number's last digit (odd for a man, even for a woman) is checked
against the printed gender.

This gives what the card prints, for filling in a form. It does not show that the card is genuine
or that it belongs to the person presenting it: that is identity verification, which this service
does not do."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

TITLE = re.compile(r"kad\s*pengenalan|mykad|identity\s*card", re.I)
_NUMBER = re.compile(r"(?<!\d)(\d{6})[\s\-]*(\d{2})[\s\-]*(\d{4})(?!\d)")
_GENDERS = {"LELAKI": "male", "PEREMPUAN": "female"}
_CITIZEN = re.compile(r"WARGA\s*NEGARA|PEMASTAUTIN\s*TETAP|BUKAN\s*WARGANEGARA", re.I)
_RELIGION = re.compile(r"ISLAM")
STATES = {"01": "Johor", "02": "Kedah", "03": "Kelantan", "04": "Melaka", "05": "Negeri Sembilan", "06": "Pahang",
          "07": "Pulau Pinang", "08": "Perak", "09": "Perlis", "10": "Selangor", "11": "Terengganu", "12": "Sabah",
          "13": "Sarawak", "14": "Kuala Lumpur", "15": "Labuan", "16": "Putrajaya"}
for _codes, _state in (("21 22 23 24", "Johor"), ("25 26 27", "Kedah"), ("28 29", "Kelantan"), ("30", "Melaka"),
                       ("31 59", "Negeri Sembilan"), ("32 33", "Pahang"), ("34 35", "Pulau Pinang"),
                       ("36 37 38 39", "Perak"), ("40", "Perlis"), ("41 42 43 44", "Selangor"), ("45 46", "Terengganu"),
                       ("47 48 49", "Sabah"), ("50 51 52 53", "Sarawak"), ("54 55 56 57", "Kuala Lumpur"), ("58", "Labuan")):
    for _c in _codes.split():
        STATES[_c] = _state


def _top(line: dict) -> float:
    return min(p[1] for p in line["box"])


def _left(line: dict) -> float:
    return min(p[0] for p in line["box"])


def born(number: str, today: Optional[date] = None) -> Optional[str]:
    """The date of birth in a card number's first six digits; None when they are not a date."""
    today = today or date.today()
    yy, mm, dd = int(number[:2]), int(number[2:4]), int(number[4:6])
    for century in (2000, 1900):
        try:
            d = date(century + yy, mm, dd)
        except ValueError:
            return None
        if d <= today:
            return d.isoformat()
    return None


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The card's fields and checks, or None when the pages are not a MyKad."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"] for ln in lines)
        if not TITLE.search(whole):
            continue
        holder = next((ln for ln in sorted(lines, key=_top) if _NUMBER.search(ln["text"])), None)
        if holder is None:
            continue
        digits = "".join(_NUMBER.search(holder["text"]).groups())
        width = page.get("width") or max(max(p[0] for p in ln["box"]) for ln in lines)
        f: dict = {"id_number": f"{digits[:6]}-{digits[6:8]}-{digits[8:]}"}
        right = []
        column = []  # the name and the address: the lines under the number, at the left
        for ln in sorted(lines, key=_top):
            text = " ".join(ln["text"].split())
            if _top(ln) <= _top(holder) or TITLE.search(text):
                continue
            if _left(ln) > 0.6 * width:
                right.append(text.upper())
            elif _left(ln) < 0.25 * width and len(text) > 2:
                column.append(ln)
        for text in right:
            if text in _GENDERS:
                f["gender"] = _GENDERS[text]
            elif _CITIZEN.fullmatch(text):
                f["citizenship"] = text
            elif _RELIGION.fullmatch(text):
                f["religion"] = text
        if column:
            # the name stands apart from the address: the widest gap between two lines divides them
            gaps = [_top(b) - _top(a) for a, b in zip(column, column[1:])]
            cut = 1
            if len(gaps) > 1:
                usual = sorted(gaps)[len(gaps) // 2]
                widest = max(range(len(gaps)), key=lambda n: gaps[n])
                if gaps[widest] > 1.4 * usual and widest < 3:
                    cut = widest + 1
            read = " ".join(" ".join(ln["text"].split()) for ln in column[:cut])
            name = re.sub(r"(?<=\w) (A)[ .]?([LP]) (?=\w)", r" \1/\2 ", read)  # A/L, A/P: the stroke is often lost
            f["name"] = name
            address = [" ".join(ln["text"].split()) for ln in column[cut:]]
            if address:
                f["address"] = ", ".join(address)
                for n, row in enumerate(address):
                    m = re.match(r"(\d{5})\s*(.*)", row)
                    if m:
                        f["postcode"], f["city"] = m.group(1), m.group(2).strip(" ,") or None
                        if n + 1 < len(address):
                            f["state"] = address[n + 1]
        extra = {"name_as_read": read} if column and read != f.get("name") else {}
        birth = born(digits, today)
        f["date_of_birth"] = birth
        f["place_of_birth"] = STATES.get(digits[6:8]) or ("outside Malaysia" if digits[6:8] >= "60" else None)
        checks = [{"name": "number_holds_a_date", "passed": birth is not None,
                   "detail": "the first six digits of the number are a date of birth" if birth
                   else f"the first six digits, {digits[:6]}, are not a date"}]
        if f.get("gender"):
            by_number = "male" if int(digits[-1]) % 2 else "female"
            checks.append({"name": "gender_matches_number", "passed": by_number == f["gender"],
                           "detail": f"the number's last digit says {by_number}, the card prints {f['gender']}"})
        return {"fields": f, "checks": checks, "extra": extra, "page": page.get("page", 1)}
    return None
