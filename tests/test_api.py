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


# --- duplicates, rate limit, audit, CSV -----------------------------------------------------
def test_the_same_file_twice_is_reported(client, api_key, need_engine):
    png = synth.image_bytes(["A document sent twice", "Reference 7781"])
    first = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, headers=api_key).json()
    again = client.post("/v1/ocr", files={"file": ("b.png", png, "image/png")}, headers=api_key).json()
    assert first["duplicate_file_first_seen"] is None and again["duplicate_file_first_seen"].endswith("Z")
    other = client.post("/v1/ocr", files={"file": ("c.png", synth.image_bytes(["Another one"]), "image/png")},
                        headers=api_key).json()
    assert other["duplicate_file_first_seen"] is None


def test_requests_are_limited_per_key(client, api_key, monkeypatch):
    from app import auth
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "rate_per_minute", 3)
    auth._calls.clear()
    codes = [client.get("/v1/jobs", headers=api_key).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    auth._calls.clear()
    assert client.get("/v1/jobs", headers=api_key).status_code == 200


def test_console_actions_are_logged(client):
    made = client.post("/admin/api/clients", json={"name": "audited app"}, headers=ADMIN).json()
    client.delete("/admin/api/clients/" + made["id"], headers=ADMIN)
    events = client.get("/admin/api/audit", headers=ADMIN).json()["events"]
    assert [e["action"] for e in events[:2]] == ["client.revoked", "client.created"]
    assert made["id"] in events[0]["target"] and made["api_key"] not in str(events)
    assert client.get("/admin/api/audit").status_code == 401


def test_health_reports_free_disk(client):
    assert client.get("/health").json()["disk_free_gb"] > 0


def test_a_page_on_its_side_is_turned_upright(client, api_key, need_engine):
    import io
    from PIL import Image
    upright = synth.page_image(LINES)
    for degrees in (90, 270):  # PIL turns anticlockwise; turning the same amount clockwise undoes it
        buf = io.BytesIO()
        upright.rotate(degrees, expand=True).save(buf, "PNG")
        page = client.post("/v1/ocr", files={"file": ("s.png", buf.getvalue(), "image/png")}, headers=api_key).json()["pages"][0]
        assert page["rotated"] == degrees and page["width"] == upright.width and page["height"] == upright.height
        assert "INV-2026-00417" in page["text"] and page["text"].index("Invoice") < page["text"].index("Jumlah")
    plain = client.post("/v1/ocr", files={"file": ("u.png", synth.image_bytes(LINES), "image/png")}, headers=api_key).json()
    assert "rotated" not in plain["pages"][0]


def test_the_console_can_be_kept_off_the_public_address(client, api_key, monkeypatch):
    from app.config import get_settings
    through = {"cf-ray": "a6072fa9291955bd-KUL", "cf-connecting-ip": "203.0.113.9"}
    # as get.ekyc and get.route do: the console answers at the public address, behind its token
    assert client.get("/admin", headers=through).status_code == 200
    assert client.get("/admin/api/clients", headers=through).status_code == 401
    assert client.get("/admin/api/clients", headers={**ADMIN, **through}).status_code == 200
    # OCR_ADMIN_PUBLIC=false: through Cloudflare there is nothing there, even with the right token
    monkeypatch.setattr(get_settings(), "admin_public", False)
    assert client.get("/admin", headers=through).status_code == 404
    assert client.get("/admin/api/clients", headers={**ADMIN, **through}).status_code == 404
    assert client.get("/admin").status_code == 200  # on the machine itself: as before
    assert client.get("/admin/api/clients", headers=ADMIN).status_code == 200
    # the API for other apps is what the tunnel is for
    assert client.get("/health", headers=through).status_code == 200
    assert client.post("/v1/ocr", headers=through).status_code == 401
    assert client.get("/v1/types", headers={**api_key, **through}).status_code == 200


def test_an_address_that_keeps_sending_wrong_console_tokens_must_wait(client, monkeypatch):
    from app import auth
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "admin_tries", 3)
    guesser = {"cf-connecting-ip": "198.51.100.77"}
    for n in range(3):
        assert client.get("/admin/api/clients", headers={**guesser, "X-Admin-Token": f"guess-{n}"}).status_code == 401
    # now even the right token waits, from that address only
    late = client.get("/admin/api/clients", headers={**guesser, **ADMIN})
    assert late.status_code == 429 and late.json()["error"] == "too_many_attempts"
    assert client.get("/admin/api/clients", headers={"cf-connecting-ip": "198.51.100.78", **ADMIN}).status_code == 200
    # opening the page without a token is not a guess
    for _ in range(5):
        assert client.get("/admin/api/clients", headers={"cf-connecting-ip": "198.51.100.79"}).status_code == 401
    assert client.get("/admin/api/clients", headers={"cf-connecting-ip": "198.51.100.79", **ADMIN}).status_code == 200
    auth._wrong.clear()


def test_sign_in_with_a_username_and_password(client, monkeypatch):
    from app.routers import session
    made = client.post("/admin/api/users", json={"name": "Aina", "role": "admin", "username": "Aina.K", "password": "kata-laluan-panjang"},
                       headers=ADMIN)
    assert made.status_code == 201 and made.json()["username"] == "aina.k"
    assert client.post("/admin/api/users", json={"name": "B", "username": "aina.k", "password": "kata-laluan-lain"}, headers=ADMIN).status_code == 409
    assert client.post("/admin/api/users", json={"name": "B", "username": "bbb"}, headers=ADMIN).status_code == 422  # a username needs a password
    assert client.post("/admin/api/users", json={"name": "B", "username": "bbb", "password": "pendek"}, headers=ADMIN).status_code == 422
    # wrong password, unknown name: the same answer
    assert client.post("/admin/api/login", json={"username": "aina.k", "password": "salah-salah-salah"}).status_code == 401
    assert client.post("/admin/api/login", json={"username": "tiada", "password": "salah-salah-salah"}).json()["message"] == "wrong username or password"
    r = client.post("/admin/api/login", json={"username": "AINA.K ", "password": "kata-laluan-panjang"})
    assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["expires_in"] == 12 * 3600
    signed = {"X-Admin-Token": r.json()["token"]}
    # the session works where a token does, with the user's own rights
    assert client.get("/admin/api/clients", headers=signed).status_code == 200
    assert client.post("/admin/api/clients", json={"name": "by-aina"}, headers=signed).status_code == 201
    assert client.get("/admin/api/users", headers=signed).status_code == 403  # only the owner's token manages users
    listed = client.get("/admin/api/users", headers=ADMIN).json()["users"]
    mine = next(u for u in listed if u["username"] == "aina.k")
    assert mine["last_login_at"] and "password" not in str(listed).lower().replace("password_set", "")
    # a session cannot be made up or altered
    forged = r.json()["token"].rsplit(".", 1)[0] + "." + "0" * 64
    assert client.get("/admin/api/clients", headers={"X-Admin-Token": forged}).status_code == 401
    # she changes her own password; the old one stops working
    assert client.post("/admin/api/password", json={"current": "bukan-yang-betul", "new": "kata-laluan-baharu"}, headers=signed).status_code == 403
    assert client.post("/admin/api/password", json={"current": "kata-laluan-panjang", "new": "kata-laluan-baharu"}, headers=signed).status_code == 200
    assert client.post("/admin/api/login", json={"username": "aina.k", "password": "kata-laluan-panjang"}).status_code == 401
    assert client.post("/admin/api/login", json={"username": "aina.k", "password": "kata-laluan-baharu", "remember": True}).json()["expires_in"] == 30 * 86400
    # the owner sets a new password; revoking the user ends her session at once
    assert client.put(f"/admin/api/users/{mine['id']}/password", json={"username": "aina", "password": "ditetapkan-pemilik"}, headers=ADMIN).status_code == 200
    assert client.post("/admin/api/login", json={"username": "aina", "password": "ditetapkan-pemilik"}).status_code == 200
    assert client.delete(f"/admin/api/users/{mine['id']}", headers=ADMIN).status_code == 204
    assert client.get("/admin/api/clients", headers=signed).status_code == 401
    assert client.post("/admin/api/login", json={"username": "aina", "password": "ditetapkan-pemilik"}).status_code == 401
    # five wrong passwords lock that name from that address
    session._failures.clear()
    for _ in range(5):
        assert client.post("/admin/api/login", json={"username": "kunci", "password": "salah-salah-salah"}).status_code == 401
    assert client.post("/admin/api/login", json={"username": "kunci", "password": "salah-salah-salah"}).status_code == 429
    session._failures.clear()


def test_a_console_user_can_be_made_from_the_command_line(client, monkeypatch):
    from app import adduser
    monkeypatch.setattr(adduser.getpass, "getpass", lambda prompt="": "dari-baris-arahan")
    assert adduser.main(["pemilik"]) == 0
    r = client.post("/admin/api/login", json={"username": "pemilik", "password": "dari-baris-arahan"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    monkeypatch.setattr(adduser.getpass, "getpass", lambda prompt="": "kata-laluan-kedua")
    assert adduser.main(["pemilik"]) == 0  # the same name again: a new password
    assert client.post("/admin/api/login", json={"username": "pemilik", "password": "kata-laluan-kedua"}).status_code == 200
    monkeypatch.setattr(adduser.getpass, "getpass", lambda prompt="": "pendek")
    assert adduser.main(["lain"]) == 1 and adduser.main(["x"]) == 2


def test_passwords_are_hashed_without_the_function_the_macs_python_lacks(monkeypatch):
    import hashlib

    from app import auth
    reference = hashlib.scrypt(b"kata-laluan-panjang", salt=b"0123456789abcdef", n=2 ** 14, r=8, p=1, dklen=32)
    assert auth._scrypt("kata-laluan-panjang", b"0123456789abcdef") == reference  # the same hash either way
    monkeypatch.delattr(hashlib, "scrypt")  # Python built against LibreSSL has none
    stored = auth.hash_password("kata-laluan-panjang")
    assert stored.startswith("scrypt$") and auth.check_password("kata-laluan-panjang", stored)
    assert not auth.check_password("kata-laluan-lain", stored) and not auth.check_password("x", "rosak")
