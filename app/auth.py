from __future__ import annotations

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


def find_client(db: Session, key: str) -> ApiClient | None:
    if not key:
        return None
    client = db.scalar(select(ApiClient).where(ApiClient.key_hash == crypto.sha256_hex(key)))
    return client if client is not None and client.active else None


def require_client(x_api_key: str = Header(default=""), db: Session = Depends(get_db)) -> ApiClient:
    client = find_client(db, x_api_key)
    if client is None:
        raise ApiError(401, "unauthorized", "invalid or missing X-API-Key")
    return client


def require_admin(x_admin_token: str = Header(default="")) -> str:
    if x_admin_token and hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        return "admin"
    raise ApiError(401, "unauthorized", "invalid or missing X-Admin-Token")
