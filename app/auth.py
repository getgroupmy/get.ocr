from __future__ import annotations
from typing import Optional

import threading
import time
import hmac
import secrets

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto
from .config import get_settings
from .db import AdminUser, ApiClient, get_db
from .errors import ApiError


def new_api_key() -> str:
    return "ok_" + secrets.token_urlsafe(32)


def find_client(db: Session, key: str) -> Optional[ApiClient]:
    if not key:
        return None
    client = db.scalar(select(ApiClient).where(ApiClient.key_hash == crypto.sha256_hex(key)))
    return client if client is not None and client.active else None


_calls: dict = {}  # client id -> times of its recent requests
_calls_lock = threading.Lock()


def _within_rate(client_id: str) -> bool:
    limit = get_settings().rate_per_minute
    if limit <= 0:
        return True
    now = time.monotonic()
    with _calls_lock:
        recent = [t for t in _calls.get(client_id, ()) if now - t < 60]
        ok = len(recent) < limit
        if ok:
            recent.append(now)
        _calls[client_id] = recent
    return ok


def require_client(x_api_key: str = Header(default=""), db: Session = Depends(get_db)) -> ApiClient:
    client = find_client(db, x_api_key)
    if client is None:
        raise ApiError(401, "unauthorized", "invalid or missing X-API-Key")
    if not _within_rate(client.id):
        raise ApiError(429, "rate_limited", f"more than {get_settings().rate_per_minute} requests in a minute; "
                                            "slow down and try again")
    return client


def require_admin(request: Request, x_admin_token: str = Header(default=""), db: Session = Depends(get_db)) -> str:
    """The owner's token from the configuration, or a console user's own token. A viewer may look
    and try a document, but not change anything. Returns who it is, for the activity log."""
    if x_admin_token and hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        request.state.admin, request.state.role = "owner", "owner"
        return "owner"
    user = db.scalar(select(AdminUser).where(AdminUser.token_hash == crypto.sha256_hex(x_admin_token))) \
        if x_admin_token else None
    if user is None or not user.active:
        raise ApiError(401, "unauthorized", "invalid or missing X-Admin-Token")
    changing = request.method not in ("GET", "HEAD") and not request.url.path.endswith(("/try", "/searchable"))
    if changing and user.role != "admin":
        raise ApiError(403, "forbidden", "this console user may look, not change")
    if "/users" in request.url.path:
        raise ApiError(403, "forbidden", "only the owner's token manages console users")
    request.state.admin, request.state.role = user.name, user.role
    return user.name
