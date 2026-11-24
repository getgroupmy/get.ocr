"""The second reader: a small vision model (GLM-OCR by default) that looks at
the picture of a page and writes out what it says. It runs on the model server
on this machine, like the extraction model. It reads handwriting and difficult
pictures the standard reader cannot, but gives the text of the whole page with
no position or confidence for each line, and it is slower."""
from __future__ import annotations

import re

import numpy as np

from . import llm
from .config import get_settings

NAME = "glm-ocr"
# The prompts the model was trained on, as its model card gives them.
TASKS = {"text": "Text Recognition:", "table": "Table Recognition:", "formula": "Formula Recognition:"}
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*$", re.S)


def available() -> bool:
    return llm.vision_ready()


def read(img: np.ndarray, task: str = "text") -> str:
    """The text of a BGR picture. `task` chooses what is asked for: the text, a table, or a formula."""
    import cv2
    side = get_settings().vlm_max_side
    h, w = img.shape[:2]
    if max(h, w) > side:
        scale = side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError("the page could not be encoded as a picture")
    text = llm.read_image(TASKS[task], jpeg.tobytes()).strip()
    fenced = _FENCE.match(text)  # some answers come wrapped as a block of code
    return (fenced.group(1) if fenced else text).strip()
