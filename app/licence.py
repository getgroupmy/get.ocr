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

from . import mykad

DRIVING = re.compile(r"lesen\s*memandu|driving\s*licen[cs]e", re.I)
VOCATIONAL = re.compile(r"lesen\s*vokasional|vocational\s*licen[cs]e", re.I)
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
        labels, values = _labels([ln for ln in lines if not (DRIVING.search(ln["text"]) or VOCATIONAL.search(ln["text"]))])
        if not labels:
            continue
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
            f["holder_name"] = " ".join(got["name"])
        digits = re.search(r"(?<!\d)\d{12}(?!\d)", re.sub(r"(?<=\d)[\s\-](?=\d)", "", " ".join(got.get("id_number", []))))
        if digits:
            f["id_number"] = digits.group(0)
        country = [t for t in got.get("nationality", []) + got.get("id_number", []) if re.fullmatch(r"[A-Z]{3,}", t)]
        if country:
            f["nationality"] = country[0]
        dates = re.findall(_DATE, " ".join(got.get("validity", [])))
        if dates:
            f["issue_date"], f["expiry_date"] = (_iso(dates[0]), _iso(dates[-1])) if len(dates) > 1 else (None, _iso(dates[0]))
        birth = re.search(_DATE, " ".join(got.get("date_of_birth", [])))
        if birth:
            f["date_of_birth"] = _iso(birth.groups())
        if got.get("address"):
            f["address"] = ", ".join(re.sub(r"^NO(?=\d)", "NO ", row) for row in got["address"])
        rows = got.get("type", []) + got.get("classes", [])
        if kind == "vocational_licence":
            # PSV and GDL each start a row; what follows, over one or two lines, is that licence's class
            parts: list[list[str]] = []
            for text in sorted(set(rows), key=rows.index):
                m = re.match(r"(%s)\b\s*(.*)" % "|".join(_KINDS), text)
                if m:
                    parts.append([m.group(1), m.group(2)])
                elif parts:
                    parts[-1][1] = (parts[-1][1] + " " + text).strip()
                else:
                    parts.append(["", text])
            kinds = [p[0] for p in parts if p[0]]
            # the type and its class are side by side, so the reader may give them as separate lines
            loose = [p[1] for p in parts if not p[0]]
            for p in parts:
                if p[0] and not p[1] and loose:
                    p[1] = loose.pop(0)
            if kinds:
                f["licence_types"] = ", ".join(kinds)
            f["classes"] = "; ".join((p[0] + " " + p[1]).strip() for p in parts if p[0] or p[1]) or None
        else:
            if got.get("classes"):  # the class codes, set apart: "B2D" is B2 and D
                codes = re.findall(r"B2|B1|E1|E2|A1|DA|[A-IM]", "".join(got["classes"]).replace(" ", ""))
                f["classes"] = " ".join(codes) or " ".join(got["classes"])
        if not f.get("id_number") and not f.get("expiry_date"):
            continue
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
    return None
