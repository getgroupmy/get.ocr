"""The client API: read a file now (/v1/ocr) or queue it (/v1/jobs)."""
from __future__ import annotations
from typing import Optional

import base64
import binascii
import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import extract, service, storage
from ..auth import require_client
from ..config import get_settings
from ..db import ApiClient, Job, get_db
from ..errors import ApiError

router = APIRouter(prefix="/v1", tags=["ocr"])

_FILE_BODY = {
    "requestBody": {"required": True, "content": {
        "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary", "description": "Image (JPEG, PNG, WEBP, TIFF, BMP, GIF) or PDF"},
            "pages": {"type": "string", "description": "PDF pages to read, e.g. 1-3,7. Default: all"},
            "detail": {"type": "string", "enum": ["lines", "text"], "default": "lines",
                       "description": "'lines' adds every line with its position and confidence"},
            "force_ocr": {"type": "boolean", "default": False,
                          "description": "Read PDF pages as pictures even when they carry a text layer"},
            "type": {"type": "string", "enum": list(extract.TYPES),
                     "description": "/v1/extract: what the document is (default auto). "
                                    "/v1/jobs: send it to get fields instead of plain text"},
            "fields": {"type": "string",
                       "description": "With type=custom: the values to find. Names separated by commas, or JSON "
                                      '{"name": "string|number|date"} or {"name": {"type": ..., "description": ...}}'},
            "include_text": {"type": "boolean", "default": False,
                             "description": "Extraction only: also return the full text that was read"},
            "external_ref": {"type": "string", "description": "Jobs only: your own id for this document"},
        }}},
        "application/json": {"schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "description": "The file, base64 encoded"},
            "pages": {"type": "string"}, "detail": {"type": "string", "enum": ["lines", "text"]},
            "force_ocr": {"type": "boolean"}, "type": {"type": "string"}, "include_text": {"type": "boolean"},
            "external_ref": {"type": "string"},
        }}},
    }},
}


def _truthy(v) -> bool:  # noqa: ANN001
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


async def read_input(request: Request) -> tuple[bytes, dict, Optional[str]]:
    """The uploaded file, the reading options and external_ref, from a
    multipart form or a JSON body with the file in base64."""
    limit = get_settings().max_upload_bytes
    too_large = ApiError(413, "too_large", f"the file is larger than {get_settings().max_upload_mb} MB")
    ctype = request.headers.get("content-type", "").lower()
    is_json = ctype.startswith("application/json")
    declared = request.headers.get("content-length", "")
    allowed = limit * 4 // 3 + 65536 if is_json else limit + 65536  # base64 is a third larger
    if declared.isdigit() and int(declared) > allowed:
        raise too_large
    if is_json:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > allowed:
                raise too_large
        try:
            import json
            fields = json.loads(bytes(raw))
            data = base64.b64decode(fields["file"], validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise ApiError(422, "bad_request", 'send JSON like {"file": "<base64>"}') from exc
    elif ctype.startswith("multipart/form-data"):
        try:
            fields = await request.form(max_files=1, max_fields=10)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(422, "bad_request", "the form could not be read") from exc
        upload = fields.get("file")
        if upload is None or isinstance(upload, str):
            raise ApiError(422, "bad_request", "send the document in a form field named 'file'")
        data = await upload.read(limit + 1)
    else:
        raise ApiError(415, "bad_request", "send multipart/form-data with a 'file' field, or JSON with base64 'file'")
    if len(data) > limit:
        raise too_large
    if not data:
        raise ApiError(422, "bad_request", "the file is empty")
    options = service.clean_options(fields.get("pages"), fields.get("detail"), _truthy(fields.get("force_ocr", False)),
                                    fields.get("type"), _truthy(fields.get("include_text", False)),
                                    fields.get("fields"))
    ref = fields.get("external_ref")
    return data, options, (str(ref)[:200] if ref else None)


@router.post("/ocr", openapi_extra=_FILE_BODY, summary="Read a file now")
async def ocr_now(request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """Reads an image or a short PDF and answers with the text. Nothing is stored:
    the file and the text exist only in memory for the length of the request."""
    data, options, _ = await read_input(request)
    options["extract"] = None  # plain reading here; fields come from /v1/extract
    return await _now(db, client, data, options, "sync")


async def _now(db: Session, client: ApiClient, data: bytes, options: dict, kind: str) -> dict:
    try:
        result = await run_in_threadpool(service.run, data, options, get_settings().sync_max_pages)
    except ApiError:
        service.record_usage(db, client.id, kind, False, 0, len(data), 0)
        db.commit()
        raise
    service.record_usage(db, client.id, kind, True, len(result["pages"]), len(data), result["duration_ms"])
    return result


@router.post("/extract", openapi_extra=_FILE_BODY, summary="Read a file and extract its fields")
async def extract_now(request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """Reads an invoice, receipt, bank statement or order list and answers with
    its fields, its lines, and checks on them. The text is turned into fields by
    a language model running on this server; nothing is sent elsewhere and
    nothing is stored."""
    data, options, _ = await read_input(request)
    options["extract"] = options["extract"] or "auto"
    return await _now(db, client, data, options, "extract")


def job_out(job: Job, with_result: bool = True) -> dict:
    out = {
        "id": job.id,
        "status": job.status,
        "external_ref": job.external_ref,
        "pages": job.pages,
        "error": job.error,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() + "Z",
        "completed_at": job.completed_at.isoformat() + "Z" if job.completed_at else None,
        "result_available": job.status == "completed" and not job.result_purged,
    }
    if with_result:
        out["result"] = job.result if job.status == "completed" else None
    return out


def _get(db: Session, client: ApiClient, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.client_id != client.id:
        raise ApiError(404, "not_found", "job not found")
    return job


@router.post("/jobs", status_code=202, openapi_extra=_FILE_BODY, summary="Queue a file")
async def create_job(request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """For long PDFs. Answers at once with a job id; the result comes by signed
    webhook, or poll GET /v1/jobs/{id}. The file is kept encrypted until it has
    been read, then deleted."""
    data, options, ref = await read_input(request)
    waiting = db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("queued", "processing"))))
    if waiting >= get_settings().max_queue:
        raise ApiError(429, "queue_full", "too many documents are waiting; try again shortly")
    job = Job(id="job_" + secrets.token_hex(12), client_id=client.id, external_ref=ref, options=options,
              size_bytes=len(data))
    storage.save(job.id, data)
    db.add(job)
    db.commit()
    service.enqueue(job.id)
    return job_out(job, with_result=False)


@router.get("/jobs", summary="List your jobs")
def list_jobs(status: Optional[str] = None, external_ref: Optional[str] = None, limit: int = 50,
              client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    q = select(Job).where(Job.client_id == client.id).order_by(Job.created_at.desc()).limit(max(1, min(limit, 200)))
    if status:
        q = q.where(Job.status == status)
    if external_ref:
        q = q.where(Job.external_ref == external_ref)
    return {"jobs": [job_out(j, with_result=False) for j in db.scalars(q)]}


@router.get("/jobs/{job_id}", summary="A job and, once completed, its result")
def get_job(job_id: str, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    return job_out(_get(db, client, job_id))


@router.delete("/jobs/{job_id}", status_code=204, summary="Delete a job, its file and its result")
def delete_job(job_id: str, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    job = _get(db, client, job_id)
    storage.delete(job.id)
    db.delete(job)
    return Response(status_code=204)
