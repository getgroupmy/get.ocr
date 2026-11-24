"""The language model that turns read text into fields. It runs on this machine
(Ollama by default); nothing is sent anywhere else."""
from __future__ import annotations

import json
import threading
import time

import httpx

from .config import get_settings
from .errors import ApiError

_lock = threading.Lock()  # one request at a time: a local model uses the whole machine
_status: tuple[float, bool] = (0.0, False)


def _base() -> str:
    return get_settings().llm_url.rstrip("/")


def ready(max_age: float = 30.0) -> bool:
    """Is the model server answering and the configured model installed? Cached briefly."""
    global _status
    s = get_settings()
    if not s.llm_url:
        return False
    if time.monotonic() - _status[0] < max_age and _status[0]:
        return _status[1]
    ok = False
    try:
        if s.llm_api == "ollama":
            names = [m.get("name", "") for m in httpx.get(_base() + "/api/tags", timeout=2).json().get("models", [])]
            want = s.llm_model if ":" in s.llm_model else s.llm_model + ":latest"
            ok = want in names
        else:
            ok = s.llm_model in [m.get("id") for m in httpx.get(_base() + "/models", timeout=2).json().get("data", [])]
    except Exception:  # noqa: BLE001 - not running, wrong address, not JSON
        ok = False
    _status = (time.monotonic(), ok)
    return ok


_vision_status: tuple[float, bool] = (0.0, False)


def vision_ready(max_age: float = 30.0) -> bool:
    """Is the vision reader's model installed on the model server? Ollama only. Cached briefly."""
    global _vision_status
    s = get_settings()
    if not s.llm_url or not s.vlm_model or s.llm_api != "ollama":
        return False
    if time.monotonic() - _vision_status[0] < max_age and _vision_status[0]:
        return _vision_status[1]
    try:
        names = [m.get("name", "") for m in httpx.get(_base() + "/api/tags", timeout=2).json().get("models", [])]
        ok = (s.vlm_model if ":" in s.vlm_model else s.vlm_model + ":latest") in names
    except Exception:  # noqa: BLE001
        ok = False
    _vision_status = (time.monotonic(), ok)
    return ok


def read_image(prompt: str, image: bytes) -> str:
    """What the vision model answers for one picture (JPEG or PNG bytes). The picture goes to the
    model server on this machine as base64, in the message's `images`, as Ollama's chat API takes it."""
    import base64
    s = get_settings()
    body = {"model": s.vlm_model, "stream": False, "keep_alive": "30m",
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}],
            "options": {"temperature": 0, "num_predict": s.vlm_max_tokens}}
    try:
        with _lock:
            r = httpx.post(_base() + "/api/chat", json=body, timeout=s.vlm_timeout_seconds)
    except httpx.TimeoutException as exc:
        raise ApiError(504, "reader_timeout", "the vision reader took too long on a page") from exc
    except httpx.HTTPError as exc:
        raise ApiError(503, "reader_unavailable", "the model server on this machine is not answering") from exc
    if r.status_code == 404:
        raise ApiError(503, "reader_unavailable", f"the model '{s.vlm_model}' is not installed on the model server")
    if r.status_code >= 300:
        raise ApiError(503, "reader_unavailable", f"the model server answered HTTP {r.status_code}")
    try:
        return str(r.json()["message"]["content"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(502, "reader_failed", "the vision reader did not answer with text") from exc


def chat_json(system: str, user: str, schema: dict) -> dict:
    """One answer from the model as JSON that follows `schema`."""
    s = get_settings()
    if not s.llm_url:
        raise ApiError(503, "extraction_unavailable", "field extraction is turned off on this server (OCR_LLM_URL)")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if s.llm_api == "ollama":
        url = _base() + "/api/chat"
        body = {"model": s.llm_model, "messages": messages, "stream": False, "format": schema, "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": s.llm_context}}
    else:
        url = _base() + "/chat/completions"
        body = {"model": s.llm_model, "messages": messages, "temperature": 0,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "extraction", "schema": schema, "strict": True}}}
    try:
        with _lock:
            r = httpx.post(url, json=body, timeout=s.llm_timeout_seconds)
    except httpx.TimeoutException as exc:
        raise ApiError(504, "extraction_timeout", "the model took too long; try fewer pages or queue a job") from exc
    except httpx.HTTPError as exc:
        raise ApiError(503, "extraction_unavailable", "the model server on this machine is not answering") from exc
    if r.status_code == 404:
        raise ApiError(503, "extraction_unavailable", f"the model '{s.llm_model}' is not installed on the model server")
    if r.status_code >= 300:
        raise ApiError(503, "extraction_unavailable", f"the model server answered HTTP {r.status_code}")
    try:
        data = r.json()
        content = data["message"]["content"] if s.llm_api == "ollama" else data["choices"][0]["message"]["content"]
        out = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ApiError(502, "extraction_failed", "the model did not answer with usable JSON") from exc
    if not isinstance(out, dict):
        raise ApiError(502, "extraction_failed", "the model did not answer with usable JSON")
    return out
