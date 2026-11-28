"""Malaysian driving licences, read by their labels: the driving licence card (Lesen Memandu) and
the vocational licence (Lesen Vokasional: PSV, GDL).

Both print a small label with the value under it, or after it on the same line. Labels are in
small mixed-case print and values in capitals, which is how the two are told apart. No model is
asked. The validity is two dates; the holder's number holds the date of birth, which is checked
against the printed one when the card prints it."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from . import headings, mykad

# the first letter of a heading is often cut off by the edge of the photo
DRIVING = re.compile(r"esen\s*meman|iving\s*lice", re.I)
VOCATIONAL = re.compile(r"esen\s*vokasional|vocational\s*licen[cs]e|vokasional\s*malaysia", re.I)
LABELS = [
    ("name", r"nama\s*/\s*name|nama\b|name\b"),
    ("id_number", r"no\.?\s*k\.?\s*p\.?(?:\s*/\s*[il1]\.?\s*d\.?)?|no\.?\s*pengenalan(?:\s*/\s*[il]dentity\s*no\.?)?|[il]dentity\s*no\.?"),
    ("nationality", r"warganegara(?:\s*/\s*nationality)?|nationality"),
    ("date_of_birth", r"tarikh\s*lahir(?:\s*/\s*date\s*of\s*birth)?|date\s*of\s*birth"),
    ("validity", r"tempoh(?:\s*/\s*valid(?:ity|\s*until))?|valid(?:ity|\s*until)"),
    ("type", r"j[ae]nis(?:\s*/\s*type)?|type\b"),
    ("classes", r"kelas(?:\s*/\s*class)?|class\b"),
    ("address", r"alamat(?:\s*/\s*address)?|address"),
]
_LABEL = re.compile("|".join(f"(?P<{name}>{p})" for name, p in LABELS), re.I)
_DATE = r"(\d{1,2})\s*[/.]\s*(\d{1,2})\s*[/.]\s*((?:19|20)\d{2})"
_STOP = re.compile(r"ketua\s*pengarah|pengangkutan\s*jalan|^malaysia$", re.I)
_KINDS = ("PSV", "GDL", "CDL", "PDL", "LDL")


def _box(line: dict) -> tuple:
    xs, ys = [p[0] for p in line["box"]], [p[1] for p in line["box"]]
    return min(xs), min(ys), max(xs), max(ys)


def _iso(m) -> Optional[str]:  # noqa: ANN001
    try:
        return date(int(m[2]), int(m[1]), int(m[0])).isoformat()
    except ValueError:
        return None


def _labels(lines: list[dict]) -> tuple[list, list]:
    """The labels with where each starts, and the values: what is left of a line once its labels
    are taken out, and every line that holds none."""
    labels, values = [], []
    for ln in lines:
        text = ln["text"]
        x0, y0, x1, y1 = _box(ln)
        per_char = (x1 - x0) / max(len(text), 1)
        marks = [m for m in _LABEL.finditer(text) if any(ch.islower() for ch in m.group(0))]  # values are in capitals
        if not marks:
            if sum(ch.islower() for ch in text) < 2:  # small mixed-case print that is no label: a label read badly
                values.append((x0, y0, y1, " ".join(text.split()), ln))
            continue
        for n, m in enumerate(marks):
            labels.append({"field": m.lastgroup, "x": x0 + m.start() * per_char, "y": y0, "bottom": y1})
            rest = text[m.end():marks[n + 1].start() if n + 1 < len(marks) else len(text)]
            rest = re.sub(r"^[\s:;./]+", "", rest).strip()
            if rest and not _LABEL.fullmatch(rest) and sum(ch.islower() for ch in rest) < 2:  # not the rest of a label
                values.append((x0 + m.end() * per_char, y0, y1, " ".join(rest.split()), ln))
    return labels, values


_DATE_LOOSE = r"(\d{1,2})\s*/+\s*(\d{1,2})\s*/+\s*((?:19|20)\d{2})"
_TYPE = re.compile(r"(P[S5][VY]|G[DO0]L|CDL)\b")
_CLASS = re.compile(r"[A-Z]\s*:\s*\S")


def _vocational(lines: list[dict]) -> dict:
    """The vocational licence by the shape of its values. Its small labels are printed over a
    patterned background in a dot print and are often misread, so nothing here depends on them:
    the number is twelve digits at the end of a line, the validity is two dates, the name is the
    line of capitals above the number, and each class starts with a letter and a colon."""
    f: dict = {}
    rows = [(_box(ln), " ".join(ln["text"].split())) for ln in lines]
    number = next(((b, m.group(1)) for b, t in rows for m in [re.search(r"(\d{12})\W*$", t)] if m), None)
    dated = next(((b, re.findall(_DATE_LOOSE, t)) for b, t in rows if len(re.findall(_DATE_LOOSE, t)) >= 2), None)
    if number:
        f["id_number"] = number[1]
        names = [t for b, t in rows if b[1] < number[0][1] - 5 and re.fullmatch(r"[A-Z][A-Z .'/@\-]{4,}", t)
                 and not VOCATIONAL.search(t) and t != "MALAYSIA"]
        if names:
            f["holder_name"] = " ".join(names[-2:] if len(names) > 1 and len(names[-1].split()) == 1 else names[-1:])
        if any(re.fullmatch(r"MAL\W?", t) for b, t in rows if abs(b[1] - number[0][1]) < (number[0][3] - number[0][1])):
            f["nationality"] = "MAL"
    if dated:
        f["issue_date"], f["expiry_date"] = _iso(dated[1][0]), _iso(dated[1][-1])
    start = (dated or number or ((0, 0, 0, 0),))[0][3]
    kinds: list[str] = []
    classes: list[str] = []
    last = None  # where the last class line stands: the line under it carries on
    for b, t in rows:
        if b[1] < start - 5:
            continue
        if _STOP.search(t) or re.search(r"siri", t, re.I):
            break
        m = _TYPE.match(t)
        if m:
            kind = "GDL" if m.group(1).startswith("G") else "PSV" if m.group(1).startswith("P") else m.group(1)
            if kind not in kinds:
                kinds.append(kind)
            t = t[m.end():].strip()
            if not t:
                continue
        if _CLASS.match(t):
            classes.append(re.sub(r"\s*:\s*", ":", t, count=1))
            last = b
        elif last is not None and classes and b[1] - last[1] < 2.2 * (last[3] - last[1]) and b[0] > last[0] - 120 \
                and sum(ch.islower() for ch in t) < 2 and len(t) > 3:
            classes[-1] = (classes[-1].rstrip("/") + "/" + t.lstrip("/")) if classes[-1].endswith("/") else classes[-1] + " " + t
            last = b
    # each class belongs to one type by the card's design: passenger classes to PSV, goods weights to GDL
    joined = " ".join(classes).upper()
    for kind, words in (("PSV", r"TEKSI|SEWA|HAIL|BAS\b"), ("GDL", r"BTM|\bKG\b")):
        if kind not in kinds and re.search(words, joined):
            kinds.append(kind)
    if kinds:
        f["licence_types"] = ", ".join(k for k in ("PSV", "GDL", "CDL") if k in kinds)
    if classes:
        f["classes"] = "; ".join(classes)
    return f


VOCATIONAL_TITLES = ("lesenvokasionalmalaysia", "malaysianvocationallicence")
DRIVING_TITLES = ("lesenmemandu", "drivinglicence", "lesenmemandumalaysia", "drivinglicencemalaysia")
# the card's own labels are printed in both languages with a stroke between: a form that asks about a licence has none
_CARD_LABEL = re.compile(r"warganegara\s*/\s*n|pengenalan\s*/\s*[il1]|kelas\s*/\s*c|tempoh\s*/\s*v|alamat\s*/\s*a|tarikh\s*lahir\s*/\s*d", re.I)
_CLASS_CODES = re.compile(r"(?:(?:B2|B1|E1|E2|A1|DA|[A-IM])\s?){1,8}")


def card_kind(lines: list[dict]) -> Optional[str]:
    """Which licence a page is, for naming it: "vocational_licence", "driving_licence" or None.
    The card's heading has to stand as a line of its own, above what a card prints: the holder's
    twelve-digit number, the validity as two dates, the classes, the card's two-language labels.
    A rental agreement, a police report, an application form or a letter that mentions a driving
    licence and holds an identity number is not the card."""
    if len(lines) > 60:
        return None
    for kind, titles in (("vocational_licence", VOCATIONAL_TITLES), ("driving_licence", DRIVING_TITLES)):
        heads, below = headings.under(lines, titles)
        if not heads:
            continue
        texts = [" ".join(ln["text"].split()) for ln in below]
        both = len({"ms" if re.search(r"esen|vokasional", headings.key(h["text"])) else "en" for h in heads}) >= 2  # both languages
        number = any(re.search(r"(?<!\d)\d{12}\W*$", re.sub(r"(?<=\d)[\s\-](?=\d)", "", t)) for t in texts)
        dates = any(len(re.findall(_DATE_LOOSE, t)) >= 2 for t in texts)
        if kind == "vocational_licence":
            classes = any(_TYPE.match(t) or _CLASS.match(t) for t in texts)
            if both or number or dates or classes:
                return kind
            continue
        labels = sum(1 for t in texts if _CARD_LABEL.search(t))
        codes = any(_CLASS_CODES.fullmatch(t) and len(t) <= 16 for t in texts)
        # two things a card prints, one of them something a form about a licence does not print
        if sum((both, number, dates, labels >= 1, codes)) >= 2 and (both or dates or labels >= 1):
            return kind
    return None


def parse(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The licence's kind ("driving_licence" or "vocational_licence"), fields and checks; None
    when the pages are neither."""
    for page in pages:
        lines = sorted(page.get("lines") or [], key=lambda ln: _box(ln)[1])
        whole = " ".join(ln["text"] for ln in lines)
        kind = "vocational_licence" if VOCATIONAL.search(whole) else "driving_licence" if DRIVING.search(whole) else None
        if kind is None:
            continue
        width = page.get("width") or max(_box(ln)[2] for ln in lines)
        # a card photographed in a wallet has another card above it: only what stands under this card's title is its own
        title = VOCATIONAL if kind == "vocational_licence" else DRIVING
        top = min((_box(ln)[1] for ln in lines if title.search(ln["text"])), default=0)  # the title may be split over two lines
        lines = [ln for ln in lines if _box(ln)[1] >= top - 5]
        if kind == "vocational_licence":
            f = _vocational(lines)
            if not f.get("id_number") and not f.get("expiry_date"):
                continue
            return _finish(kind, f, page, today)
        labels, values = _labels([ln for ln in lines if not (DRIVING.search(ln["text"]) or VOCATIONAL.search(ln["text"]))])
        if not labels:  # no label was read: the address then starts under the two dates
            dates = next((v for v in values if len(re.findall(_DATE_LOOSE, v[3])) >= 2), None)
            if dates is None:
                continue
            labels = [{"field": "address", "x": dates[0], "y": dates[2] - 1, "bottom": dates[2]}]
        first = min(lb["y"] for lb in labels)
        got: dict = {}
        # the holder's number is twelve digits wherever its label was read or not; the nationality stands beside it
        number = next((v for v in values if re.fullmatch(r"\d{12}", re.sub(r"[\s\-]", "", v[3]))), None)
        if number is not None and kind == "driving_licence":
            got["id_number"] = [number[3]]
            beside = [v for v in values if v is not number and abs(v[1] - number[1]) < 0.6 * (number[2] - number[1])
                      and v[0] < number[0] and re.fullmatch(r"[A-Z ]{3,}", v[3])]
            if beside:
                got["nationality"] = [beside[0][3]]
            values = [v for v in values if v is not number and v not in beside]
        # the validity is two dates on one line, whether its label was read or not
        dated = next((v for v in values if len(re.findall(_DATE_LOOSE, v[3])) >= 2), None)
        if dated is not None and kind == "driving_licence":
            got["validity"] = [dated[3]]
            values = [v for v in values if v is not dated]
        for x, y, bottom, text, ln in values:
            if _STOP.search(text) and "nationality" not in [lb["field"] for lb in labels if lb["y"] < y]:
                continue
            height = max(bottom - y, 1)
            same_row = [lb for lb in labels if abs(lb["y"] - y) < 0.6 * height and lb["x"] <= x]
            above = [lb for lb in labels if lb["y"] < y - 0.4 * height]
            if same_row:
                owner = max(same_row, key=lambda lb: lb["x"])
            elif above:
                latest = max(lb["y"] for lb in above)
                row = [lb for lb in above if latest - lb["y"] < 0.6 * height]
                left = [lb for lb in row if lb["x"] <= x + 0.04 * width]
                owner = max(left, key=lambda lb: lb["x"]) if left else min(row, key=lambda lb: lb["x"])
            elif y < first and kind == "driving_licence":
                # the driving licence prints the name with no label, above the rest; the class letters of
                # the card's background (B1, B2, A) are not part of it
                if len(text) < 3 or any(ch.isdigit() for ch in text) or text == "MALAYSIA":
                    continue
                owner = {"field": "name"}
            else:
                continue
            got.setdefault(owner["field"], []).append(text)
        f: dict = {}
        if got.get("name"):
            f["holder_name"] = re.sub(r"(?<=\w) (A)[ .]?([LP]) (?=\w)", r" \1/\2 ", " ".join(got["name"]))  # A/L, A/P
        digits = re.search(r"(?<!\d)\d{12}(?!\d)", re.sub(r"(?<=\d)[\s\-](?=\d)", "", " ".join(got.get("id_number", []))))
        if digits:
            f["id_number"] = digits.group(0)
        country = [t for t in got.get("nationality", []) + got.get("id_number", []) if re.fullmatch(r"[A-Z]{3,}", t)]
        if country:
            f["nationality"] = country[0]
        dates = re.findall(_DATE_LOOSE, " ".join(got.get("validity", [])))
        if dates:
            f["issue_date"], f["expiry_date"] = (_iso(dates[0]), _iso(dates[-1])) if len(dates) > 1 else (None, _iso(dates[0]))
        birth = re.search(_DATE, " ".join(got.get("date_of_birth", [])))
        if birth:
            f["date_of_birth"] = _iso(birth.groups())
        if got.get("address"):
            f["address"] = ", ".join(re.sub(r"^(NO|\d{5})(?=(?<=NO)\d|(?<=\d)[A-Z])", r"\1 ", row) for row in got["address"])
        if not got.get("classes"):
            code = [v[3] for v in values if re.fullmatch(r"(?:(?:B2|B1|E1|E2|A1|DA|[A-IM])\s?){1,8}", v[3]) and len(v[3]) <= 16
                    and v[3] not in got.get("name", [])]
            if code:
                got["classes"] = code[:1]
        if got.get("classes"):  # the class codes, set apart: "B2D" is B2 and D
            codes = re.findall(r"B2|B1|E1|E2|A1|DA|[A-IM]", "".join(got["classes"]).replace(" ", ""))
            f["classes"] = " ".join(codes) or " ".join(got["classes"])
        if not f.get("id_number") and not f.get("expiry_date"):
            continue
        return _finish(kind, f, page, today)
    return None


def _finish(kind: str, f: dict, page: dict, today: Optional[date]) -> dict:
    checks, extra = [], {}
    if f.get("id_number"):
        by_number = mykad.born(f["id_number"], today)
        if f.get("date_of_birth"):
            checks.append({"name": "birth_date_matches_number", "passed": by_number == f["date_of_birth"],
                           "detail": f"the number gives {by_number}, the card prints {f['date_of_birth']}"})
        elif by_number and f.get("nationality", "MAL").startswith("MAL"):
            f["date_of_birth"] = by_number
    if f.get("expiry_date"):
        extra["expired"] = date.fromisoformat(f["expiry_date"]) < (today or date.today())
    if kind == "vocational_licence":
        extra["e_hailing"] = bool(re.search(r"e\s*-?\s*hailing", f.get("classes") or "", re.I))
    return {"kind": kind, "fields": f, "checks": checks, "extra": extra, "page": page.get("page", 1)}
