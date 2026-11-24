"""Files waiting in the job queue, encrypted on local disk. A file is deleted
as soon as its job has been read."""
from __future__ import annotations

from pathlib import Path

from . import crypto
from .config import get_settings


def _path(job_id: str) -> Path:
    if not job_id.replace("_", "").isalnum():
        raise ValueError("bad job id")
    return get_settings().inputs_dir / f"{job_id}.bin"


def save(job_id: str, data: bytes) -> None:
    p = _path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(crypto.encrypt(data, aad=job_id.encode()))


def load(job_id: str) -> bytes:
    return crypto.decrypt(_path(job_id).read_bytes(), aad=job_id.encode())


def delete(job_id: str) -> None:
    _path(job_id).unlink(missing_ok=True)
