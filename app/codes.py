"""QR codes and barcodes on a page, read on this machine by ZXing. A code is decoded exactly or
not at all, so there is nothing to guess."""
from __future__ import annotations

import logging
import re
from typing import Optional

import numpy as np

log = logging.getLogger("ocr.codes")
_EINVOICE = re.compile(r"https?://(?:preprod\.)?myinvois\.hasil\.gov\.my/([A-Za-z0-9]{10,40})/share/([A-Za-z0-9]+)", re.I)


def _box(points) -> list:  # noqa: ANN001
    return [[round(float(x), 1), round(float(y), 1)] for x, y in np.asarray(points).reshape(-1, 2)[:4]]


def _zxing(img: np.ndarray) -> list[dict]:
    """QR codes and barcodes read by ZXing: QR, Data Matrix, PDF417, Aztec, Code 128, Code 39, ITF, EAN, UPC."""
    import zxingcpp
    out = []
    for c in zxingcpp.read_barcodes(img):
        if not c.text:
            continue
        p = c.position
        kind = "qr" if "QR" in c.format.name.upper() else re.sub(r"[^a-z0-9]+", "_", c.format.name.lower()).strip("_")
        out.append({"kind": kind, "text": c.text,
                    "box": [[float(p.top_left.x), float(p.top_left.y)], [float(p.top_right.x), float(p.top_right.y)],
                            [float(p.bottom_right.x), float(p.bottom_right.y)],
                            [float(p.bottom_left.x), float(p.bottom_left.y)]]})
    return out


def _opencv(img: np.ndarray) -> list[dict]:
    """The fallback when ZXing is not installed: OpenCV reads QR codes and EAN/UPC only, and misses many."""
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
    return out


def find(img: np.ndarray) -> list[dict]:
    """Every QR code and barcode on a BGR image: its kind, what it says and its four corners."""
    try:
        out = _zxing(img)
    except ImportError:
        out = _opencv(img)
    except Exception:  # noqa: BLE001 - a picture the decoder cannot take is a page without codes
        log.debug("code reading failed", exc_info=True)
        out = []
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
