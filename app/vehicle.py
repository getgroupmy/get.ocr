"""The Malaysian vehicle ownership certificate (Sijil Pemilikan Kenderaan, the "geran" issued by
JPJ), read by its labels.

The certificate is a form: a label at the left, a colon, the value at the right. Each value is
taken from the line of its label, so nothing is guessed and no model is asked. Both printings are
read: the one with a label of its own for each value, and the later one that puts two values on a
line ("No. Chasis/No. Enjin : A / B"). Anything else goes to the model as before."""
from __future__ import annotations

import re
from typing import Optional

from . import engine

TITLE = re.compile(r"sijil\s*pemilikan\s*kenderaan", re.I)
# label -> the field, or the two fields of a line that carries two values
LABELS = [
    (r"n[oö0]\.?\s*c[h]?as+is\s*/\s*n[oö0]\.?\s*enjin", ("chassis_number", "engine_number")),
    (r"buatan\s*/\s*nama\s*model", ("make", "model")),
    (r"jenis\s*badan\s*/\s*tahun\s*dibuat", ("body_type", "year_made")),
    (r"n[oö0]\.?\s*pendaftaran", "plate_number"),
    (r"[nñ][oö0]\.?\s*[il1]\s*d\b", "owner_id_number"),
    (r"nama\s*pem\w*\s*berdaftar", "owner_name"),
    (r"alamat", "owner_address"),
    (r"n[oö0]\.?\s*c[h]?as+is", "chassis_number"),
    (r"n[oö0]\.?\s*enjin", "engine_number"),
    (r"nama\s*model", "model"),
    (r"buatan", "make"),
    (r"keupayaan\s*enjin", "engine_capacity_cc"),
    (r"bahan\s*bakar", "fuel_type"),
    (r"status\s*asal", "origin_status"),
    (r"kelas\s*kegunaan", "usage_class"),
    (r"jenis\s*badan", "body_type"),
    (r"tahun\s*dibuat", "year_made"),
    (r"tarikh\s*pendaftaran", "registration_date"),
    (r"tarikh\s*cetakan", "print_date"),
    (r"b\.?\s*d\.?\s*m\.?\s*/\s*b\.?\s*g\.?\s*k\.?(?:\s*/\s*b\.?\s*t\.?\s*m\.?)?", "weights"),
    (r"b\.?\s*t\.?\s*m\b\.?", "weights"),
    (r"syarat\s*pendaftaran\s*\d", "registration_conditions"),
    (r"syarat\s*-?\s*syarat\s*pendaftaran", "registration_conditions"),
]
_LABEL = re.compile("|".join(f"(?P<l{n}>{p})" for n, (p, _) in enumerate(LABELS)), re.I)
_EMPTY = re.compile(r"^[\s:.\-–—_]*$")
FIELDS = ("plate_number", "owner_id_number", "owner_name", "owner_address", "chassis_number", "engine_number", "make",
          "model", "engine_capacity_cc", "fuel_type", "origin_status", "usage_class", "body_type", "year_made",
          "registration_date", "weights", "registration_conditions", "certificate_serial", "print_date")


def _value(text: str) -> Optional[str]:
    text = re.sub(r"^[\s:;.]+", "", text).strip()
    return None if _EMPTY.match(text) else " ".join(text.split())


def _left(line: dict) -> float:
    return min(p[0] for p in line["box"])


def parse(pages: list[dict]) -> Optional[dict]:
    """The certificate's fields, or None when the pages are not this certificate. `sources` in the
    answer gives the line each value was taken from."""
    for page in pages:
        lines = page.get("lines") or []
        if not lines or not TITLE.search(" ".join(ln["text"] for ln in lines)):
            continue
        found = _page(lines, page.get("width") or 0)
        if found[0].get("plate_number") or sum(1 for v in found[0].values() if v) >= 4:
            return {"fields": found[0], "confidence": found[1], "page": page.get("page", 1)}
    return None


def _page(lines: list[dict], width: float):  # noqa: ANN202
    fields: dict = {name: None for name in FIELDS}
    sure: dict = {}
    conditions: list[str] = []
    rows = engine._rows(lines)
    first_label = None
    value_column = None  # where the values start: a line there with no label continues the value above
    last: Optional[str] = None
    serial = None
    for row in rows:
        text = "  ".join(ln["text"] for ln in row)
        marks = list(_LABEL.finditer(text))
        if TITLE.search(text) or re.search(r"jabatan\s*pengangkutan", text, re.I):
            for ln in row:  # the certificate's own number stands at the right of the heading
                if width and _left(ln) > 0.7 * width and re.fullmatch(r"[A-Z0-9]{6,12}", ln["text"].strip()):
                    serial = ln
            continue
        if not marks:
            if first_label is None:
                for ln in row:
                    if width and _left(ln) > 0.7 * width and re.fullmatch(r"[A-Z0-9]{6,12}", ln["text"].strip()):
                        serial = ln
                continue
            value = _value(text)
            if value is None:
                continue
            if last == "registration_conditions":
                conditions.append(value)
            elif last == "owner_address" and value_column is not None and _left(row[0]) > value_column - 0.05 * max(width, 1):
                fields[last] = (fields[last] + ", " + value) if fields[last] else value
            continue
        first_label = first_label or row
        low = min((ln.get("confidence") or 1.0) for ln in row)
        for n, m in enumerate(marks):
            target = LABELS[int(m.lastgroup[1:])][1]
            value = _value(text[m.end():marks[n + 1].start() if n + 1 < len(marks) else len(text)])
            last = target if isinstance(target, str) else target[0]
            if value is not None and value_column is None and len(row) > 1:
                value_column = _left(row[1])
            if target == "registration_conditions":
                if value:
                    conditions.append(value)
                continue
            if value is None:
                continue
            if isinstance(target, tuple):  # two values on the line, set apart by a stroke
                parts = [p.strip() or None for p in value.split("/", 1)] + [None]
                for name, part in zip(target, parts):
                    if part and fields[name] is None:
                        fields[name], sure[name] = part, low
            elif target == "weights" and fields["weights"]:
                fields["weights"] += " / " + value
            elif fields[target] is None:
                fields[target], sure[target] = value, low
    if serial is not None:
        fields["certificate_serial"], sure["certificate_serial"] = serial["text"].strip(), serial.get("confidence") or 1.0
    if conditions:
        fields["registration_conditions"] = "\n".join(conditions)
    for name in ("registration_date", "print_date"):  # the certificate prints day/month/year
        m = re.fullmatch(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4})", fields[name] or "")
        if m:
            fields[name] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    for name in ("plate_number", "chassis_number", "engine_number", "owner_id_number"):
        if fields[name]:
            fields[name] = re.sub(r"[\s\-]", "", fields[name]).upper() if name != "owner_id_number" else re.sub(r"\s", "", fields[name])
    return fields, sure
