from __future__ import annotations
from typing import Optional

import threading
import time
import hashlib
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


SESSION_HOURS, REMEMBER_DAYS = 12, 30


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), dk)
    except Exception:  # noqa: BLE001
        return False


def _session_key() -> bytes:
    # From the storage key and the owner's token: rotating the token signs everyone out.
    s = get_settings()
    return hmac.new(s.storage_key.encode(), b"console-session|" + s.admin_token.encode(), hashlib.sha256).digest()


def make_session(user_id: str, remember: bool = False) -> tuple[str, int]:
    """What the console sends in X-Admin-Token after a sign-in with a password: who, until when,
    and a signature only this server can make. Nothing is stored for it."""
    seconds = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
    body = f"os_{user_id}.{int(time.time()) + seconds}"
    return body + "." + hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest(), seconds


def read_session(value: str) -> Optional[str]:
    """The user id of a session that is genuine and not over, or None."""
    if not value.startswith("os_") or value.count(".") != 2:
        return None
    body, sig = value.rsplit(".", 1)
    good = hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()
    until = body.rsplit(".", 1)[1]
    if not hmac.compare_digest(sig, good) or not until.isdigit() or int(until) < time.time():
        return None
    return body[3:].rsplit(".", 1)[0]


_wrong: dict = {}  # address -> times of its recent wrong console tokens
_wrong_lock = threading.Lock()
WRONG_WINDOW = 300.0


def _caller(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "") or "?"


def _too_many_wrong(address: str, add: bool = False) -> bool:
    """The console is on the internet: an address that keeps sending wrong tokens is made to wait."""
    now = time.monotonic()
    with _wrong_lock:
        recent = [t for t in _wrong.get(address, []) if now - t < WRONG_WINDOW]
        if add:
            recent.append(now)
        if recent:
            _wrong[address] = recent
        else:
            _wrong.pop(address, None)
        if len(_wrong) > 5000:  # many addresses at once: forget the oldest
            for key in sorted(_wrong, key=lambda k: _wrong[k][-1])[:1000]:
                _wrong.pop(key, None)
        return len(recent) >= get_settings().admin_tries


def require_admin(request: Request, x_admin_token: str = Header(default=""), db: Session = Depends(get_db)) -> str:
    """The owner's token from the configuration, or a console user's own token. A viewer may look
    and try a document, but not change anything. Returns who it is, for the activity log."""
    address = _caller(request)
    if _too_many_wrong(address):
        raise ApiError(429, "too_many_attempts", "too many wrong console tokens from this address; wait five minutes")
    if x_admin_token and hmac.compare_digest(x_admin_token.encode(), get_settings().admin_token.encode()):
        request.state.admin, request.state.role = "owner", "owner"
        return "owner"
    signed_in = read_session(x_admin_token) if x_admin_token else None
    user = db.get(AdminUser, signed_in) if signed_in else db.scalar(
        select(AdminUser).where(AdminUser.token_hash == crypto.sha256_hex(x_admin_token))) if x_admin_token else None
    if user is None or not user.active:
        if x_admin_token:  # a wrong token counts; opening the page without one does not
            _too_many_wrong(address, add=True)
        raise ApiError(401, "unauthorized", "invalid or missing X-Admin-Token")
    changing = request.method not in ("GET", "HEAD") and not request.url.path.endswith(("/try", "/searchable"))
    if changing and user.role != "admin":
        raise ApiError(403, "forbidden", "this console user may look, not change")
    if "/users" in request.url.path:
        raise ApiError(403, "forbidden", "only the owner's token manages console users")
    request.state.admin, request.state.role = user.name, user.role
    return user.name
