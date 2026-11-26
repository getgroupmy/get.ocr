"""Reading a document, the job queue, and housekeeping."""
from __future__ import annotations
from typing import Optional

import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import codes, crypto, engine, extract, gaps, jawi, lists, marks, scripts, signatures, storage, vision, webhooks
from .config import get_settings
from .db import ApiClient, Correction, Job, Route, Seen, Usage, session_scope, utcnow
from .documents import Document, parse_pages
from .errors import ApiError

log = logging.getLogger("ocr.service")
DETAILS = ("lines", "text")
READERS = ("standard", "detect", "jawi", "arabic", "tamil", "thai", "glm", "vision")
VISION = ("glm", "vision")


def clean_options(pages: Optional[str], detail: Optional[str], force_ocr: bool, extract_type: Optional[str] = None,
                  include_text: bool = False, fields=None, codes: bool = False, split: bool = False,  # noqa: ANN001
                  reader: Optional[str] = None, task: Optional[str] = None, find_marks: bool = False) -> dict:
    reader = (reader or "standard").strip().lower()
    if reader not in READERS:
        raise ApiError(422, "bad_engine", "engine must be one of: " + ", ".join(READERS))
    task = (task or "text").strip().lower()
    if task not in vision.TASKS:
        raise ApiError(422, "bad_task", "task must be one of: " + ", ".join(vision.TASKS))
    if task in vision.ONLY_SECOND and reader != "vision":
        raise ApiError(422, "bad_task", f"task={task} is read by engine=vision only")
    detail = (detail or "lines").lower()
    if detail not in DETAILS:
        raise ApiError(422, "bad_detail", "detail must be 'lines' or 'text'")
    extract_type = (extract_type or "").strip().lower() or None
    if extract_type and extract_type not in extract.TYPES:
        raise ApiError(422, "bad_type", "type must be one of: " + ", ".join(extract.TYPES))
    wanted = extract.clean_fields(fields) if extract_type == "custom" else None
    if extract_type == "custom" and not wanted:
        raise ApiError(422, "bad_fields", "type=custom needs 'fields': the values to find in the document")
    return {"pages": (pages or "").strip()[:200] or None, "detail": detail, "force_ocr": bool(force_ocr),
            "extract": extract_type, "include_text": bool(include_text), "fields": wanted, "codes": bool(codes),
            "split": bool(split), "engine": reader, "task": task, "marks": bool(find_marks)}


def recognise(data: bytes, options: dict, max_pages: int) -> dict:
    """Read the chosen pages of an image or PDF."""
    reader = options.get("engine")
    glm = reader in VISION  # a vision model reads the page
    if glm and not vision.available(reader):
        raise ApiError(503, "reader_unavailable",
                       "the vision reader is not available: the model server on this machine is not answering, or "
                       + (f"its model is not installed (ollama pull {get_settings().vlm_model})" if reader == "glm"
                          else "its model is not installed (./scripts/get_vision_model.sh)"))
    if not glm and not engine.available():
        raise ApiError(503, "engine_unavailable", "the OCR engine is not loaded on this server")
    script = options.get("engine") if options.get("engine") in ("detect", "jawi") + tuple(scripts.SCRIPTS) else None
    if script == "jawi" and not jawi.available():
        raise ApiError(503, "reader_unavailable", "the Jawi reader is not available: its model file is missing from models/jawi")
    if script in scripts.SCRIPTS and not scripts.available(script):
        raise ApiError(503, "reader_unavailable",
                       f"the {script} reader is not available: its model could not be fetched (run scripts/get_models.sh)")
    started = time.monotonic()
    with Document(data) as doc:
        numbers = parse_pages(options.get("pages"), doc.page_count)
        if len(numbers) > max_pages:
            raise ApiError(422, "too_many_pages",
                           f"{len(numbers)} pages asked for, the limit here is {max_pages}; "
                           "use the 'pages' option, or POST /v1/jobs for long documents")
        if glm and doc.kind in ("docx", "xlsx"):
            raise ApiError(422, "bad_engine", "a Word or Excel file is read from its own text: leave the engine out")
        pages = []
        looked = 0  # text pages whose pictures were read as well
        for n in numbers:
            page = doc.page(n, use_text_layer=not options.get("force_ocr") and not glm,
                            with_lines=bool(options.get("layer_lines")) or options.get("detail", "lines") == "lines")
            out: dict = {"page": n, "width": page.width, "height": page.height}
            if glm:  # the vision model reads the picture: whole-page text, no positions or confidence
                out.update(source=reader, text=vision.read(page.image, options.get("task") or "text", reader), confidence=None,
                           quality=engine.quality(page.image))
                lines: list[dict] = []
                task = options.get("task") or "text"
                if task == "text" and "<table" not in out["text"] and engine.available():
                    # a vision model can skip a heading or small print: what the standard reader
                    # read clearly and the model left out is put in
                    out["text"], added = gaps.fill(out["text"], engine.read(page.image))
                    if added:
                        out["added_lines"] = added
                elif task == "table" and engine.available():
                    # the model writes out the table only: what else stands on the page is listed beside it
                    outside = gaps.beside(out["text"], engine.read(page.image))
                    if outside:
                        out["other_lines"] = outside
            elif page.text_layer is not None:
                out.update(source="text_layer", text=page.text_layer, confidence=None)
                lines = page.text_lines or []
                if looked < get_settings().picture_text_pages and engine.available() and doc.kind == "pdf":
                    found = doc.pictures(n)  # words that are part of a picture are not in the stored text
                    if found is not None:
                        picture, left, top, per_point = found
                        looked += 1
                        out["text"], added = gaps.fill(out["text"], engine.read(picture))
                        for a in added:  # positions on the page in points, as the stored text's are
                            a["box"] = [[round(left + x / per_point, 1), round(top + y / per_point, 1)] for x, y in a["box"]]
                        if added:
                            out["added_lines"] = added
                            if page.text_lines is not None:
                                lines = lines + [{**a, "added": True} for a in added]
            else:
                lines, upright, turned = engine.read_upright(page.image, script)
                if turned:  # the page lay on its side: positions are given on the page turned upright
                    page.image = upright
                    out.update(width=upright.shape[1], height=upright.shape[0], rotated=turned)
                conf = round(sum(ln["confidence"] for ln in lines) / len(lines), 4) if lines else None
                out.update(source="ocr", text=engine.layout_text(lines), confidence=conf,
                           quality=engine.quality(page.image))
            if options.get("detail", "lines") == "lines":
                out["lines"] = lines
            want_codes = options.get("codes")
            if want_codes == "some":  # extraction: every picture, and where a text PDF usually carries its code
                want_codes = page.image is not None or n in numbers[:2] or n == numbers[-1]
            office = doc.kind in ("docx", "xlsx")  # no picture to look at: only its own text
            if office:
                out["source"] = "document"
            if want_codes and not office:
                image = page.image if page.image is not None else doc.page(n, use_text_layer=False).image
                scale = page.width / image.shape[1]  # a text page is measured in points, its picture in pixels
                out["codes"] = [{**c, "box": [[round(x * scale, 1), round(y * scale, 1)] for x, y in c["box"]]}
                                for c in codes.find(image)]
            if page.image is not None and options.get("extract") in ("order", "auto") and any(
                    ln.get("struck") or ln["text"][:1].isdigit() for ln in lines):
                out["list_marks"] = lists.brackets(page.image, lines)  # for an order list: which rows are ticked
            if (options.get("marks") or options.get("preview")) and not office:
                image = page.image if page.image is not None else doc.page(n, use_text_layer=False).image
                scale = out["width"] / image.shape[1]

                def to_page(box: list) -> list:
                    return [[round(x * scale, 1), round(y * scale, 1)] for x, y in box]
                if options.get("marks"):
                    seen_lines = lines if page.image is not None else []  # a text page's lines are in points, not pixels
                    out["checkboxes"] = [{**c, "box": to_page(c["box"])} for c in
                                         marks.checkboxes(image, seen_lines) + lists.brackets(image, seen_lines)] \
                        if page.image is not None else []
                    out["stamps"] = [{**st, "box": to_page(st["box"])} for st in marks.stamps(image)]
                    if signatures.available():
                        out["signatures"] = [{**sg, "box": to_page(sg["box"])} for sg in signatures.find(image)]
                if options.get("preview") and len(pages) < 3:  # the console's review view: the page as it was read
                    import base64
                    import cv2
                    side = 1400 / max(image.shape[:2])
                    small = cv2.resize(image, None, fx=side, fy=side, interpolation=cv2.INTER_AREA) if side < 1 else image
                    ok, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    out["preview"] = "data:image/jpeg;base64," + base64.b64encode(jpeg.tobytes()).decode() if ok else None
            pages.append(out)
        return {
            "engine": vision.model(reader) if glm else engine.NAME + "+" + (jawi.NAME if script == "jawi" else "scripts" if script == "detect" else scripts.name(script)) if script
            else engine.NAME,
            "kind": doc.kind,
            "file": doc.info(),
            "page_count": doc.page_count,
            "pages": pages,
            "text": "\n\n".join(p["text"] for p in pages),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }


def table_grids(data: bytes, options: dict, max_pages: int) -> dict:
    """The structure of the table on each page, merged cells included, read from the page's picture."""
    from . import tablegrid
    started = time.monotonic()
    with Document(data) as doc:
        if doc.kind in ("docx", "xlsx"):
            raise ApiError(422, "bad_type", "type=table_grid reads a table from a picture; a Word or Excel file has none")
        numbers = parse_pages(options.get("pages"), doc.page_count)
        if len(numbers) > max_pages:
            raise ApiError(422, "too_many_pages", f"{len(numbers)} pages asked for, the limit here is {max_pages}")
        found, pages = [], []
        for n in numbers:
            page = doc.page(n, use_text_layer=False)
            _, upright, turned = engine.read_upright(page.image)
            table = tablegrid.read(upright)
            pages.append({"page": n, "width": upright.shape[1], "height": upright.shape[0], "source": "ocr",
                          "confidence": None, **({"rotated": turned} if turned else {})})
            if table:
                found.append({"page": n, **table})
        if not found:
            raise ApiError(422, "no_table", "no table was found on the page(s)")
        first = found[0]
        return {"document_type": "table_grid",
                "fields": {"html": first["html"], "merged_cells": first["merged_cells"], "cells": first["cells"],
                           "other_tables": found[1:], "grid": [{f"c{i + 1}": v for i, v in enumerate(row)} for row in first["grid"]]},
                "checks": [], "checks_passed": None, "not_found_in_text": [], "unreadable_dates": [], "sources": {},
                "evidence": {}, "balance_columns_swapped": 0, "truncated": False, "model": "slanet-plus",
                "model_calls": len(found), "read_by": "table_model", "engine": engine.NAME, "kind": doc.kind,
                "file": doc.info(), "page_count": doc.page_count, "pages": pages, "codes": [], "einvoice": None,
                "duration_ms": int((time.monotonic() - started) * 1000)}


def extract_fields(data: bytes, options: dict, max_pages: int) -> dict:
    """Read the document, then have the local model turn the text into fields."""
    from . import llm
    if options.get("extract") == "table_grid":
        return table_grids(data, options, max_pages)
    if options.get("extract") != "table" and not llm.ready():  # a table is read by position, without the model
        raise ApiError(503, "extraction_unavailable",
                       "field extraction is not available: the model server on this machine is not answering, "
                       "or the model is not installed")
    started = time.monotonic()
    ocr = recognise(data, {**options, "detail": "lines", "layer_lines": True, "codes": "some"}, max_pages)
    texts = [engine.layout_columns(p["lines"]) if p["lines"] else p["text"] for p in ocr["pages"]]
    kind = options.get("extract") or "auto"
    if options.get("split"):
        documents = []
        for group in extract.split_pages(texts):
            numbers = [ocr["pages"][i]["page"] for i in group]
            try:
                one = extract.run([ocr["pages"][i] for i in group], [texts[i] for i in group], kind, options.get("fields"))
            except ApiError as exc:  # one unreadable document does not lose the others
                one = {"error": exc.code, "message": exc.message}
            documents.append({"pages": numbers, **one})
        out = {"documents": documents}
    else:
        out = extract.run(ocr["pages"], texts, kind, options.get("fields"))
    out.update(engine=ocr["engine"], kind=ocr["kind"], page_count=ocr["page_count"], file=ocr["file"],
               pages=[{k: p.get(k) for k in ("page", "width", "height", "source", "confidence", "quality", "rotated",
                                             "checkboxes", "stamps", "signatures", "preview") if k in p}
                      for p in ocr["pages"]])
    out["codes"] = [{"page": p["page"], **c} for p in ocr["pages"] for c in p.get("codes") or []]
    out["einvoice"] = codes.einvoice(out["codes"])
    if options.get("include_text"):
        out["text"] = ocr["text"]
    out["duration_ms"] = int((time.monotonic() - started) * 1000)
    return out


def run(data: bytes, options: dict, max_pages: int) -> dict:
    """Plain reading, or reading plus field extraction when the options ask for it."""
    return extract_fields(data, options, max_pages) if options.get("extract") else recognise(data, options, max_pages)


def _fingerprint(doc: dict) -> Optional[str]:
    """What identifies a sales document whatever file it arrives in: who issued it, its number and its total."""
    f = doc.get("fields") or {}
    if doc.get("document_type") not in extract.SALE_TYPES or not f.get("document_number") or f.get("total") is None:
        return None
    parts = (doc["document_type"], re.sub(r"\W", "", str(f.get("supplier_name") or "").lower()),
             re.sub(r"\W", "", str(f["document_number"]).lower()), f"{f['total']:.2f}")
    return crypto.sha256_hex("|".join(parts))


def note_duplicates(db: Session, client_id: str, data: bytes, result: dict) -> None:
    """Add to the answer whether this client has sent the same file, or the same
    document in another file, before. Only hashes are remembered."""
    days = get_settings().duplicate_days
    if days <= 0:
        return

    def seen(kind: str, digest: Optional[str]) -> Optional[str]:
        if digest is None:
            return None
        first = db.scalar(select(Seen.at).where(Seen.client_id == client_id, Seen.kind == kind, Seen.digest == digest)
                          .order_by(Seen.at).limit(1))
        if first is None:
            db.add(Seen(client_id=client_id, kind=kind, digest=digest))
        return first.isoformat() + "Z" if first else None

    file_seen = seen("file", crypto.sha256_hex(data))
    documents = result.get("documents") or ([result] if "fields" in result else [])
    for doc in documents:
        doc["duplicate"] = {"same_document_first_seen": seen("content", _fingerprint(doc))}
    result["duplicate_file_first_seen"] = file_seen


def correction_key(document_type: str, field: str, original) -> str:  # noqa: ANN001
    return crypto.sha256_hex("|".join((document_type, field, " ".join(str(original).lower().split()))))


def apply_corrections(db: Session, client_id: str, result: dict) -> None:
    """Put right the misreadings this client has corrected before: a text field whose value is one
    it reported is replaced by what it said the value should be. Numbers and dates are never
    replaced, since the same wrong number does not mean the same right one."""
    for doc in result.get("documents") or ([result] if "fields" in result else []):
        fields, kind, changed = doc.get("fields") or {}, doc.get("document_type") or "", {}
        for name, value in fields.items():
            if not isinstance(value, str) or "date" in name:
                continue
            row = db.scalar(select(Correction).where(Correction.client_id == client_id,
                                                     Correction.match == correction_key(kind, name, value))
                            .order_by(Correction.at.desc()).limit(1))
            if row is not None and row.detail.get("corrected") != value:
                changed[name] = {"from": value, "to": row.detail["corrected"]}
                fields[name] = row.detail["corrected"]
        doc["corrected_from_memory"] = changed


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
    result, error, data = None, None, None
    try:
        data = storage.load(job_id)
        result = run(data, options, get_settings().max_pages)
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
            if data is not None:
                apply_corrections(db, client_id, result)
                note_duplicates(db, client_id, data, result)
            job.status, job.result, job.pages = "completed", result, len(result["pages"])
        record_usage(db, client_id, "job", error is None, job.pages or 0, size,
                     int((time.monotonic() - started) * 1000))
        storage.delete(job_id)
        client = db.get(ApiClient, client_id)
        db.flush()
        if client is not None:
            routes = {r.kind: r.url for r in db.scalars(select(Route).where(Route.client_id == client_id))}
            webhooks.notify(job, client, routes)


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
    with session_scope() as db:
        db.execute(delete(Seen).where(Seen.at < utcnow() - timedelta(days=max(get_settings().duplicate_days, 1))))


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
    for n in range(max(1, get_settings().workers)):
        _worker = threading.Thread(target=_work, daemon=True, name=f"ocr-worker-{n + 1}")
        _worker.start()
    if get_settings().alert_webhook:
        threading.Thread(target=_watch, daemon=True, name="ocr-alerts").start()
    threading.Thread(target=_keep_house, daemon=True, name="ocr-housekeeping").start()


def last_backup() -> Optional[datetime]:
    """When scripts/backup.sh last finished (it leaves the time in the data folder), or None."""
    try:
        text = (Path(get_settings().data_dir) / "last_backup").read_text().strip()
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError):
        return None


def problems() -> dict:
    """What is wrong right now, by name. Empty when all is well."""
    import shutil
    from . import llm
    s = get_settings()
    found = {}
    if not engine.available():
        found["reader"] = "the OCR engine is not loaded"
    if s.llm_url and not llm.ready(max_age=0):
        found["extraction"] = "the model server is not answering, or the extraction model is not installed"
    try:
        free = shutil.disk_usage(s.data_dir).free / 1e9
        if free < s.alert_disk_gb:
            found["disk"] = f"only {free:.1f} GB free where the data is kept"
    except OSError:
        found["disk"] = "the data folder cannot be read"
    last = last_backup()
    if (Path(s.data_dir) / "backup_scheduled").exists() and (last is None or utcnow() - last > timedelta(hours=36)):
        found["backup"] = "the nightly backup has not run for more than a day" if last else "the nightly backup has never run"
    with session_scope() as db:
        oldest = db.scalar(select(Job.created_at).where(Job.status.in_(("queued", "processing")))
                           .order_by(Job.created_at).limit(1))
    if oldest is not None and utcnow() - oldest > timedelta(hours=1):
        found["queue"] = "a document has been waiting for more than an hour"
    return found


def _watch() -> None:
    """Tell the alert address when something stops working and when it works again. The message
    names the problem only; it never carries document content."""
    import httpx
    known: dict = {}
    while not _stop.wait(60):
        try:
            now = problems()
            for name in set(now) | set(known):
                if (name in now) != (name in known):
                    body = {"service": "get.ocr", "problem": name, "state": "failing" if name in now else "recovered",
                            "detail": now.get(name) or known.get(name),
                            "text": f"get.ocr: {name} " + ("is failing: " + now[name] if name in now else "has recovered")}
                    httpx.post(get_settings().alert_webhook, json=body, timeout=10)
            known = now
        except Exception:  # noqa: BLE001
            log.exception("the alert check failed")


def stop() -> None:
    _stop.set()
    for _ in range(max(1, get_settings().workers)):
        _queue.put(None)
