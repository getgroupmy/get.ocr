"""QR codes and barcodes on a page. Read by OpenCV on this machine; a code is
decoded exactly or not at all, so there is nothing to guess."""
from __future__ import annotations

import logging
import re
from typing import Optional

import numpy as np

log = logging.getLogger("ocr.codes")
_EINVOICE = re.compile(r"https?://(?:preprod\.)?myinvois\.hasil\.gov\.my/([A-Za-z0-9]{10,40})/share/([A-Za-z0-9]+)", re.I)


def _box(points) -> list:  # noqa: ANN001
    return [[round(float(x), 1), round(float(y), 1)] for x, y in np.asarray(points).reshape(-1, 2)[:4]]


def find(img: np.ndarray) -> list[dict]:
    """Every QR code and barcode on a BGR image: its kind, what it says and its four corners."""
    import cv2
    out: list[dict] = []
    try:
        ok, texts, points, _ = cv2.QRCodeDetector().detectAndDecodeMulti(img)
        if ok:
            out += [{"kind": "qr", "text": t, "box": _box(p)} for t, p in zip(texts, points) if t]
    except cv2.error:
        log.debug("QR detection failed", exc_info=True)
    try:
        found = cv2.barcode.BarcodeDetector().detectAndDecodeWithType(img)
        ok, texts, kinds, points = found[0], found[1], found[2], found[3]
        if ok:
            out += [{"kind": str(k).lower() or "barcode", "text": t, "box": _box(p)}
                    for t, k, p in zip(texts, kinds, points) if t]
    except (cv2.error, AttributeError, IndexError):
        log.debug("barcode detection failed", exc_info=True)
    seen, unique = set(), []
    for c in out:
        if (c["kind"], c["text"]) not in seen:
            seen.add((c["kind"], c["text"]))
            unique.append(c)
    return unique


def einvoice(codes: list[dict]) -> Optional[dict]:
    """The LHDN MyInvois validation link an e-invoice carries in its QR code, when one of the codes is that link."""
    for c in codes:
        m = _EINVOICE.search(c["text"])
        if m:
            return {"uuid": m.group(1), "url": m.group(0), "page": c.get("page")}
    return None
