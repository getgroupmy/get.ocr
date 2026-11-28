"""Two more vehicle papers read by their labels, with no model asked:

- the e-hailing vehicle permit (Permit Kenderaan E-Hailing, issued by APAD): a label at the left,
  the value at the right;
- the Puspakom inspection, as the full report (Laporan Pemeriksaan Kenderaan, two columns of
  label and value) and as the windscreen slip (Perakuan Pemeriksaan), which has almost no labels
  and is read by the shape of what it prints."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from . import headings
from .insurance import MONTHS

EVP = re.compile(r"permit\s*kenderaan\s*e\s*-?\s*hailing", re.I)
REPORT = re.compile(r"laporan\s*pemeriksaan\s*kenderaan", re.I)
_SLIP = re.compile(r"sah\s*sehingga|valid\s*until|perakuan\s*pemeriksaan", re.I)
_OTHER = re.compile(r"insur|takaful|premium|polis[iy]\b", re.I)
_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*[-/. ]\s*([A-Za-z0]{3,9}|\d{1,2})\W{0,2}[-/. ]\s*((?:19|20)\d{2})")
_PLATE = re.compile(r"[A-Z]{1,3}\s?\d{1,4}\s?[A-Z]{0,2}")


def _box(line: dict) -> tuple:
    xs, ys = [p[0] for p in line["box"]], [p[1] for p in line["box"]]
    return min(xs), min(ys), max(xs), max(ys)


def _iso(day: str, month: str, year: str) -> Optional[str]:
    month = month.upper().replace("0", "O")  # 0CT is OCT
    number = int(month) if month.isdigit() else MONTHS.get(month) or next(
        (n for m, n in MONTHS.items() if len(m) == 3 and sum(a == b for a, b in zip(m, month)) >= 2 and len(month) == 3), None)
    try:
        return date(int(year), int(number), int(day)).isoformat() if number else None
    except (ValueError, TypeError):
        return None


def _date(text: str) -> Optional[str]:
    m = _DAY.search(text)
    return _iso(*m.groups()) if m else None


def _clean(text: str) -> str:
    return re.sub(r"^[\s:;.#\-]+", "", text).strip()


def _values(lines: list[dict], labels: list[tuple], width: float) -> dict:
    """The value of each label: what follows it on its own line, or the line standing to its
    right at its level. A line that is itself a label is no value."""
    every = re.compile("|".join(p for _, p in labels), re.I)
    placed = [(ln, _box(ln), " ".join(ln["text"].split())) for ln in lines]
    out: dict = {}
    for name, pattern in labels:
        rx = re.compile(pattern, re.I)
        for ln, box, text in placed:
            m = rx.search(text)
            if not m or name in out:
                continue
            rest = _clean(text[m.end():])
            if rest and not every.fullmatch(rest):
                out[name] = rest
                break
            height, level = max(box[3] - box[1], 1), (box[1] + box[3]) / 2
            right = sorted(((b[0], abs((b[1] + b[3]) / 2 - level), t) for other, b, t in placed
                            if other is not ln and box[2] - 5 < b[0] < box[2] + 0.42 * width and abs((b[1] + b[3]) / 2 - level) < 0.9 * height))
            for _, _, t in right:
                found = every.search(t)
                if found and found.start() < 3:
                    break  # the next label on the row: this one has no value
                if _clean(t):
                    out[name] = _clean(every.split(t)[0]) if found else _clean(t)
                    break
            if name in out:
                break
    return out


EVP_LABELS = [("holder_name", r"nama\s*pemilik\s*kenderaan"), ("plate_number", r"no\.?\s*pendaftaran\s*kenderaan"),
              ("seating_capacity", r"muatan\s*tempat\s*duduk"), ("vehicle_type", r"jenis\s*kenderaan"),
              ("permit_number", r"no\.?\s*rujukan\s*permit\s*\(?\s*induk\s*\)?"),
              ("sub_permit_number", r"no\.?\s*rujukan\s*permit\s*\(?\s*sub\s*\)?"),
              ("issue_date", r"tarikh\s*mula\s*permit"), ("expiry_date", r"tarikh\s*sah\s*laku\s*permit")]


def is_permit(lines: list[dict]) -> bool:
    """Whether a page is the e-hailing vehicle permit, for naming it: its heading as a line of its
    own with three of the permit's labels under it. A letter about an application for one is not."""
    heads, below = headings.under(lines, ("permitkenderaanehailing",))
    if not heads:
        return False
    text = "\n".join(ln["text"] for ln in below)
    return sum(1 for _, label in EVP_LABELS if re.search(label, text, re.I)) >= 3


_CHASSIS = re.compile(r"[A-Z0-9]{17}")
_SLIP_MARKS = (r"PUSPA", r"\bPERAKUAN\b[\s\S]*\bPEMERIKSAAN\b|\bPEMERIKSAAN\b[\s\S]*\bPERAKUAN\b", r"KOD\s*PUSAT",
               r"SAH\s*SEHINGGA|VALID\s*UNTIL", r"(?:BERKALA|METER\s*TEKSI)\W*\d", r"(?:^|\n)[A-Z]\s?\d{9}(?:\n|$)")


def is_inspection(lines: list[dict]) -> bool:
    """Whether a page is a Puspakom inspection, for naming it. The report: its heading as a line
    of its own over three of its labels. The windscreen slip, which prints hardly a label: the
    vehicle's seventeen-character chassis number with two of the slip's own marks (Puspakom's
    name, "Perakuan" and "Pemeriksaan", the centre's code, what it is valid until, a serial
    number). "Valid until" beside a number plate alone is a season pass, a compound or a voucher."""
    heads, below = headings.under(lines, ("laporanpemeriksaankenderaan",))
    if heads:
        text = "\n".join(ln["text"] for ln in below)
        return sum(1 for name, label in REPORT_LABELS if name != "_" and re.search(label, text, re.I)) >= 3
    if not lines or len(lines) > 60:
        return False
    texts = [" ".join(ln["text"].upper().split()) for ln in lines]
    whole = "\n".join(texts)
    if _OTHER.search(whole):
        return False
    chassis = any(_CHASSIS.fullmatch(t) and any(ch.isalpha() for ch in t) and any(ch.isdigit() for ch in t) for t in texts)
    return chassis and sum(1 for mark in _SLIP_MARKS if re.search(mark, whole)) >= 2


def evp(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The e-hailing vehicle permit's fields, or None when no page is one."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"] for ln in lines)
        if not EVP.search(whole) or _OTHER.search(whole):
            continue
        width = page.get("width") or max(_box(ln)[2] for ln in lines)
        f = _values(lines, EVP_LABELS, width)
        f["permit_type"] = "EVP"
        for name in ("issue_date", "expiry_date"):
            f[name] = _date(f.get(name) or "")
        if f.get("plate_number"):
            f["plate_number"] = f["plate_number"].replace(" ", "").upper()
        # the operator the permit is tied to stands under its own heading, with its registration number in brackets
        ordered = sorted(lines, key=lambda ln: _box(ln)[1])
        for n, ln in enumerate(ordered):
            if re.search(r"pemegang\s*lesen\s*perniagaan\s*pengantaraan", ln["text"], re.I) and n + 1 < len(ordered):
                m = re.match(r"(.+?)\s*(?:\(([\dA-Z\-]{6,14})\))?\s*$", " ".join(ordered[n + 1]["text"].split()))
                if m and not re.search(r"nama\s*pemilik|permit\s*ini", m.group(1), re.I):
                    f["operator_name"], f["operator_registration"] = m.group(1).strip(), m.group(2)
                break
        if not f.get("plate_number") and not f.get("permit_number"):
            continue
        extra = {"expired": date.fromisoformat(f["expiry_date"]) < (today or date.today())} if f.get("expiry_date") else {}
        return {"fields": f, "checks": [], "extra": extra, "page": page.get("page", 1)}
    return None


REPORT_LABELS = [("report_number", r"no\.?\s*keputusan"), ("owner_name", r"\bpemilik\b(?!\s*:?\s*$)|^pemilik"),
                 ("plate_number", r"no\.?\s*pendaftaran"), ("engine_number", r"no\.?\s*enjin(?:\s*/\s*motor)?"),
                 ("chassis_number", r"no\.?\s*casis(?!\s*treler)"), ("make", r"\bbuatan\b"), ("model", r"\bmodel\b"),
                 ("year_made", r"tahun\s*diperbuat"), ("usage_class", r"kategori\s*kenderaan"), ("body_type", r"jenis\s*badan"),
                 ("fuel_type", r"bahan\s*bakar"), ("engine_capacity_cc", r"keupayaan\s*enjin(?:\s*/\s*motor)?"),
                 ("registration_date", r"tarikh\s*pendaftaran"), ("inspection_date", r"tarikh\s*pemeriksaan(?!\s*akhir)"),
                 ("inspection_type", r"jenis\s*pemeriksaan"), ("centre_name", r"kod\s*pusat"),
                 ("expiry_date", r"sah\s*sehingga|valid\s*until|tarikh\s*luput|tarikh\s*tamat"),
                 ("result", r"keputusan\s*pemeriksaan|status\s*pemeriksaan"),
                 # labels that are only there so their text is not taken for a neighbour's value
                 ("_", r"alamat\s*pemilik|pembawa|no\.?\s*casis\s*treler|[mn]o\.?\s*treler|status\s*pemunya|kadar\s*lkm|"
                       r"tarikh\s*pemeriksaan\s*akhir|kod\s*pelulus|kod\s*pemeriksa|sijil\s*t1|berat\s*\w+\s*muatan|"
                       r"muatan\s*tempat\s*duduk")]


def inspection(pages: list[dict], today: Optional[date] = None) -> Optional[dict]:
    """The Puspakom inspection report or windscreen slip, or None when no page is one."""
    for page in pages:
        lines = page.get("lines") or []
        whole = " ".join(ln["text"] for ln in lines)
        if _OTHER.search(whole):
            continue
        width = page.get("width") or (max(_box(ln)[2] for ln in lines) if lines else 0)
        if REPORT.search(whole):
            f = {k: v for k, v in _values(lines, REPORT_LABELS, width).items() if k != "_"}
            for name in ("inspection_date", "registration_date", "expiry_date"):
                if name in f:
                    f[name] = _date(f[name])
            for name in ("year_made", "engine_capacity_cc"):
                m = re.search(r"\d{2,5}", f.get(name) or "")
                f[name] = m.group(0) if m else None
            for name in ("plate_number", "chassis_number", "engine_number"):
                if f.get(name):
                    f[name] = f[name].replace(" ", "").upper()
            verdict = re.search(r"\b(LULUS|GAGAL|PASS(?:ED)?|FAIL(?:ED)?)\b", whole.upper())
            if verdict and not f.get("result"):
                f["result"] = verdict.group(1)
        elif _SLIP.search(whole) or (re.search(r"\bPERAKUAN\b", whole) and re.search(r"\bPEMERIKSAAN\b", whole)) \
                or is_inspection(lines):
            f = _slip(lines)
        else:
            continue
        if not f.get("plate_number") and not f.get("chassis_number"):
            continue
        f.setdefault("centre_name", "PUSPAKOM" if re.search(r"puspa", whole, re.I) else None)
        extra = {"expired": date.fromisoformat(f["expiry_date"]) < (today or date.today())} if f.get("expiry_date") else {}
        return {"fields": f, "checks": [], "extra": extra, "page": page.get("page", 1)}
    return None


def _slip(lines: list[dict]) -> dict:
    """The windscreen slip: a serial number, the registration number, what each inspection is
    valid until ("BERKALA-05-NOV-2023", "METER TEKSI-28-OCT-2028"), the fee, the engine and the
    chassis number. Hardly a label is printed, so each is known by its shape."""
    f: dict = {}
    dates: list[str] = []
    for ln in sorted(lines, key=lambda ln: _box(ln)[1]):
        text = " ".join(ln["text"].split()).upper()
        day = _date(text)
        valid = re.match(r"(BERKALA|METER\s*TEKSI|KHAS|AWALAN|TUKAR\s*MILIK)\W", text)
        if valid and day:
            name = "meter_expiry_date" if "METER" in valid.group(1) else "expiry_date"
            f.setdefault(name, day)
            if name == "expiry_date":
                f["inspection_type"] = valid.group(1)
        elif day:
            dates.append(day)
        elif re.fullmatch(r"[A-Z]\s?\d{9}", text):
            f.setdefault("report_number", text.replace(" ", ""))
        elif re.fullmatch(r"RM\s*\d+[.,]\d{2}", text):
            f.setdefault("fee", re.sub(r"[^\d.]", "", text.replace(",", ".")))
        elif re.fullmatch(r"[A-Z0-9]{17}", text) and any(ch.isalpha() for ch in text):
            f.setdefault("chassis_number", text)
        elif _PLATE.fullmatch(text) and any(ch.isdigit() for ch in text):
            f.setdefault("plate_number", text.replace(" ", ""))
        elif re.fullmatch(r"[A-Z0-9]{6,14}", text) and any(ch.isdigit() for ch in text) and sum(ch.isalpha() for ch in text) >= 2:
            f.setdefault("engine_number", text)
    if "expiry_date" not in f and dates:  # a slip that prints its date large, with no word before it
        f["expiry_date"] = max(dates)
        dates.remove(f["expiry_date"])
    earlier = [d for d in dates if d < (f.get("expiry_date") or "9999")]
    if earlier:
        f["inspection_date"] = max(earlier)
    return f
