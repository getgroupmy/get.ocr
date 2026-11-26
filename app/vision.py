"""The vision readers: small vision models that look at the picture of a page and write out what
it says. They run on the model server on this machine, like the extraction model. They read
handwriting and difficult pictures the standard reader cannot, but give the text of the whole
page with no position or confidence for each line, and they are slower.

Two can be installed side by side: GLM-OCR ("glm") and MiniCPM-OCR 1.0 ("minicpm")."""
from __future__ import annotations

import html
import re

import numpy as np

from . import llm
from .config import get_settings

NAME = "glm-ocr"
# The prompts the models were trained on, as their model cards give them. Both take the first three.
TASKS = {"text": "Text Recognition:", "table": "Table Recognition:", "formula": "Formula Recognition:",
         "seal": "Seal Recognition:", "chart": "Chart to Table:"}
ONLY_MINICPM = ("seal", "chart")
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*$", re.S)
_OTSL = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|ched|rhed|srow|nl)>")


def model(reader: str = "glm") -> str:
    s = get_settings()
    return s.vlm2_model if reader == "minicpm" else s.vlm_model


def available(reader: str = "glm") -> bool:
    return llm.vision_ready(model(reader))


def otsl_to_html(text: str) -> str:
    """MiniCPM-OCR writes a table as a run of cell marks (OTSL): <fcel>text for a cell, <ecel> for
    an empty one, <lcel> and <ucel> for a place taken by the cell to its left or above, <nl> at
    the end of a row. Turned into an HTML table here, which is what GLM-OCR gives and what the
    rest of the service reads."""
    body = re.sub(r"</?otsl>", "", text)
    marks = list(_OTSL.finditer(body))
    if not marks:
        return text
    rows: list[list] = [[]]
    for n, m in enumerate(marks):
        kind = m.group(1)
        if kind == "nl":
            rows.append([])
            continue
        content = body[m.end():marks[n + 1].start() if n + 1 < len(marks) else len(body)].strip()
        rows[-1].append((kind, content))
    rows = [r for r in rows if r]
    cells: dict = {}  # (row, column) of a cell's top-left corner -> [text, rowspan, colspan, heading]
    owner: dict = {}
    for r, row in enumerate(rows):
        for c, (kind, content) in enumerate(row):
            above, left = owner.get((r - 1, c)), owner.get((r, c - 1))
            if kind == "lcel" and left:
                owner[(r, c)] = left
                cells[left][2] = max(cells[left][2], c - left[1] + 1)
            elif kind == "ucel" and above:
                owner[(r, c)] = above
                cells[above][1] = max(cells[above][1], r - above[0] + 1)
            elif kind == "xcel" and (above or left):
                owner[(r, c)] = above or left
            else:
                owner[(r, c)] = (r, c)
                cells[(r, c)] = [content, 1, 1, kind in ("ched", "rhed")]
    out = ["<table>"]
    for r in range(len(rows)):
        out.append("<tr>")
        for c in range(len(rows[r])):
            if (r, c) in cells:
                content, down, across, heading = cells[(r, c)]
                tag = "th" if heading else "td"
                span = (f' rowspan="{down}"' if down > 1 else "") + (f' colspan="{across}"' if across > 1 else "")
                out.append(f"<{tag}{span}>{html.escape(content)}</{tag}>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def read(img: np.ndarray, task: str = "text", reader: str = "glm") -> str:
    """The text of a BGR picture. `task` chooses what is asked for: the text, a table, a formula,
    the words of a stamp, or the numbers of a chart. `reader` chooses the model."""
    import cv2
    side = get_settings().vlm_max_side
    h, w = img.shape[:2]
    if max(h, w) > side:
        scale = side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError("the page could not be encoded as a picture")
    text = llm.read_image(TASKS[task], jpeg.tobytes(), model(reader)).strip()
    fenced = _FENCE.match(text)  # some answers come wrapped as a block of code
    text = (fenced.group(1) if fenced else text).strip()
    return otsl_to_html(text) if "<fcel>" in text or "<otsl>" in text else text
