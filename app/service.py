"""Reading a document, the job queue, and housekeeping."""
from __future__ import annotations
from typing import Optional

import logging
import queue
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import engine, extract, storage, webhooks
from .config import get_settings
from .db import ApiClient, Job, Usage, session_scope, utcnow
from .documents import Document, parse_pages
from .errors import ApiError

log = logging.getLogger("ocr.service")
DETAILS = ("lines", "text")


def clean_options(pages: Optional[str], detail: Optional[str], force_ocr: bool, extract_type: Optional[str] = None,
                  include_text: bool = False) -> dict:
    detail = (detail or "lines").lower()
    if detail not in DETAILS:
        raise ApiError(422, "bad_detail", "detail must be 'lines' or 'text'")
    extract_type = (extract_type or "").strip().lower() or None
    if extract_type and extract_type not in extract.TYPES:
        raise ApiError(422, "bad_type", "type must be one of: " + ", ".join(extract.TYPES))
    return {"pages": (pages or "").strip()[:200] or None, "detail": detail, "force_ocr": bool(force_ocr),
            "extract": extract_type, "include_text": bool(include_text)}


def recognise(data: bytes, options: dict, max_pages: int) -> dict:
    """Read the chosen pages of an image or PDF."""
    if not engine.available():
        raise ApiError(503, "engine_unavailable", "the OCR engine is not loaded on this server")
    started = time.monotonic()
    with Document(data) as doc:
        numbers = parse_pages(options.get("pages"), doc.page_count)
        if len(numbers) > max_pages:
            raise ApiError(422, "too_many_pages",
                           f"{len(numbers)} pages asked for, the limit here is {max_pages}; "
                           "use the 'pages' option, or POST /v1/jobs for long documents")
        pages = []
        for n in numbers:
            page = doc.page(n, use_text_layer=not options.get("force_ocr"), with_lines=bool(options.get("layer_lines")))
            out: dict = {"page": n, "width": page.width, "height": page.height}
            if page.text_layer is not None:
                out.update(source="text_layer", text=page.text_layer, confidence=None)
                lines: list[dict] = page.text_lines or []
            else:
                lines = engine.read(page.image)
                conf = round(sum(ln["confidence"] for ln in lines) / len(lines), 4) if lines else None
                out.update(source="ocr", text=engine.layout_text(lines), confidence=conf)
            if options.get("detail", "lines") == "lines":
                out["lines"] = lines
            pages.append(out)
        return {
            "engine": engine.NAME,
            "kind": doc.kind,
            "page_count": doc.page_count,
            "pages": pages,
            "text": "\n\n".join(p["text"] for p in pages),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }


def extract_fields(data: bytes, options: dict, max_pages: int) -> dict:
    """Read the document, then have the local model turn the text into fields."""
    from . import llm
    if not llm.ready():
        raise ApiError(503, "extraction_unavailable",
                       "field extraction is not available: the model server on this machine is not answering, "
                       "or the model is not installed")
    started = time.monotonic()
    ocr = recognise(data, {**options, "detail": "lines", "layer_lines": True}, max_pages)
    texts = [engine.layout_columns(p["lines"]) if p["lines"] else p["text"] for p in ocr["pages"]]
    out = extract.run(ocr["pages"], texts, options.get("extract") or "auto")
    out.update(engine=ocr["engine"], kind=ocr["kind"], page_count=ocr["page_count"],
               pages=[{k: p[k] for k in ("page", "width", "height", "source", "confidence")} for p in ocr["pages"]])
    if options.get("include_text"):
        out["text"] = ocr["text"]
    out["duration_ms"] = int((time.monotonic() - started) * 1000)
    return out


def run(data: bytes, options: dict, max_pages: int) -> dict:
    """Plain reading, or reading plus field extraction when the options ask for it."""
    return extract_fields(data, options, max_pages) if options.get("extract") else recognise(data, options, max_pages)


def record_usage(db: Session, client_id: str, kind: str, ok: bool, pages: int, size: int, ms: int) -> None:
    db.add(Usage(client_id=client_id, kind=kind, ok=ok, pages=pages, size_bytes=size, duration_ms=ms))


# --- job queue -----------------------------------------------------------------
_queue: "queue.Queue[Optional[str]]" = queue.Queue()
_stop = threading.Event()
_worker: Optional[threading.Thread] = None


def queue_length() -> int:
    return _queue.qsize()


def enqueue(job_id: str) -> None:
    _queue.put(job_id)


def process(job_id: str) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None or job.status not in ("queued", "processing"):
            return
        job.status = "processing"
        options, client_id, size = dict(job.options or {}), job.client_id, job.size_bytes
    started = time.monotonic()
    result, error = None, None
    try:
        result = run(storage.load(job_id), options, get_settings().max_pages)
    except ApiError as exc:
        error = (exc.code, exc.message)
    except FileNotFoundError:
        error = ("file_missing", "the uploaded file is no longer on the server")
    except Exception:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        error = ("internal_error", "the document could not be read")
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:  # deleted by the client while it was being read
            storage.delete(job_id)
            return
        job.completed_at = utcnow()
        if error:
            job.status, job.error, job.error_message = "failed", error[0], error[1][:300]
        else:
            job.status, job.result, job.pages = "completed", result, len(result["pages"])
        record_usage(db, client_id, "job", error is None, job.pages or 0, size,
                     int((time.monotonic() - started) * 1000))
        storage.delete(job_id)
        client = db.get(ApiClient, client_id)
        db.flush()
        if client is not None:
            webhooks.notify(job, client)


def _work() -> None:
    while not _stop.is_set():
        job_id = _queue.get()
        if job_id is None:
            break
        try:
            process(job_id)
        except Exception:  # noqa: BLE001
            log.exception("job %s could not be finished", job_id)


def housekeeping() -> None:
    """Delete stored results once the retention period is over."""
    days = get_settings().retention_days
    if days <= 0:
        return
    with session_scope() as db:
        old = db.scalars(select(Job).where(Job.completed_at < utcnow() - timedelta(days=days),
                                           Job.result_purged.is_(False)))
        for job in old:
            job.result, job.result_purged = None, True
            storage.delete(job.id)


def _keep_house() -> None:
    while not _stop.is_set():
        try:
            housekeeping()
        except Exception:  # noqa: BLE001
            log.exception("housekeeping failed")
        _stop.wait(600)


def start() -> None:
    """Start the worker, and pick up jobs a restart interrupted."""
    global _worker
    _stop.clear()
    with session_scope() as db:
        waiting = db.scalars(select(Job.id).where(Job.status.in_(("queued", "processing")))
                             .order_by(Job.created_at)).all()
    for job_id in waiting:
        enqueue(job_id)
    _worker = threading.Thread(target=_work, daemon=True, name="ocr-worker")
    _worker.start()
    threading.Thread(target=_keep_house, daemon=True, name="ocr-housekeeping").start()


def stop() -> None:
    _stop.set()
    _queue.put(None)
