"""The console's API: API clients, usage, and a test reading. Admin token only."""
from __future__ import annotations
from typing import Optional

import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .. import crypto, extract, service
from ..auth import new_api_key, require_admin
from ..config import get_settings
from ..db import ApiClient, Audit, Job, Usage, get_db
from ..errors import ApiError
from .ocr import job_out, read_input

router = APIRouter(prefix="/admin/api", tags=["admin"], dependencies=[Depends(require_admin)])


class CreateClient(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    webhook_url: Optional[str] = Field(default=None, max_length=500)


class WebhookChange(BaseModel):
    webhook_url: Optional[str] = Field(default=None, max_length=500)


def _check_url(url: Optional[str]) -> Optional[str]:
    url = (url or "").strip() or None
    if url and not url.lower().startswith(("https://", "http://")):
        raise ApiError(422, "bad_webhook_url", "the webhook URL must start with https:// or http://")
    return url


def _client_out(c: ApiClient, usage: Optional[dict] = None) -> dict:
    u = usage or {}
    return {"id": c.id, "name": c.name, "key_prefix": c.key_prefix, "webhook_url": c.webhook_url, "active": c.active,
            "created_at": c.created_at.isoformat() + "Z", "requests": u.get("requests", 0), "pages": u.get("pages", 0),
            "last_used_at": u.get("last_used_at")}


def _audit(db: Session, request: Request, action: str, target: Optional[str] = None) -> None:
    origin = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
    db.add(Audit(action=action, target=(target or "")[:160] or None, origin=(origin or "")[:60] or None))


def _client(db: Session, cid: str) -> ApiClient:
    c = db.get(ApiClient, cid)
    if c is None:
        raise ApiError(404, "not_found", "client not found")
    return c


@router.post("/clients", status_code=201)
def create_client(body: CreateClient, request: Request, db: Session = Depends(get_db)):
    key = new_api_key()
    url = _check_url(body.webhook_url)
    secret = secrets.token_urlsafe(32) if url else None
    c = ApiClient(id="cli_" + secrets.token_hex(8), name=body.name.strip(), key_hash=crypto.sha256_hex(key),
                  key_prefix=key[:9], webhook_url=url, webhook_secret=secret.encode() if secret else None)
    db.add(c)
    _audit(db, request, "client.created", f"{c.id} {c.name}")
    db.flush()
    # The key and the webhook secret are shown this once; only a hash of the key is kept.
    return {**_client_out(c), "api_key": key, "webhook_secret": secret}


@router.get("/clients")
def list_clients(db: Session = Depends(get_db)):
    rows = db.execute(select(Usage.client_id, func.count(), func.coalesce(func.sum(Usage.pages), 0), func.max(Usage.at))
                      .group_by(Usage.client_id))
    usage = {cid: {"requests": n, "pages": pages, "last_used_at": last.isoformat() + "Z" if last else None}
             for cid, n, pages, last in rows}
    clients = db.scalars(select(ApiClient).order_by(ApiClient.created_at.desc()))
    return {"clients": [_client_out(c, usage.get(c.id)) for c in clients]}


@router.put("/clients/{cid}/webhook")
def set_webhook(cid: str, body: WebhookChange, request: Request, db: Session = Depends(get_db)):
    """Set or clear the webhook. Setting it makes a new signing secret, shown once."""
    c = _client(db, cid)
    url = _check_url(body.webhook_url)
    secret = secrets.token_urlsafe(32) if url else None
    c.webhook_url, c.webhook_secret = url, secret.encode() if secret else None
    _audit(db, request, "client.webhook_set" if url else "client.webhook_cleared", f"{c.id} {c.name}")
    return {**_client_out(c), "webhook_secret": secret}


@router.delete("/clients/{cid}", status_code=204)
def revoke_client(cid: str, request: Request, db: Session = Depends(get_db)):
    """The key stops working at once. Usage history is kept."""
    c = _client(db, cid)
    c.active = False
    _audit(db, request, "client.revoked", f"{c.id} {c.name}")
    return Response(status_code=204)


@router.get("/audit")
def audit(limit: int = 100, db: Session = Depends(get_db)):
    """What was done in the console, newest first."""
    rows = db.scalars(select(Audit).order_by(Audit.at.desc()).limit(max(1, min(limit, 500))))
    return {"events": [{"at": a.at.isoformat() + "Z", "action": a.action, "target": a.target, "origin": a.origin}
                       for a in rows]}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    total, ok, pages, ms = db.execute(select(
        func.count(), func.coalesce(func.sum(case((Usage.ok, 1), else_=0)), 0),
        func.coalesce(func.sum(Usage.pages), 0), func.coalesce(func.sum(Usage.duration_ms), 0))).one()
    by_status = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    s = get_settings()
    return {"requests": total, "failed": total - ok, "pages": pages,
            "seconds_per_page": round(ms / 1000 / pages, 2) if pages else None,
            "jobs": by_status, "queue": service.queue_length(),
            "types": [{"type": t["type"], "label": t["label"]} for t in extract.catalogue()],
            "limits": {"max_upload_mb": s.max_upload_mb, "sync_max_pages": s.sync_max_pages, "max_pages": s.max_pages,
                       "retention_days": s.retention_days}}


@router.get("/jobs")
def list_jobs(limit: int = 100, db: Session = Depends(get_db)):
    """Recent jobs of every client: status and counts, never the text."""
    names = dict(db.execute(select(ApiClient.id, ApiClient.name)).all())
    jobs = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500))))
    return {"jobs": [{**job_out(j, with_result=False), "client": names.get(j.client_id)} for j in jobs]}


@router.post("/try")
async def try_it(request: Request):
    """Read a file from the console. Not stored and not counted as usage."""
    data, options, _ = await read_input(request)
    if request.headers.get("content-type", "").lower().startswith("multipart/"):
        from .. import llm
        llm.use_model((await request.form()).get("model"))  # try another installed model for this one reading
    # the console is the administrator's own: whole documents, as a job would take them
    return await run_in_threadpool(service.run, data, options, get_settings().max_pages)
