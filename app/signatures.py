"""Finds handwritten signatures on a page with a small detector (YOLOS-tiny, Apache-2.0, 26 MB)
that runs on this machine. It says where a signature is and how sure it is; it does not say whose
it is or whether it is genuine. The model file is fetched once with scripts/get_models.sh."""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .config import get_settings

log = logging.getLogger("ocr.signatures")
URL = "https://huggingface.co/nuetzliches/yolos-tiny-signature-detection-onnx/resolve/main/model.onnx"
SHA256 = "d6d5bf2b9ddba9888e77846507a3da90bfd11b113a9556deaebaf132a42a5f7c"
THRESHOLD = 0.7  # on a public test set: 84% of signatures found, 73% of hits real; lower finds a little more, wrongly more often
_session = None
_failed = False
_lock = threading.Lock()


def model_path() -> Path:
    return Path(get_settings().data_dir) / "models" / "signature.onnx"


def _load():
    global _session, _failed
    with _lock:
        if _session is None and not _failed:
            path = model_path()
            try:
                if not path.exists():
                    raise FileNotFoundError(path)
                if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
                    raise ValueError("the signature model file is not the expected one")
                import onnxruntime as ort
                _session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001
                log.info("signature detection is off: %s", exc)
                _failed = True
    return _session


def available() -> bool:
    return _load() is not None


def find(img: np.ndarray, threshold: float = THRESHOLD) -> list[dict]:
    """Signatures on a BGR picture: each with its four corners and the detector's confidence."""
    import cv2
    session = _load()
    if session is None:
        return []
    h, w = img.shape[:2]
    rgb = cv2.cvtColor(cv2.resize(img, (640, 640), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    x = (rgb.astype(np.float32) / 255 - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    with _lock:
        logits, boxes = session.run(None, {"pixel_values": x.transpose(2, 0, 1)[None]})
    e = np.exp(logits[0] - logits[0].max(axis=1, keepdims=True))
    scores = (e / e.sum(axis=1, keepdims=True))[:, 0]  # the last class is "nothing here"
    out: list[dict] = []
    for score, (cx, cy, bw, bh) in sorted(zip(scores, boxes[0]), key=lambda p: -p[0]):
        if score < threshold:
            break
        x0, y0, x1, y1 = (cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h
        if any(_overlap((x0, y0, x1, y1), o["_xyxy"]) > 0.5 for o in out):
            continue  # the same signature found twice
        out.append({"confidence": round(float(score), 3), "_xyxy": (x0, y0, x1, y1),
                    "box": [[round(float(v), 1) for v in p] for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]})
    for o in out:
        del o["_xyxy"]
    return out


def _overlap(a: tuple, b: tuple) -> float:
    ix, iy = max(0.0, min(a[2], b[2]) - max(a[0], b[0])), max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / union if union > 0 else 0.0
