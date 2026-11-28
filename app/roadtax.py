"""The Malaysian road tax disc (Lesen Kenderaan Motor, LKM), read by the shape of what it prints.

The disc has no labels: a plate number, the expiry in large print ("24 MAC" over "2023"), the
period as two runs of eight digits (25032022 24032023), the engine capacity, the fee after "RM",
the region and the class of use, and a serial number. Each is taken by its shape, no model is
asked, and the large expiry is checked against the period."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from . import headings

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


_REGION = re.compile(r"\w?EMENANJUNG|SABAH|SARAWAK|LABUAN|LANGKAWI|PANGKOR")  # the first letter is often lost at the disc's edge
_ANY_FEE = re.compile(r"RM\s*\d{2,6}")  # so is the fee's small point


def is_disc(lines: list[dict]) -> bool:
    """Whether a page is a road tax disc, for naming it. The disc is known by its shape: the
    period as two runs of eight digits beside a fee, a region or a class of use, on a page that
    holds little else. That holds in whatever holder it is kept (an insurer's prints its name
    beside it). Or by its heading standing as a line of its own, with the period or the expiry in
    large print. A summons or a circular that mentions "Lesen Kenderaan Motor" is neither."""
    if not lines or len(lines) > 45:
        return False
    texts = [" ".join(ln["text"].upper().split()) for ln in lines]
    whole = " ".join(texts)
    period = _PERIOD.search(whole)
    if period and (_FEE.search(whole) or _ANY_FEE.search(whole) or _REGION.search(whole) or _USES.search(whole)):
        return True
    if re.search(r"PERAKUAN\s*PENDAFTARAN|SIJIL\s*PEMILIKAN", whole):  # the certificate prints the licence's rate
        return False
    # the licence as JPJ shows it on a screen, which stands in for the disc: its name, a number plate, when it runs out
    if re.search(r"LESEN\s*KENDERAAN\s*MOTOR", whole) and re.search(r"TARIKH\s*(?:LUPUT|TAMAT)|EXPIRY\s*DATE", whole) \
            and any(_PLATE.fullmatch(t) and len(t.replace(" ", "")) >= 4 for t in texts):
        return True
    titled = any(headings.is_title(ln["text"], ("lesenkenderaanmotor",)) for ln in lines)
    large = any(m and m.group(2) in MONTHS for m in (_DAY_MONTH.fullmatch(t) for t in texts)) \
        and any(re.fullmatch(r"20\d{2}", t) for t in texts)
    return bool(titled and (period or large))


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The disc's fields and checks, or None when the pages are not a road tax disc."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"].upper() for ln in lines)
        # the heading is often folded away or cut off: the disc is then known by what only it prints
        if not is_disc(lines) and (not TITLE.search(whole) or _NOT_A_DISC.search(whole)):
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
            # the one read most often, then the longest, then the first as the disc is read (not whichever a set gives first)
            f["serial_number"] = max(dict.fromkeys(serials), key=lambda s: (serials.count(s), len(s)))
        if "expiry_date" not in f and not printed:
            # the licence on a screen: "Tarikh Mula" and "Tarikh Luput" each with its date beside it; the
            # licence's own pair stands last, under the vehicle's number
            for label, name in ((r"TARIKH\s*MULA|START\s*DATE", "period_start"), (r"TARIKH\s*(?:LUPUT|TAMAT)|EXPIRY\s*DATE", "expiry_date")):
                for ln, t in texts:
                    if re.search(label, t):
                        y0, y1 = min(p[1] for p in ln["box"]), max(p[1] for p in ln["box"])
                        beside = [u for other, u in texts if other is not ln
                                  and abs((min(p[1] for p in other["box"]) + max(p[1] for p in other["box"])) / 2 - (y0 + y1) / 2) < (y1 - y0)]
                        day = next((m for u in [t] + beside for m in [re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](20\d{2})", u)] if m), None)
                        if day and _iso(*day.groups()):
                            f[name] = _iso(*day.groups())
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
