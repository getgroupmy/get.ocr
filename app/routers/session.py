"""Console sign-in with a username and password. The answer is a session the console then sends
as its token; the owner's token and the users' own tokens keep working beside it."""
from __future__ import annotations

import base64
import secrets
import threading
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import auth
from .. import crypto
from ..db import AdminPassword, AdminTotp, AdminUser, Audit, get_db, utcnow
from ..errors import ApiError

router = APIRouter(prefix="/admin/api", tags=["admin sign-in"])
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60
_failures: dict = {}
_lock = threading.Lock()


class Login(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)
    remember: bool = False
    code: Optional[str] = Field(default=None, max_length=12)  # from the authenticator app, when two-step sign-in is on


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
    second = db.get(AdminTotp, user.id)
    if second is not None and second.confirmed:
        if not body.code:
            # the password was right: say that a code is wanted, without counting it as a failure
            raise ApiError(401, "code_required", "enter the six-digit code from your authenticator app")
        step = auth.totp_step_for(crypto.decrypt(second.secret, user.id.encode()), body.code, second.last_step)
        if step is None:
            _locked(key, add=True)
            db.add(Audit(action="user.login_failed", target=f"{name[:60]} (code)", origin=address[:60]))
            db.commit()
            raise ApiError(401, "unauthorized", "that code is not right, or was used already; wait for the next one")
        second.last_step = step
    with _lock:
        _failures.pop(key, None)
    row.last_login_at = utcnow()
    db.add(Audit(action="user.login", target=f"{user.id} {user.name}"[:160], origin=address[:60]))
    token, seconds = auth.make_session(user.id, body.remember)
    return {"token": token, "expires_in": seconds, "name": user.name, "role": user.role,
            "two_step": bool(second is not None and second.confirmed)}


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


def _me(request: Request, db: Session) -> tuple:
    """The user of a password sign-in and their password row; two-step sign-in is theirs to set."""
    user_id = auth.read_session(request.headers.get("x-admin-token", ""))
    user = db.get(AdminUser, user_id) if user_id else None
    row = db.query(AdminPassword).filter(AdminPassword.user_id == user_id).first() if user and user.active else None
    if row is None:
        raise ApiError(401, "unauthorized", "sign in with your username and password first")
    return user, row


def _qr(text: str) -> Optional[str]:
    """The text as a QR code picture (a data: address), or None where the barcode library cannot draw one."""
    try:
        import io
        import warnings

        import zxingcpp
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            picture = Image.fromarray(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, text, 320, 320))
        out = io.BytesIO()
        picture.save(out, format="PNG")
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:  # noqa: BLE001 - the key can be typed in by hand
        return None


@router.get("/2fa")
def two_step_state(request: Request, db: Session = Depends(get_db)):
    user, _ = _me(request, db)
    second = db.get(AdminTotp, user.id)
    return {"on": bool(second is not None and second.confirmed)}


@router.post("/2fa/setup")
def two_step_setup(request: Request, db: Session = Depends(get_db)):
    """Start turning two-step sign-in on: a new secret for the authenticator app, as a QR code and
    as a key to type. Nothing changes for the sign-in until /2fa/confirm proves the app has it."""
    user, row = _me(request, db)
    second = db.get(AdminTotp, user.id)
    if second is not None and second.confirmed:
        raise ApiError(409, "already_on", "two-step sign-in is on; turn it off first to set it up again")
    secret = secrets.token_bytes(20)
    if second is None:
        db.add(AdminTotp(user_id=user.id, secret=crypto.encrypt(secret, user.id.encode()), confirmed=False))
    else:
        second.secret, second.last_step = crypto.encrypt(secret, user.id.encode()), 0
    key = base64.b32encode(secret).decode().rstrip("=")
    address = f"otpauth://totp/{quote('GET OCR')}:{quote(row.username)}?secret={key}&issuer={quote('GET OCR')}&digits=6&period=30"
    return {"key": key, "address": address, "qr": _qr(address)}


class Code(BaseModel):
    code: str = Field(max_length=12)


@router.post("/2fa/confirm")
def two_step_confirm(body: Code, request: Request, db: Session = Depends(get_db)):
    user, row = _me(request, db)
    second = db.get(AdminTotp, user.id)
    if second is None:
        raise ApiError(409, "not_started", "start with /2fa/setup")
    step = auth.totp_step_for(crypto.decrypt(second.secret, user.id.encode()), body.code, second.last_step)
    if step is None:
        raise ApiError(422, "wrong_code", "that code is not right; check the time on your phone and enter the code now showing")
    second.confirmed, second.last_step = True, step
    db.add(Audit(action="user.two_step_on", target=row.username))
    return {"on": True}


class Off(BaseModel):
    password: str = Field(max_length=200)


@router.post("/2fa/off")
def two_step_off(body: Off, request: Request, db: Session = Depends(get_db)):
    """Turn two-step sign-in off, with the password. Someone who lost their phone asks the owner."""
    user, row = _me(request, db)
    if not auth.check_password(body.password, row.password_hash):
        raise ApiError(403, "forbidden", "the password is wrong")
    second = db.get(AdminTotp, user.id)
    if second is not None:
        db.delete(second)
        db.add(Audit(action="user.two_step_off", target=row.username))
    return {"on": False}
