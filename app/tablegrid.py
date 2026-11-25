"""Reads the structure of a table from its picture with a small model (SLANet-plus through
RapidTable, Apache-2.0): rows, columns and cells that span several of them. Use it for tables the
position-based reader cannot take: merged headings, ruled grids, cells of several lines."""
from __future__ import annotations

import logging
import threading
from html.parser import HTMLParser
from typing import Optional

import numpy as np

from . import engine
from .errors import ApiError

log = logging.getLogger("ocr.tablegrid")
_table = None
_lock = threading.Lock()


def _model():
    global _table
    with _lock:
        if _table is None:
            try:
                from rapid_table import ModelType, RapidTable, RapidTableInput
                logging.getLogger("RapidTable").setLevel(logging.ERROR)
                _table = RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS))
            except Exception as exc:  # noqa: BLE001 - not installed, or its model could not be fetched
                log.info("table structure reading is off: %s", exc)
                raise ApiError(503, "table_model_unavailable",
                               "the table structure model is not available on this server "
                               "(pip install rapid_table; it fetches its model on first use)") from exc
    return _table


class _Cells(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[dict] = []
        self._row, self._col, self._open, self._taken = -1, 0, None, set()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "tr":
            self._row, self._col = self._row + 1, 0
        elif tag in ("td", "th"):
            a = dict(attrs)
            while (self._row, self._col) in self._taken:  # a cell from the row above reaches down here
                self._col += 1
            span = lambda k: max(int(a.get(k) or 1), 1) if str(a.get(k) or "1").isdigit() else 1  # noqa: E731
            self._open = {"row": self._row, "column": self._col, "rowspan": span("rowspan"), "colspan": span("colspan"),
                          "text": ""}
            for r in range(self._open["rowspan"]):
                for c in range(self._open["colspan"]):
                    self._taken.add((self._row + r, self._col + c))
            self._col += self._open["colspan"]

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._open["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._open is not None:
            self._open["text"] = " ".join(self._open["text"].split())
            self.cells.append(self._open)
            self._open = None


def cells_of(html: str) -> list[dict]:
    parser = _Cells()
    parser.feed(html)
    return parser.cells


def read(img: np.ndarray) -> Optional[dict]:
    """The table on a BGR picture: its HTML, its cells (row, column, rowspan, colspan, text) and
    the same as a grid of rows in which a spanning cell is repeated in every place it covers."""
    eng = engine._engine()
    if eng is None:
        raise ApiError(503, "engine_unavailable", "the OCR engine is not loaded on this server")
    table = _model()
    with engine._run_lock:
        seen = eng(img)
        if seen is None or seen.txts is None:
            return None
        result = table(img, ocr_results=[(seen.boxes, seen.txts, seen.scores)])
    html = result.pred_htmls[0] if result.pred_htmls else ""
    cells = cells_of(html)
    if not cells:
        return None
    height = max(c["row"] + c["rowspan"] for c in cells)
    width = max(c["column"] + c["colspan"] for c in cells)
    grid = [["" for _ in range(width)] for _ in range(height)]
    for c in cells:
        for r in range(c["rowspan"]):
            for k in range(c["colspan"]):
                grid[c["row"] + r][c["column"] + k] = c["text"]
    return {"html": html, "cells": cells, "grid": grid,
            "merged_cells": sum(c["rowspan"] > 1 or c["colspan"] > 1 for c in cells)}
