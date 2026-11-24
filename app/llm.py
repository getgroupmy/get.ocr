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
