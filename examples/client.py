"""A small client for get.ocr, to copy into your backend. Needs `httpx`.

    from client import GetOcr
    ocr = GetOcr("https://ocr.example.com", "ok_...")
    text = ocr.read("letter.pdf")["text"]
    bill = ocr.extract("tnb.pdf", type="utility_bill")
    if bill["checks_passed"] is not True or bill["not_found_in_text"]:
        ...  # have a person look
    job = ocr.queue("statement.pdf", type="bank_statement", external_ref="acc-42")
    result = ocr.wait(job["id"])

Call it from your server only: the key must never be in a mobile or web app."""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import time
from typing import Optional

import httpx


class OcrError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


class GetOcr:
    def __init__(self, base_url: str, api_key: str, timeout: float = 600):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers={"X-API-Key": api_key}, timeout=timeout)

    def _send(self, path: str, file: str, options: dict) -> dict:
        p = pathlib.Path(file)
        data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else str(v)
                for k, v in options.items() if v is not None}
        r = self._http.post(path, files={"file": (p.name, p.read_bytes())}, data=data)
        return self._answer(r)

    @staticmethod
    def _answer(r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise OcrError(r.status_code, body.get("error", "error"), body.get("message", r.text))
        return body

    def read(self, file: str, **options) -> dict:
        """The text of a file. Options: pages, detail, force_ocr, codes, engine, task."""
        return self._send("/v1/ocr", file, options)

    def extract(self, file: str, type: str = "auto", **options) -> dict:  # noqa: A002
        """The fields of a document. Options: fields (for type='custom'), split, pages, engine, include_text."""
        return self._send("/v1/extract", file, {"type": type, **options})

    def queue(self, file: str, type: Optional[str] = None, external_ref: Optional[str] = None, **options) -> dict:  # noqa: A002
        """Queue a long document. The result comes by webhook, or with wait()."""
        return self._send("/v1/jobs", file, {"type": type, "external_ref": external_ref, **options})

    def job(self, job_id: str) -> dict:
        return self._answer(self._http.get(f"/v1/jobs/{job_id}"))

    def wait(self, job_id: str, every: float = 3, limit: float = 3600) -> dict:
        end = time.time() + limit
        while time.time() < end:
            job = self.job(job_id)
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(every)
        raise TimeoutError(job_id)

    def types(self) -> list:
        return self._answer(self._http.get("/v1/types"))["types"]


def webhook_is_valid(secret: str, timestamp: str, raw_body: bytes, signature: str, max_age: int = 300) -> bool:
    """Check a webhook from get.ocr: headers X-OCR-Timestamp and X-OCR-Signature, and the body as received."""
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature) and abs(time.time() - int(timestamp)) < max_age
