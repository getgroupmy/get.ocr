"""The Malaysian road tax disc (Lesen Kenderaan Motor, LKM), read by the shape of what it prints.

The disc has no labels: a plate number, the expiry in large print ("24 MAC" over "2023"), the
period as two runs of eight digits (25032022 24032023), the engine capacity, the fee after "RM",
the region and the class of use, and a serial number. Each is taken by its shape, no model is
asked, and the large expiry is checked against the period."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

TITLE = re.compile(r"lesen\s*kenderaan\s*motor", re.I)
MONTHS = {m: n for n, names in enumerate(
    ("JAN", "FEB", "MAC MAR", "APR", "MEI MAY", "JUN", "JUL", "OGO OGOS AUG", "SEP SEPT", "OKT OCT", "NOV", "DIS DEC"), 1)
    for m in names.split()}
_PLATE = re.compile(r"[A-Z]{1,4}\s?\d{1,4}\s?[A-Z]{0,2}")
_PERIOD = re.compile(r"(?<!\d)([0-3]\d)([01]\d)(20\d{2})\s*([0-3]\d)([01]\d)(20\d{2})(?!\d)")
_DAY_MONTH = re.compile(r"(\d{1,2})\s*([A-Z]{3,4})")
_FEE = re.compile(r"RM\s*(\d{1,5})\s*[.,\s]\s*(\d{2})(?!\d)")
_NOT_A_DISC = re.compile(r"PERAKUAN\s*PENDAFTARAN|SIJIL\s*PEMILIKAN|INSUR|TAKAFUL|SCHEDULE|JADUAL")  # these name the licence too
_REGIONS = ("SEMENANJUNG", "SABAH", "SARAWAK", "LABUAN", "LANGKAWI", "PANGKOR")
_USES = re.compile(r"PERSENDIRIAN|PERDAGANGAN|SYARIKAT|TEKSI|KERETA SEWA|SEWA PANDU|E-?HAILING|BAS|LORI|MOTOSIKAL|KERAJAAN")


def _iso(day: str, month: str, year: str) -> Optional[str]:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _height(line: dict) -> float:
    ys = [p[1] for p in line["box"]]
    return max(ys) - min(ys)


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The disc's fields and checks, or None when the pages are not a road tax disc."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"].upper() for ln in lines)
        # the heading is often folded away or cut off: the disc is then known by what only it prints,
        # the period as two runs of eight digits beside a fee or a region
        shaped = len(lines) <= 40 and _PERIOD.search(whole) and (_FEE.search(whole) or any(r in whole for r in _REGIONS))
        if (not TITLE.search(whole) and not shaped) or _NOT_A_DISC.search(whole):
            continue
        f: dict = {}
        texts = [(ln, " ".join(ln["text"].upper().split())) for ln in lines]
        plates = [ln for ln, t in texts if _PLATE.fullmatch(t)]
        if plates:
            f["plate_number"] = max(plates, key=_height)["text"].upper().replace(" ", "")
        printed = None
        for ln, t in texts:
            m = _PERIOD.search(t)
            if m and "period_start" not in f:
                f["period_start"], f["expiry_date"] = _iso(*m.group(1, 2, 3)), _iso(*m.group(4, 5, 6))
            m = _FEE.search(t)
            if m and "amount" not in f:
                f["amount"], f["currency"] = m.group(1) + "." + m.group(2), "MYR"
                cc = re.match(r"(\d{2,5})\s*[A-Z]", t)
                if cc:
                    f["engine_capacity_cc"] = cc.group(1)
            if t in _REGIONS:
                f["region"] = t
            elif _USES.fullmatch(t):
                f["usage_class"] = t
        # the expiry in large print: a day and month, with the year on a line of its own
        years = [t for _, t in texts if re.fullmatch(r"20\d{2}", t)]
        for _, t in texts:
            m = _DAY_MONTH.fullmatch(t)
            if m and m.group(2) in MONTHS and years:
                printed = _iso(m.group(1), str(MONTHS[m.group(2)]), years[0])
        serials = [t for _, t in texts if re.fullmatch(r"\d{8,10}", t)]
        if serials:
            f["serial_number"] = max(set(serials), key=lambda s: (serials.count(s), len(s)))
        if not f.get("plate_number") and not f.get("expiry_date") and not printed:
            continue
        checks = []
        if printed and f.get("expiry_date"):
            checks.append({"name": "expiry_matches", "passed": printed == f["expiry_date"],
                           "detail": f"the expiry in large print is {printed}, the period ends {f['expiry_date']}"})
        f.setdefault("expiry_date", printed)
        extra = {}
        if f.get("expiry_date"):
            extra["expired"] = date.fromisoformat(f["expiry_date"]) < (today or date.today())
        return {"fields": f, "checks": checks, "extra": extra, "page": page.get("page", 1)}
    return None
