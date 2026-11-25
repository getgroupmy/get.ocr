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
from ..config import get_settings, resolves_to_internal
from ..db import AdminUser, ApiClient, Audit, Job, Route, Usage, get_db
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
    if url is None:
        return None
    if not url.lower().startswith(("https://", "http://")):
        raise ApiError(422, "bad_webhook_url", "the webhook URL must start with https:// or http://")
    if resolves_to_internal(url):
        raise ApiError(422, "bad_webhook_url",
                       "the webhook URL must be a public address, not localhost or a private or internal host")
    return url


def _client_out(c: ApiClient, usage: Optional[dict] = None) -> dict:
    u = usage or {}
    return {"id": c.id, "name": c.name, "key_prefix": c.key_prefix, "webhook_url": c.webhook_url, "active": c.active,
            "created_at": c.created_at.isoformat() + "Z", "requests": u.get("requests", 0), "pages": u.get("pages", 0),
            "last_used_at": u.get("last_used_at")}


def _audit(db: Session, request: Request, action: str, target: Optional[str] = None) -> None:
    origin = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
    who = getattr(request.state, "admin", "owner")
    db.add(Audit(action=action, target=((target or "") + f" (by {who})")[:160], origin=(origin or "")[:60] or None))


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


class RouteChange(BaseModel):
    routes: dict = Field(default_factory=dict)  # kind of document -> webhook URL


@router.put("/clients/{cid}/routes")
def set_routes(cid: str, body: RouteChange, request: Request, db: Session = Depends(get_db)):
    """Send finished jobs of some kinds of document to their own address, e.g. {"routes": {"invoice":
    "https://accounts.example.com/hook"}}. They are signed with the client's webhook secret, so set a
    webhook first. Sending {} clears them."""
    c = _client(db, cid)
    clean = {}
    for kind, url in body.routes.items():
        if kind not in extract.TYPES or kind in ("auto", "custom"):
            raise ApiError(422, "bad_type", f"'{kind}' is not a kind of document")
        clean[kind] = _check_url(str(url))
        if not clean[kind]:
            raise ApiError(422, "bad_webhook_url", "every route needs a URL")
    if clean and not c.webhook_secret:
        raise ApiError(422, "no_webhook", "set the client's webhook first: routes are signed with its secret")
    for old in db.scalars(select(Route).where(Route.client_id == c.id)).all():
        db.delete(old)
    db.add_all([Route(client_id=c.id, kind=k, url=u) for k, u in clean.items()])
    _audit(db, request, "client.routes_set", f"{c.id} {c.name}: {', '.join(sorted(clean)) or 'none'}")
    return {"id": c.id, "routes": clean}


@router.get("/clients/{cid}/routes")
def get_routes(cid: str, db: Session = Depends(get_db)):
    c = _client(db, cid)
    return {"id": c.id, "routes": {r.kind: r.url for r in db.scalars(select(Route).where(Route.client_id == c.id))}}


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="viewer", pattern="^(admin|viewer)$")


@router.post("/users", status_code=201)
def create_user(body: NewUser, request: Request, db: Session = Depends(get_db)):
    """A console sign-in for another person. Their token is shown this once. Only the owner's token
    (OCR_ADMIN_TOKEN) may manage users. A viewer may look and try a document; an admin may also
    create and revoke clients."""
    token = "oa_" + secrets.token_urlsafe(32)
    u = AdminUser(id="usr_" + secrets.token_hex(8), name=body.name.strip(), token_hash=crypto.sha256_hex(token),
                  role=body.role)
    db.add(u)
    _audit(db, request, "user.created", f"{u.id} {u.name} ({u.role})")
    return {"id": u.id, "name": u.name, "role": u.role, "token": token}


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    return {"users": [{"id": u.id, "name": u.name, "role": u.role, "active": u.active,
                       "created_at": u.created_at.isoformat() + "Z"}
                      for u in db.scalars(select(AdminUser).order_by(AdminUser.created_at.desc()))]}


@router.delete("/users/{uid}", status_code=204)
def revoke_user(uid: str, request: Request, db: Session = Depends(get_db)):
    u = db.get(AdminUser, uid)
    if u is None:
        raise ApiError(404, "not_found", "user not found")
    u.active = False
    _audit(db, request, "user.revoked", f"{u.id} {u.name}")
    return Response(status_code=204)


@router.post("/searchable")
async def try_searchable(request: Request):
    """The console's searchable PDF of an uploaded scan. Not stored and not counted as usage."""
    from .. import searchable
    data, options, _ = await read_input(request)
    pdf, _ = await run_in_threadpool(searchable.build, data, options.get("pages"), get_settings().max_pages,
                                          "jawi" if options.get("engine") == "jawi" else None)
    return Response(pdf, media_type="application/pdf")


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
    options["preview"] = True  # the review view shows the page as it was read
    # the console is the administrator's own: whole documents, as a job would take them
    return await run_in_threadpool(service.run, data, options, get_settings().max_pages)
