"""A motor insurance or takaful schedule, cover note or certificate, read by its labels.

Insurers lay their schedules out differently but use one vocabulary: a policy or cover note
number, the insured, the vehicle's registration, chassis and engine numbers, the period, the sum
insured, and a column of premium, discount, tax and stamp duty that ends in a total. Each value is
looked for beside its label, then under it, and is only taken when it has the shape that value
must have (a registration number, an amount, a date). No model is asked.

Whether the cover extends to e-hailing is read too: schedules print it as an extra cover."""
from __future__ import annotations

import re
from datetime import date
from typing import Callable, Optional

from . import engine

_KNOWN = re.compile(r"insur|takaful|assurance", re.I)
_SCHEDULE = re.compile(r"schedule|jadual|cover\s*note|nota\s*perlindungan|certificate\s*of\s*insurance|sijil\s*insurans|"
                       r"confirmation\s*of\s*purchase|polisi|policy", re.I)
_PREMIUM = re.compile(r"premium|contribution|caruman", re.I)
E_HAILING = re.compile(r"e\s*-?\s*hail|private\s*hire\s*car|kereta\s*sewa\s*persendirian", re.I)
MONTHS = {m: n for n, names in enumerate(
    ("JAN JANUARY JANUARI", "FEB FEBRUARY FEBRUARI", "MAC MAR MARCH", "APR APRIL", "MEI MAY", "JUN JUNE", "JUL JULY JULAI",
     "OGO OGOS AUG AUGUST", "SEP SEPT SEPTEMBER", "OKT OCT OCTOBER OKTOBER", "NOV NOVEMBER", "DIS DEC DECEMBER DISEMBER"), 1)
    for m in names.split()}
_DAY = r"(\d{1,2})\s*[-/. ]\s*(\d{1,2}|[A-Za-z]{3,9})\s*[-/. ]\s*((?:19|20)\d{2})"
_MONEY = r"(?<![\d.,])(\d{1,3}(?:[,.]\d{3})+|\d+)[.,](\d{2})(?![\d,.])"
_PLATE = r"\b([A-Z]{1,3}\s?\d{1,4}\s?[A-Z]{0,2})\b"


def _iso(day: str, month: str, year: str) -> Optional[str]:
    month = month.upper()
    number = MONTHS.get(month) if not month.isdigit() else int(month)
    try:
        return date(int(year), int(number), int(day)).isoformat() if number else None
    except (ValueError, TypeError):
        return None


def _money(text: str, last: bool = True) -> Optional[str]:
    found = re.findall(_MONEY, text)
    if not found:
        return None
    whole, cents = found[-1 if last else 0]
    return whole.replace(",", "").replace(".", "") + "." + cents


def _clean(text: str) -> str:
    return re.sub(r"^[\s:;.\-/]+", "", text).strip()


def _number(text: str) -> Optional[str]:  # a policy or cover note number
    for m in re.finditer(r"[A-Z0-9][A-Z0-9/\-]{5,30}", _clean(text)):
        if sum(ch.isdigit() for ch in m.group(0)) >= 4 and not re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", m.group(0)):
            return m.group(0).strip("-/")
    return None


def _plate(text: str) -> Optional[str]:
    m = re.match(_PLATE + r"(?![\-/\d])", _clean(text).upper())  # the value itself, not a code that starts like one
    v = m.group(1).replace(" ", "") if m else None
    return v if v and any(ch.isdigit() for ch in v) and any(ch.isalpha() for ch in v) else None


def _code(least: int, most: int) -> Callable:
    def check(text: str) -> Optional[str]:
        for token in re.findall(r"[A-Z0-9][A-Z0-9\-]{%d,%d}" % (least - 1, most - 1), _clean(text).upper()):
            if any(ch.isdigit() for ch in token) and sum(ch.isalpha() for ch in token) >= 1:
                return token
        return None
    return check


def _identity(text: str) -> Optional[str]:
    m = re.search(r"(?<!\d)(\d{6})\s?-?\s?(\d{2})\s?-?\s?(\d{4})(?!\d)", text)
    return "".join(m.groups()) if m else None


def _name(text: str) -> Optional[str]:
    text = _clean(text)
    return text if re.fullmatch(r"[A-Z][A-Z .'/@&()\-]{4,60}", text) and len(text.split()) >= 2 else None


def _words(text: str) -> Optional[str]:
    text = _clean(text)
    return text if re.fullmatch(r"[A-Z0-9][A-Z0-9 .,'/&()\-]{2,70}", text) and any(ch.isalpha() for ch in text) else None


def _percent(text: str) -> Optional[str]:
    m = re.search(r"(\d{1,3}(?:\.\d{1,2})?)\s*%", text)
    return m.group(1) if m else None


# field -> (label, what the value must look like)
LABELS: list[tuple[str, str, Callable]] = [
    ("policy_number", r"policy\s*(?:/\s*endorsement\s*)?n(?:o|umber)\b\.?|no\.?\s*polisi|nombor\s*polisi|no\.?\s*sijil|certificate\s*no\.?", _number),
    ("cover_note_number", r"(?:e-?)?cover\s*note\s*no\.?|no\.?\s*nota(?:\s*perlindungan)?|e-?c/?note\s*no\.?", _number),
    ("insured_name", r"the\s*insured|pemegang\s*polisi|policy\s*holder|insured\s*name|name\s*of\s*insured|participant(?:\s*name)?"
                     r"|\binsured\b|\bpemunya\b|yang\s*diinsuranskan", _name),
    ("id_number", r"i\.?\s?c\.?\s*no\.?|nric(?:\s*no\.?)?|no\.?\s*kad\s*pengenalan|new\s*i/?c\s*no\.?|no\.?\s*k/?p(?:\s*baru)?", _identity),
    ("plate_number", r"vehicle(?:/trailer)?\s*reg(?:istration|n)?\.?(?:/trailer)?(?:\s*no\.?)?|reg(?:istration|n)\.?\s*no\.?|"
                     r"no\.?\s*pendaftaran(?:\s*kend\w*)?|vehicle\s*no\.?|index\s*mark", _plate),
    ("chassis_number", r"chassis\s*(?:no|number)\.?|no\.?\s*c[h]?as[ie]s|nombor\s*casis|chassis\b", _code(11, 20)),
    ("engine_number", r"engine(?:/motor)?\s*(?:no|number)\.?|no\.?\s*enjin(?:/motor)?|nombor\s*enjin|engin[ea]\b", _code(5, 16)),
    ("make_model", r"make\s*(?:&|and)\s*(?:type\s*of\s*body|model(?:\s*of\s*vehicle)?)|buatan\s*(?:&|dan)\s*(?:jenis\s*badan|model)|"
                   r"make\s*of\s*vehicle|\bmake\b|\bmodel\b", _words),
    ("cover_type", r"type\s*of\s*cover|jenis\s*perlindungan|cover\s*type|perlindungan\s*yang\s*diberi", _words),
]
# the premium column: label -> field; the amount is the last one on the label's row
AMOUNTS: list[tuple[str, str]] = [
    ("sum_insured", r"sum\s*insured|jumlah\s*diinsuranskan|estimated?\s*(?:of\s*)?value|estimate\s*of\s*value|sum\s*covered|"
                    r"nilai\s*anggaran|jumlah\s*perlindungan|agreed\s*value|market\s*value"),
    ("gross_premium", r"gross\s*premium|premium\s*kasar|gross\s*contribution|total\s*\(excluding\s*sst\)"),
    ("basic_premium", r"basic\s*premium|premium\s*asas|basic\s*contribution|premium\s*/\s*premium|^premium\b|^contribution\b"),
    ("service_tax", r"service\s*tax|cukai\s*perkhidmatan|\bsst\b"),
    ("stamp_duty", r"sta[mn]p\s*dut[yi]i?(?!\s*paid)|duti\s*setem(?!\s*(?:telah|dibayar))"),
    ("total_payable", r"total\s*amount\s*(?:due|payable)|total\s*due(?!\s*\(otc\))|jumlah\s*berbayar(?!\s*di)|total\s*\(after\s*rounded\)|"
                      r"total\s*premium|jumlah\s*premium|payable\s*(?:contribution|premium)|amount\s*payable|total\s*payable|"
                      r"total\s*contribution|^total\b|^jumlah\b"),
    ("ncd_amount", r"\bncd\b|no\s*claim\s*discount|diskaun\s*tanpa\s*tuntutan"),
]
# lines whose label only looks like the one wanted
REJECT = {"plate_number": re.compile(r"\bst\s*reg|business|bus\.?\s*reg|card|kad|perniagaan|service\s*tax|perakuan", re.I),
          "id_number": re.compile(r"old\s*i/?c|lama", re.I)}
FIELDS = ("insurer_name", "policy_number", "cover_note_number", "insured_name", "id_number", "plate_number", "chassis_number",
          "engine_number", "make_model", "cover_type", "period_start", "period_end", "sum_insured", "basic_premium",
          "ncd_percent", "ncd_amount", "gross_premium", "service_tax", "stamp_duty", "total_payable", "currency")


def _box(line: dict) -> tuple:
    xs, ys = [p[0] for p in line["box"]], [p[1] for p in line["box"]]
    return min(xs), min(ys), max(xs), max(ys)


def is_schedule(lines: list[dict]) -> bool:
    whole = " ".join(ln["text"] for ln in lines)
    return bool(len(lines) >= 20 and _KNOWN.search(whole) and _SCHEDULE.search(whole) and _PREMIUM.search(whole)
                and re.search(_MONEY, whole))


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The schedule's fields and checks, or None when no page is one."""
    for page in pages:
        lines = page.get("lines") or []
        if not is_schedule(lines):
            continue
        f = _page(lines, page.get("width") or max(_box(ln)[2] for ln in lines))
        if sum(1 for k in ("policy_number", "cover_note_number", "plate_number", "total_payable", "period_end") if f.get(k)) < 2:
            continue
        whole = " ".join(ln["text"] for ln in lines)
        extra = {"e_hailing": bool(E_HAILING.search(whole))}
        if f.get("period_end"):
            extra["expired"] = date.fromisoformat(f["period_end"]) < (today or date.today())
        return {"fields": f, "checks": [], "extra": extra, "page": page.get("page", 1)}
    return None


def _adds_up(f: dict) -> Optional[bool]:
    try:
        return abs(float(f["gross_premium"]) + float(f["service_tax"]) + float(f["stamp_duty"]) - float(f["total_payable"])) < 0.06
    except (KeyError, ValueError):
        return None


def _settle(f: dict, amounts: list[float]) -> None:
    """The premium column by its arithmetic. A photographed page seldom keeps a label and its
    amount on one level, so the amounts are also found by what they must satisfy: the gross
    premium, the service tax on it (6% or 8%) and the stamp duty (RM 10) come to the total. When
    the amounts read beside their labels do not add up and no such set is on the page, the ones
    that cannot be confirmed are left out."""
    if _adds_up(f):
        return
    seen = sorted(set(round(a, 2) for a in amounts))

    def on_page(v: float) -> bool:
        return any(abs(v - s) < 0.011 for s in seen)
    totals = [float(f["total_payable"])] if f.get("total_payable") else sorted((s for s in seen if s > 50), reverse=True)[:5]
    for total in totals:
        for gross in sorted(seen, reverse=True):
            for stamp in (10.0, 0.0):
                tax = round(total - gross - stamp, 2)
                if gross > 20 and tax > 0 and on_page(tax) and min(abs(tax - 0.06 * gross), abs(tax - 0.08 * gross)) < 0.06 \
                        and (stamp == 0 or on_page(stamp)):
                    f.update(gross_premium="%.2f" % gross, service_tax="%.2f" % tax, stamp_duty="%.2f" % stamp,
                             total_payable="%.2f" % total)
                    return
    if "gross_premium" not in f and all(k in f for k in ("total_payable", "service_tax", "stamp_duty")):
        gross = float(f["total_payable"]) - float(f["service_tax"]) - float(f["stamp_duty"])
        if gross > 0 and min(abs(float(f["service_tax"]) - 0.06 * gross), abs(float(f["service_tax"]) - 0.08 * gross)) < 0.06:
            f["gross_premium"] = "%.2f" % gross  # not printed: what is left of the total without tax and stamp duty


def _page(lines: list[dict], width: float) -> dict:
    f: dict = {}
    rows = engine._rows(lines)
    placed = [(ln, _box(ln), " ".join(ln["text"].split())) for row in rows for ln in row]
    row_of = {id(ln): n for n, row in enumerate(rows) for ln in row}

    def beside_and_below(ln: dict, box: tuple, rest: str) -> list[str]:
        """Where a label's value can stand: after it on its line, to its right on the row, then under it."""
        height = max(box[3] - box[1], 1)
        right = [t for other, b, t in placed if row_of[id(other)] == row_of[id(ln)] and b[0] > box[0] and other is not ln]
        below = sorted(((b[1], t) for other, b, t in placed if box[3] - 0.3 * height < b[1] < box[3] + 2.6 * height
                        and abs(b[0] - box[0]) < 0.06 * width and other is not ln))
        return [rest] + right[:3] + [t for _, t in below[:2]]

    for name, label, shape in LABELS:
        pattern = re.compile(label, re.I)
        for ln, box, text in placed:
            m = pattern.search(text)
            if not m or name in f or (name in REJECT and REJECT[name].search(text)):
                continue
            for candidate in beside_and_below(ln, box, text[m.end():]):
                if re.compile("|".join(lb for _, lb, _ in LABELS), re.I).fullmatch(_clean(candidate).rstrip(" :./")):
                    continue  # another label, or the same one in the other language
                value = shape(candidate)
                if value:
                    f[name] = value
                    break
    loose: set = set()
    for name, label in AMOUNTS:
        pattern = re.compile(label, re.I)
        for row in rows:
            texts = [" ".join(ln["text"].split()) for ln in row]
            at = next((n for n, t in enumerate(texts) if pattern.search(t)), None)
            if at is None or name in f:
                continue
            m = pattern.search(texts[at])
            after = texts[at][m.end():] + "  " + "  ".join(texts[at + 1:])
            amount = _money(after)
            if amount is None and name == "sum_insured":
                whole_number = re.search(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d{4,7})(?![\d,.])", after)  # 31,300 with no cents
                amount = whole_number.group(1).replace(",", "") + ".00" if whole_number else None
            if amount is not None and name == "sum_insured" and float(amount) < 1000 and float(amount) != 0:
                amount = None  # a vehicle's value, not a premium that happens to stand on the label's level
            if amount is not None and name in ("gross_premium", "service_tax", "stamp_duty", "total_payable"):
                lb = _box(row[at])
                holder = next((ln for ln in reversed(row[at:]) if _money(ln["text"]) == amount), row[at])
                hb = _box(holder)
                if abs((hb[1] + hb[3]) / 2 - (lb[1] + lb[3]) / 2) > 0.5 * (lb[3] - lb[1]):
                    loose.add(name)  # the amount does not stand level with the label: it may be a neighbour's
            if name == "ncd_amount":
                percent = _percent(after)
                if percent is not None:
                    f["ncd_percent"] = percent
            if amount:
                f[name] = amount
    # the period: two dates with "to" between them
    joined = ["  ".join(" ".join(ln["text"].split()) for ln in row) for row in rows]
    for n, text in enumerate(joined):
        if not re.search(r"period\s*of\s*(?:insurance|takaful|cover)|tempoh\s*(?:insurans|takaful|perlindungan)|\bfrom\b|\bdari\b", text, re.I):
            continue
        days = [d for d in (_iso(*m) for m in re.findall(_DAY, "  ".join(joined[n:n + 3]))) if d]
        pair = next(((a, b) for a, b in zip(days, days[1:])
                     if 25 <= (date.fromisoformat(b) - date.fromisoformat(a)).days <= 800), None)
        if pair:  # the two dates may stand on separate lines, with the other language between them
            f["period_start"], f["period_end"] = pair
            break
    if "sum_insured" not in f:  # some schedules print the sum under its label, not beside it
        pattern = re.compile(AMOUNTS[0][1], re.I)
        for ln, box, text in placed:
            if pattern.search(text):
                under = [_money(t) for other, b, t in placed if box[3] < b[1] < box[3] + 5 * (box[3] - box[1])
                         and abs(b[0] - box[0]) < 0.12 * width and _money(t) and float(_money(t)) >= 1000]
                if under:
                    f["sum_insured"] = under[0]
                    break
    if "insured_name" not in f and f.get("policy_number"):
        # the insured in an address window: the name stands under the policy number, above the address
        for ln, box, text in placed:
            if text.replace(" ", "") == f["policy_number"].replace(" ", ""):
                under = [t for other, b, t in placed if box[3] - 3 < b[1] < box[3] + 2.5 * (box[3] - box[1])
                         and abs(b[0] - box[0]) < 0.05 * width and _name(t)]
                if under:
                    f["insured_name"] = under[0]
                    break
    insurer = next((t for _, _, t in placed if re.search(r"(insurance|insurans|takaful|assurance).{0,30}(berhad|bhd)|motor\s*insurance\s*pool",
                                                         t, re.I)), None)
    if insurer:
        f["insurer_name"] = re.sub(r"\s*[\d(].*$", "", insurer).strip() or insurer
    _settle(f, [float(_money(t)) for _, _, t in placed for t in re.findall(r"\S+", t) if _money(t)])
    if not _adds_up(f):
        for name in loose:  # not confirmed by the arithmetic: better left out than wrong
            f.pop(name, None)
        gross, tax = f.get("gross_premium"), f.get("service_tax")
        if tax is not None and (gross is None or min(abs(float(tax) - 0.06 * float(gross)),
                                                     abs(float(tax) - 0.08 * float(gross))) > 0.06):
            f.pop("service_tax")  # a tax that is not 6% or 8% of the gross premium is another line's amount
        if f.get("stamp_duty") not in (None, "10.00", "0.00"):
            f.pop("stamp_duty")
    if any(k in f for k in ("total_payable", "gross_premium", "basic_premium")):
        f["currency"] = "MYR"
    return f
