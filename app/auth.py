from __future__ import annotations
from typing import Optional

import threading
import time
import hmac
import secrets

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto
from .config import get_settings
from .db import ApiClient, get_db
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


def require_admin(x_admin_token: str = Header(default="")) -> str:
    if x_admin_token and hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        return "admin"
    raise ApiError(401, "unauthorized", "invalid or missing X-Admin-Token")
