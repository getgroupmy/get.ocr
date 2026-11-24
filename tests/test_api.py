import base64
import time

from tests import synth
from tests.conftest import ADMIN, make_client

LINES = ["Invoice No: INV-2026-00417", "Jumlah perlu dibayar RM 1,250.80"]


def wait_for(client, key, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/v1/jobs/{job_id}", headers=key).json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_health_needs_no_key_and_checks_one_when_sent(client, api_key):
    assert client.get("/health").json()["engine"] == "pp-ocr"
    assert client.get("/health", headers=api_key).json()["key"] == "ok"
    assert client.get("/health", headers={"X-API-Key": "ok_wrong"}).status_code == 401


def test_api_needs_a_key(client):
    files = {"file": ("a.png", synth.image_bytes(LINES), "image/png")}
    for headers in ({}, {"X-API-Key": "ok_wrong"}):
        r = client.post("/v1/ocr", files=files, headers=headers)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert client.get("/v1/jobs", headers={}).status_code == 401


def test_admin_needs_the_token(client):
    assert client.get("/admin/api/clients").status_code == 401
    assert client.get("/admin/api/clients", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.get("/admin").status_code == 200  # the page itself holds no data


def test_reads_an_image(client, api_key, need_engine):
    r = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "image" and body["page_count"] == 1
    page = body["pages"][0]
    assert page["source"] == "ocr" and page["confidence"] > 0.8
    assert "INV-2026-00417" in body["text"] and "1,250.80" in body["text"]
    assert body["text"].index("Invoice") < body["text"].index("Jumlah")  # reading order
    line = page["lines"][0]
    assert len(line["box"]) == 4 and 0 < line["confidence"] <= 1
    assert 0 <= line["box"][0][0] <= page["width"]


def test_json_base64_body_and_text_only(client, api_key, need_engine):
    body = {"file": base64.b64encode(synth.image_bytes(LINES, "JPEG")).decode(), "detail": "text"}
    r = client.post("/v1/ocr", json=body, headers=api_key)
    assert r.status_code == 200, r.text
    assert "INV-2026-00417" in r.json()["text"] and "lines" not in r.json()["pages"][0]


def test_blank_image_gives_empty_text(client, api_key, need_engine):
    r = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes([]), "image/png")}, headers=api_key)
    assert r.status_code == 200 and r.json()["text"] == "" and r.json()["pages"][0]["lines"] == []


def test_scanned_pdf_pages_option(client, api_key, need_engine):
    pdf = synth.scanned_pdf([["First page alpha"], ["Second page bravo"], ["Third page charlie"]])
    r = client.post("/v1/ocr", files={"file": ("s.pdf", pdf, "application/pdf")}, data={"pages": "2-3"}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf" and body["page_count"] == 3
    assert [p["page"] for p in body["pages"]] == [2, 3]
    assert "bravo" in body["pages"][0]["text"] and "alpha" not in body["text"]


def test_sync_refuses_long_pdfs(client, api_key, need_engine):
    pdf = synth.scanned_pdf([["a"], ["b"], ["c"]])
    r = client.post("/v1/ocr", files={"file": ("s.pdf", pdf, "application/pdf")}, headers=api_key)
    assert r.status_code == 422 and r.json()["error"] == "too_many_pages"


def test_pdf_text_layer_is_used_unless_forced(client, api_key, need_engine):
    pdf = synth.text_pdf("Perjanjian sewa kenderaan 2026 between two parties")
    r = client.post("/v1/ocr", files={"file": ("t.pdf", pdf, "application/pdf")}, headers=api_key)
    page = r.json()["pages"][0]
    assert page["source"] == "text_layer" and "Perjanjian sewa kenderaan 2026" in page["text"]
    r = client.post("/v1/ocr", files={"file": ("t.pdf", pdf, "application/pdf")}, data={"force_ocr": "true"},
                    headers=api_key)
    page = r.json()["pages"][0]
    assert page["source"] == "ocr" and "Perjanjian" in page["text"] and page["lines"]


def test_bad_input(client, api_key):
    def post(**kw):
        return client.post("/v1/ocr", headers=api_key, **kw)
    r = post(files={"file": ("a.txt", b"just some text, not a picture", "text/plain")})
    assert r.status_code == 415 and r.json()["error"] == "unsupported_format"
    assert post(files={"file": ("a.pdf", b"%PDF-1.4 broken", "application/pdf")}).json()["error"] == "bad_file"
    assert post(files={"file": ("a.png", b"", "image/png")}).status_code == 422
    assert post(data={"pages": "1"}).status_code in (415, 422)
    assert post(json={"file": "not base64 !!"}).json()["error"] == "bad_request"
    png = synth.image_bytes(["x"])
    assert post(files={"file": ("a.png", png, "image/png")}, data={"detail": "words"}).json()["error"] == "bad_detail"
    assert post(files={"file": ("a.png", png, "image/png")}, data={"pages": "x"}).json()["error"] == "bad_pages"
    assert post(files={"file": ("a.png", png, "image/png")}, data={"pages": "4"}).json()["error"] == "bad_pages"


def test_upload_limit(client, api_key, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    r = client.post("/v1/ocr", files={"file": ("a.png", b"x" * 70000, "image/png")}, headers=api_key)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    r = client.post("/v1/ocr", files={"file": ("a.png", b"x" * 100, "image/png")}, headers=api_key)
    assert r.status_code == 413


def test_job_flow_with_signed_webhook(client, need_engine, monkeypatch):
    from app import webhooks
    calls = []

    class Ok:
        status_code = 200

    def fake_post(url, content, headers, timeout):
        calls.append((url, content, headers))
        return Ok()

    monkeypatch.setattr(webhooks.httpx, "post", fake_post)
    made = make_client(client, "hooked", "https://app.example.com/hooks/ocr")
    key = {"X-API-Key": made["api_key"]}
    pdf = synth.scanned_pdf([["First page alpha"], ["Second page bravo"]])
    r = client.post("/v1/jobs", files={"file": ("s.pdf", pdf, "application/pdf")},
                    data={"external_ref": "doc-1042"}, headers=key)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued" and "result" not in r.json()
    job = wait_for(client, key, r.json()["id"])
    assert job["status"] == "completed" and job["pages"] == 2 and job["external_ref"] == "doc-1042"
    assert "alpha" in job["result"]["text"] and "bravo" in job["result"]["text"]

    from app.config import get_settings
    assert not list(get_settings().inputs_dir.iterdir())  # the upload is gone once read

    for _ in range(50):
        if calls:
            break
        time.sleep(0.1)
    url, content, headers = calls[0]
    assert url == "https://app.example.com/hooks/ocr"
    assert headers["X-OCR-Signature"] == webhooks.sign(made["webhook_secret"].encode(), headers["X-OCR-Timestamp"], content)
    assert b'"event":"ocr.completed"' in content and b"doc-1042" in content and b"alpha" not in content

    listed = client.get("/v1/jobs", params={"external_ref": "doc-1042"}, headers=key).json()["jobs"]
    assert [j["id"] for j in listed] == [job["id"]]
    assert client.delete(f"/v1/jobs/{job['id']}", headers=key).status_code == 204
    assert client.get(f"/v1/jobs/{job['id']}", headers=key).status_code == 404


def test_failed_job_reports_a_code(client, api_key, need_engine):
    pdf = synth.scanned_pdf([["a"], ["b"], ["c"], ["d"]])  # OCR_MAX_PAGES is 3 in tests
    r = client.post("/v1/jobs", files={"file": ("s.pdf", pdf, "application/pdf")}, headers=api_key)
    job = wait_for(client, api_key, r.json()["id"])
    assert job["status"] == "failed" and job["error"] == "too_many_pages" and job["result"] is None


def test_clients_only_see_their_own_jobs(client, api_key, need_engine):
    other = {"X-API-Key": make_client(client, "other-app")["api_key"]}
    r = client.post("/v1/jobs", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")}, headers=api_key)
    jid = r.json()["id"]
    wait_for(client, api_key, jid)
    assert client.get(f"/v1/jobs/{jid}", headers=other).status_code == 404
    assert client.delete(f"/v1/jobs/{jid}", headers=other).status_code == 404
    assert jid not in [j["id"] for j in client.get("/v1/jobs", headers=other).json()["jobs"]]


def test_stored_result_is_encrypted_and_purged_after_retention(client, api_key, need_engine, monkeypatch):
    import sqlite3
    from datetime import timedelta

    from app import service
    from app.config import get_settings
    from app.db import Job, session_scope, utcnow
    r = client.post("/v1/jobs", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")}, headers=api_key)
    jid = r.json()["id"]
    wait_for(client, api_key, jid)
    raw = sqlite3.connect(get_settings().data_dir / "ocr.db").execute(
        "select result from jobs where id = ?", (jid,)).fetchone()[0]
    assert b"INV-2026" not in raw
    with session_scope() as db:
        db.get(Job, jid).completed_at = utcnow() - timedelta(days=30)
    service.housekeeping()
    job = client.get(f"/v1/jobs/{jid}", headers=api_key).json()
    assert job["status"] == "completed" and job["result"] is None and job["result_available"] is False


def test_admin_clients_usage_and_revoke(client, need_engine):
    made = make_client(client, "billing")
    assert made["api_key"].startswith("ok_") and made["webhook_secret"] is None
    key = {"X-API-Key": made["api_key"]}
    client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")}, headers=key)
    row = next(c for c in client.get("/admin/api/clients", headers=ADMIN).json()["clients"] if c["id"] == made["id"])
    assert row["requests"] == 1 and row["pages"] == 1 and row["last_used_at"] and "api_key" not in row
    assert made["api_key"] not in client.get("/admin/api/clients", headers=ADMIN).text

    r = client.put(f"/admin/api/clients/{made['id']}/webhook", json={"webhook_url": "ftp://x"}, headers=ADMIN)
    assert r.status_code == 422
    r = client.put(f"/admin/api/clients/{made['id']}/webhook", json={"webhook_url": "https://x.example/h"}, headers=ADMIN)
    assert r.json()["webhook_secret"]

    stats = client.get("/admin/api/stats", headers=ADMIN).json()
    assert stats["requests"] >= 1 and stats["pages"] >= 1 and stats["limits"]["sync_max_pages"] == 2
    jobs = client.get("/admin/api/jobs", headers=ADMIN).json()["jobs"]
    assert all("result" not in j for j in jobs)

    assert client.delete(f"/admin/api/clients/{made['id']}", headers=ADMIN).status_code == 204
    assert client.get("/health", headers=key).status_code == 401
    assert client.get("/admin/api/clients/missing", headers=ADMIN).status_code in (404, 405)


def test_console_try_it(client, need_engine):
    r = client.post("/admin/api/try", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")}, headers=ADMIN)
    assert r.status_code == 200 and "INV-2026-00417" in r.json()["text"]
