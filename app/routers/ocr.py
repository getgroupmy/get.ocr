"""The client API: read a file now (/v1/ocr) or queue it (/v1/jobs)."""
from __future__ import annotations
from typing import Optional

import base64
import re
import binascii
import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import extract, service, storage
from ..auth import require_client
from ..config import get_settings
from ..db import ApiClient, Correction, Job, get_db
from ..errors import ApiError, RequestTooLarge

router = APIRouter(prefix="/v1", tags=["ocr"])

_FILE_BODY = {
    "requestBody": {"required": True, "content": {
        "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary", "description": "Image (JPEG, PNG, WEBP, TIFF, BMP, GIF) or PDF"},
            "pages": {"type": "string", "description": "PDF pages to read, e.g. 1-3,7. Default: all"},
            "detail": {"type": "string", "enum": ["lines", "text"], "default": "lines",
                       "description": "'lines' adds every line with its position and confidence"},
            "pdf_password": {"type": "string",
                             "description": "The password of a protected PDF. Used once to open it and kept nowhere"},
            "force_ocr": {"type": "boolean", "default": False,
                          "description": "Read PDF pages as pictures even when they carry a text layer"},
            "type": {"type": "string", "enum": list(extract.TYPES),
                     "description": "/v1/extract: what the document is (default auto). "
                                    "/v1/jobs: send it to get fields instead of plain text"},
            "fields": {"type": "string",
                       "description": "With type=custom: the values to find. Names separated by commas, or JSON "
                                      '{"name": "string|number|date"} or {"name": {"type": ..., "description": ...}}'},
            "engine": {"type": "string", "enum": ["standard", "detect", "jawi", "arabic", "tamil", "thai", "glm", "minicpm"], "default": "standard",
                       "description": "'jawi' reads the lines in Jawi (Malay in Arabic script) with a model made "
                                      "for it and marks them \"script\": \"jawi\"; the other lines are read as usual. "
                                      "'detect' finds out by itself which of these scripts a page is in. 'arabic', 'tamil' and 'thai' do the same for those scripts (the model is fetched on first use). "
                                      "'glm' and 'minicpm' read each page with a vision model (handwriting, hard photos): "
                                      "slower, whole-page text, no line positions"},
            "task": {"type": "string", "enum": ["text", "table", "formula", "seal", "chart"], "default": "text",
                     "description": "With engine=glm or minicpm: what to ask the vision model for. "
                                    "'seal' (the words of a stamp) and 'chart' (a chart as a table) are minicpm only"},
            "marks": {"type": "boolean", "default": False,
                      "description": "Experimental: also report tick boxes (with the text beside each and whether "
                                     "it is ticked) and patches of stamp ink on every page"},
            "split": {"type": "boolean", "default": False,
                      "description": "Extraction only: the file holds several documents scanned together; "
                                     "answer with one entry per document under 'documents'"},
            "codes": {"type": "boolean", "default": False,
                      "description": "Also read QR codes and barcodes on every page (extraction always looks for them)"},
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
            fields = await request.form(max_files=1, max_fields=20)
        except RequestTooLarge:
            raise
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
                                    fields.get("fields"), _truthy(fields.get("codes", False)),
                                    _truthy(fields.get("split", False)), fields.get("engine"), fields.get("task"),
                                    _truthy(fields.get("marks", False)))
    password = fields.get("pdf_password")
    if password:  # used here once to open the file; it is not kept with the job or written anywhere
        from ..documents import unlock
        data = await run_in_threadpool(unlock, data, str(password)[:200])
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
    service.apply_corrections(db, client.id, result)
    service.note_duplicates(db, client.id, data, result)
    return result


@router.post("/searchable", openapi_extra=_FILE_BODY, summary="Turn a scan into a searchable PDF")
async def searchable_pdf(request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """Answers with a PDF of the same pages with the text that was read laid invisibly over each line,
    so it can be searched and copied. A PDF that already carries text is returned unchanged. Latin
    script only. Nothing is stored."""
    from .. import searchable
    data, options, _ = await read_input(request)
    try:
        pdf, pages = await run_in_threadpool(searchable.build, data, options.get("pages"), get_settings().sync_max_pages,
                                          options.get("engine") if options.get("engine") in ("detect", "jawi", "arabic", "tamil", "thai") else None)
    except ApiError:
        service.record_usage(db, client.id, "sync", False, 0, len(data), 0)
        db.commit()
        raise
    service.record_usage(db, client.id, "sync", True, pages, len(data), 0)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="searchable.pdf"'})


@router.get("/types", summary="The kinds of document /v1/extract understands")
def types(client: ApiClient = Depends(require_client)):
    """Each kind with its fields, the name of its list of lines, and its checks."""
    return {"types": extract.catalogue()}


@router.post("/corrections", status_code=201, summary="Report a value that was read wrongly")
async def add_correction(request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """`{"document_type": "invoice", "field": "supplier_name", "original": "ACME SUPPLlES", "corrected":
    "ACME SUPPLIES"}`. From then on, the same misreading of that text field in that kind of document is
    replaced for this client, and the answer says so under `corrected_from_memory`. Text fields only."""
    try:
        body = await request.json()
        kind, field = str(body["document_type"])[:40], str(body["field"])[:60]
        original, corrected = str(body["original"])[:500], str(body["corrected"])[:500]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(422, "bad_request", "send document_type, field, original and corrected") from exc
    if not original.strip() or not corrected.strip() or original == corrected:
        raise ApiError(422, "bad_request", "original and corrected must be two different pieces of text")
    row = Correction(client_id=client.id, match=service.correction_key(kind, field, original),
                     detail={"document_type": kind, "field": field, "original": original, "corrected": corrected})
    db.add(row)
    db.flush()
    return {"id": row.id, **row.detail}


@router.get("/corrections", summary="The corrections this client has reported")
def list_corrections(client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    rows = db.scalars(select(Correction).where(Correction.client_id == client.id).order_by(Correction.at.desc()).limit(500))
    return {"corrections": [{"id": r.id, "at": r.at.isoformat() + "Z", **r.detail} for r in rows]}


@router.delete("/corrections/{correction_id}", status_code=204, summary="Forget a correction")
def delete_correction(correction_id: int, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    row = db.get(Correction, correction_id)
    if row is None or row.client_id != client.id:
        raise ApiError(404, "not_found", "correction not found")
    db.delete(row)
    return Response(status_code=204)


@router.post("/match", summary="Compare extracted documents with each other")
async def match_documents(request: Request, client: ApiClient = Depends(require_client)):
    """Send the `fields` you got from /v1/extract. `{"compare": {"a": fields, "b": fields}}` sets an
    invoice against its purchase or delivery order, line by line. `{"reconcile": {"statement": fields,
    "documents": [{"ref": "your id", "fields": fields}], "days": 7}}` finds the statement line that
    pays each document. Nothing is read or stored; no model is used."""
    from .. import match
    try:
        body = await request.json()
        if "compare" in body:
            return match.compare(body["compare"]["a"], body["compare"]["b"])
        if "reconcile" in body:
            r = body["reconcile"]
            return match.reconcile(r["statement"], list(r["documents"]), int(r.get("days", 7)))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ApiError(422, "bad_request", "send {\"compare\": {\"a\": …, \"b\": …}} or "
                                           "{\"reconcile\": {\"statement\": …, \"documents\": […]}}") from exc
    raise ApiError(422, "bad_request", "send 'compare' or 'reconcile'")


@router.post("/extract", openapi_extra=_FILE_BODY, summary="Read a file and extract its fields")
async def extract_now(request: Request, client: ApiClient = Depends(require_client), db: Session = Depends(get_db)):
    """Reads an invoice, receipt, bank statement or order list and answers with
    its fields, its lines, and checks on them. The text is turned into fields by
    a language model running on this server; nothing is sent elsewhere and
    nothing is stored."""
    data, options, _ = await read_input(request)
    options["extract"] = options["extract"] or "auto"
    result = await _now(db, client, data, options, "extract")
    accept = request.headers.get("accept", "").lower()
    if "text/csv" in accept:
        layout = re.search(r"profile=\"?([a-z0-9-]+)", accept)
        text = bank_csv(result, layout.group(1)) if layout else rows_csv(result)
        return Response(text, media_type="text/csv; charset=utf-8")
    return result


def bank_csv(result: dict, layout: str) -> str:
    """A statement's transactions in a layout an accounting system imports.
    quickbooks-3: Date, Description, Amount (money out negative). quickbooks-4: Date, Description,
    Credit, Debit. Both as QuickBooks Online documents them: dates dd/mm/yyyy, no zeros, no thousands
    separators. generic: Date, Description, Reference, Money out, Money in, Balance, for systems whose
    import lets you map the columns (SQL Account, AutoCount, Bukku). xero: the columns of Xero's
    bank statement template (*Date, *Amount, Payee, Description, Reference, Check Number), money
    out negative; Payee is left empty, since a statement does not say who it is apart from the description."""
    import csv
    import io
    if layout not in ("quickbooks-3", "quickbooks-4", "xero", "generic"):
        raise ApiError(422, "bad_profile", "profile must be quickbooks-3, quickbooks-4, xero or generic")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    head = {"quickbooks-3": ["Date", "Description", "Amount"], "quickbooks-4": ["Date", "Description", "Credit", "Debit"],
            "xero": ["*Date", "*Amount", "Payee", "Description", "Reference", "Check Number"],
            "generic": ["Date", "Description", "Reference", "Money out", "Money in", "Balance"]}[layout]
    writer.writerow(head)

    def money(v) -> str:  # noqa: ANN001
        return "" if v in (None, 0) else f"{v:.2f}"

    for doc in result.get("documents") or [result]:
        for t in (doc.get("fields") or {}).get("transactions") or []:
            day = t.get("date") or ""
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = f"{day[8:]}/{day[5:7]}/{day[:4]}"
            text = " ".join(str(t.get("description") or "").split())
            if layout == "quickbooks-3":
                amount = (t.get("credit") or 0) - (t.get("debit") or 0)
                writer.writerow([day, text, money(amount)])
            elif layout == "quickbooks-4":
                writer.writerow([day, text, money(t.get("credit")), money(t.get("debit"))])
            elif layout == "xero":
                amount = (t.get("credit") or 0) - (t.get("debit") or 0)
                writer.writerow([day, f"{amount:.2f}", "", text, t.get("reference") or "", ""])
            else:
                writer.writerow([day, text, t.get("reference") or "", money(t.get("debit")), money(t.get("credit")),
                                 money(t.get("balance"))])
    return out.getvalue()


def rows_csv(result: dict) -> str:
    """The list of lines of an extraction (transactions, line items, rows) as CSV, for a spreadsheet.
    With several documents in the file, a first column says which one each row is from."""
    import csv
    import io
    documents = result.get("documents") or [result]
    out = io.StringIO()
    writer, header = csv.writer(out), None
    for n, doc in enumerate(documents, 1):
        rows = next((v for v in (doc.get("fields") or {}).values() if isinstance(v, list) and v
                     and isinstance(v[0], dict)), [])
        for row in rows:
            if header is None:
                header = list(row)
                writer.writerow((["document"] if len(documents) > 1 else []) + header)
            writer.writerow(([n] if len(documents) > 1 else []) + [row.get(k, "") if row.get(k) is not None else ""
                                                                 for k in header])
    return out.getvalue()


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
