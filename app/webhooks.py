"""Signed webhook delivery so the calling app learns a job is done without polling.

Signature: X-OCR-Signature = "sha256=" + HMAC_SHA256(secret, f"{timestamp}.{body}")
The payload says which job finished, never the text: fetch that with the API key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

import httpx

from .config import get_settings, resolves_to_internal
from .db import ApiClient, Job

log = logging.getLogger("ocr.webhooks")
BACKOFF = (0, 5, 30)


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def payload_for(job: Job) -> dict:
    return {
        "event": "ocr.completed" if job.status == "completed" else "ocr.failed",
        "id": job.id,
        "external_ref": job.external_ref,
        "status": job.status,
        "pages": job.pages,
        "error": job.error,
        "completed_at": job.completed_at.isoformat() + "Z" if job.completed_at else None,
    }


def _deliver(url: str, secret: bytes, payload: dict) -> None:
    if resolves_to_internal(url):
        log.warning("webhook for %s not sent: %s is a local or internal address", payload["id"], url)
        return
    body = json.dumps(payload, separators=(",", ":")).encode()
    outcome = "failed"
    for delay in BACKOFF:
        time.sleep(delay)
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-OCR-Timestamp": ts,
                   "X-OCR-Signature": sign(secret, ts, body)}
        try:
            r = httpx.post(url, content=body, headers=headers, timeout=get_settings().webhook_timeout_seconds)
            if r.status_code < 300:
                outcome = "delivered"
                break
            outcome = f"http_{r.status_code}"
        except httpx.HTTPError as exc:
            outcome = type(exc).__name__
    log.info("webhook for %s: %s", payload["id"], outcome)


def notify(job: Job, client: ApiClient) -> None:
    if not client.webhook_url or not client.webhook_secret:
        return
    threading.Thread(target=_deliver, daemon=True,
                     args=(client.webhook_url, client.webhook_secret, payload_for(job))).start()
