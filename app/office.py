"""The text of a Word document (.docx) or an Excel workbook (.xlsx), read from the file itself.

Both are zip archives of XML. Nothing is drawn or recognised: the words are taken as they were
typed, so they are exact. A Word document is one page; each sheet of a workbook is a page. The
old binary formats (.doc, .xls) are not read."""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, timedelta
from typing import Optional
from xml.etree import ElementTree

from .config import get_settings
from .errors import ApiError

MAX_PART = 40_000_000  # one part of the archive, unpacked: a small file that unpacks to more is refused
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48))  # Excel's built-in date and time formats
_CELL = re.compile(r"^([A-Z]+)(\d+)$")


def kind(data: bytes) -> Optional[str]:
    """"docx", "xlsx" or None for anything else."""
    if data[:4] != b"PK\x03\x04":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return None
    return "docx" if "word/document.xml" in names else "xlsx" if "xl/workbook.xml" in names else None


def _xml(z: zipfile.ZipFile, name: str) -> Optional[ElementTree.Element]:
    try:
        entry = z.getinfo(name)
    except KeyError:
        return None
    if entry.file_size > MAX_PART:
        raise ApiError(413, "too_large", "the document unpacks to more than this server reads")
    raw = z.read(entry)
    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw[:4096]:
        raise ApiError(422, "bad_file", "the document carries definitions an office file does not need")  # entity bombs
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ApiError(422, "bad_file", "the document is damaged") from exc


def pages(data: bytes, what: str) -> list[str]:
    """The text of each page: one for a Word document, one per sheet of a workbook."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            out = [_word(z)] if what == "docx" else _sheets(z)
    except zipfile.BadZipFile as exc:
        raise ApiError(422, "bad_file", "the document is damaged") from exc
    out = [text for text in out if text.strip()] or [""]
    cap = get_settings().max_pages
    if len(out) > cap:
        raise ApiError(422, "too_many_pages", f"the workbook has {len(out)} sheets; the limit is {cap}")
    return out


def _paragraph(p: ElementTree.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("    ")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _word(z: zipfile.ZipFile) -> str:
    """Paragraphs in order; a table row as one line, its cells set apart, so a list of items or
    amounts stays a list."""
    root = _xml(z, "word/document.xml")
    body = root.find(W + "body") if root is not None else None
    lines: list[str] = []
    for block in (body if body is not None else []):
        if block.tag == W + "p":
            lines.append(_paragraph(block))
        elif block.tag == W + "tbl":
            for row in block.iter(W + "tr"):
                cells = [" ".join(_paragraph(p) for p in cell.iter(W + "p")).strip() for cell in row.findall(W + "tc")]
                lines.append("    ".join(cells))
    return "\n".join(line.rstrip() for line in lines).strip()


def _column(letters: str) -> int:
    n = 0
    for c in letters:
        n = n * 26 + ord(c) - 64
    return n - 1


def _sheets(z: zipfile.ZipFile) -> list[str]:
    strings: list[str] = []
    shared = _xml(z, "xl/sharedStrings.xml")
    for si in (shared if shared is not None else []):
        strings.append("".join(t.text or "" for t in si.iter(S + "t")))
    # which cell styles are dates: a date is stored as a count of days
    dates: set = set()
    styles = _xml(z, "xl/styles.xml")
    if styles is not None:
        custom = {int(f.get("numFmtId", 0)): f.get("formatCode", "") for f in styles.iter(S + "numFmt")}
        xfs = styles.find(S + "cellXfs")
        for n, xf in enumerate(xfs if xfs is not None else []):
            fmt = int(xf.get("numFmtId", 0))
            code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(fmt, "")).lower()
            if fmt in _DATE_FORMATS or ("y" in code and ("d" in code or "m" in code)) or ("d" in code and "m" in code):
                dates.add(n)
    book = _xml(z, "xl/workbook.xml")
    rels = _xml(z, "xl/_rels/workbook.xml.rels")
    target = {r.get("Id"): r.get("Target", "") for r in (rels if rels is not None else [])}
    epoch = date(1904, 1, 1) if book is not None and (book.find(S + "workbookPr") is not None
                                                      and book.find(S + "workbookPr").get("date1904") in ("1", "true")) else date(1899, 12, 30)
    out = []
    for sheet in (book.iter(S + "sheet") if book is not None else []):
        path = target.get(sheet.get(R + "id"), "")
        path = path.lstrip("/") if path.startswith("/") else "xl/" + path
        root = _xml(z, path)
        if root is None:
            continue
        rows: list[list[str]] = []
        for row in root.iter(S + "row"):
            cells: dict = {}
            for c in row.findall(S + "c"):
                m = _CELL.match(c.get("r", ""))
                at = _column(m.group(1)) if m else len(cells)
                v, kind_, style = c.find(S + "v"), c.get("t"), int(c.get("s", 0) or 0)
                if kind_ == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(S + "t"))
                elif v is None or v.text is None:
                    continue
                elif kind_ == "s":
                    text = strings[int(v.text)] if v.text.isdigit() and int(v.text) < len(strings) else ""
                elif kind_ == "b":
                    text = "TRUE" if v.text == "1" else "FALSE"
                elif style in dates and kind_ in (None, "n"):
                    try:
                        text = (epoch + timedelta(days=int(float(v.text)))).isoformat()
                    except (ValueError, OverflowError):
                        text = v.text
                else:
                    text = v.text
                    if kind_ in (None, "n") and re.fullmatch(r"-?\d+\.\d{3,}", text):
                        text = f"{float(text):.2f}"  # 1250.8000000000002 as it is stored: 1250.80 as it is shown
                if text.strip() and at < 200:
                    cells[at] = " ".join(text.split())
            if cells:
                rows.append([cells.get(k, "") for k in range(max(cells) + 1)])
        if not rows:
            continue
        # columns under each other, as on the sheet, so the amounts of a table stay in their columns
        widths = [min(max((len(r[k]) for r in rows if k < len(r)), default=0), 40) for k in range(max(len(r) for r in rows))]
        text = "\n".join("  ".join(cell.ljust(widths[k]) for k, cell in enumerate(r)).rstrip() for r in rows)
        out.append((sheet.get("name") or "") + "\n" + text if len(list(book.iter(S + "sheet"))) > 1 else text)
    return out
