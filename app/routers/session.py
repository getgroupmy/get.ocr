"""Console sign-in with a username and password. The answer is a session the console then sends
as its token; the owner's token and the users' own tokens keep working beside it."""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import auth
from ..db import AdminPassword, AdminUser, Audit, get_db, utcnow
from ..errors import ApiError

router = APIRouter(prefix="/admin/api", tags=["admin sign-in"])
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60
_failures: dict = {}
_lock = threading.Lock()


class Login(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)
    remember: bool = False


def _locked(key: str, add: bool = False) -> bool:
    now = time.monotonic()
    with _lock:
        recent = [t for t in _failures.get(key, []) if now - t < LOCK_SECONDS]
        if add:
            recent.append(now)
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)
        if len(_failures) > 5000:
            for old in sorted(_failures, key=lambda k: _failures[k][-1])[:1000]:
                _failures.pop(old, None)
        return len(recent) >= LOCK_AFTER


@router.post("/login")
def login(body: Login, request: Request, db: Session = Depends(get_db)):
    """Sign in with a username and password. Five wrong passwords for one username from one
    address lock that pair for fifteen minutes."""
    address = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    name = body.username.strip().lower()
    key = f"{address}|{name}"
    if _locked(key):
        raise ApiError(429, "too_many_attempts", "too many wrong passwords; try again in 15 minutes")
    row = db.get(AdminPassword, name)
    user = db.get(AdminUser, row.user_id) if row else None
    # the password is checked even for a name that does not exist, so the answer takes as long
    good = auth.check_password(body.password, row.password_hash if row else "scrypt$00$00")
    if not good or user is None or not user.active:
        _locked(key, add=True)
        db.add(Audit(action="user.login_failed", target=name[:60], origin=address[:60]))
        db.commit()
        raise ApiError(401, "unauthorized", "wrong username or password")
    with _lock:
        _failures.pop(key, None)
    row.last_login_at = utcnow()
    db.add(Audit(action="user.login", target=f"{user.id} {user.name}"[:160], origin=address[:60]))
    token, seconds = auth.make_session(user.id, body.remember)
    return {"token": token, "expires_in": seconds, "name": user.name, "role": user.role}


class NewPassword(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=10, max_length=200)


@router.post("/password")
def change_password(body: NewPassword, request: Request, db: Session = Depends(get_db)):
    """A signed-in user changes their own password."""
    user_id = auth.read_session(request.headers.get("x-admin-token", ""))
    row = db.query(AdminPassword).filter(AdminPassword.user_id == user_id).first() if user_id else None
    if row is None:
        raise ApiError(401, "unauthorized", "sign in with your username and password first")
    if not auth.check_password(body.current, row.password_hash):
        raise ApiError(403, "forbidden", "the current password is wrong")
    row.password_hash = auth.hash_password(body.new)
    db.add(Audit(action="user.password_changed", target=row.username))
    return {"ok": True}
