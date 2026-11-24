"""The security and correctness review: internal webhook addresses, the upload ceiling, many-frame
TIFFs, running balances over lines without one, card statements, and telling statements apart."""
import io

import pytest

from app import extract, statement
from app.config import get_settings, resolves_to_internal
from tests import synth
from tests.conftest import *  # noqa: F401,F403
from tests.test_api import ADMIN, LINES


def t(debit=None, credit=None, balance=None):
    return {"date": "2026-01-02", "description": "x", "reference": None, "debit": debit, "credit": credit,
            "balance": balance}


# --- balances -------------------------------------------------------------------------------
def test_a_correct_day_of_several_lines_is_left_alone():
    txns = [t(credit=3000, balance=4000), t(debit=1200, balance=2800), t(debit=600), t(credit=300, balance=2500)]
    assert extract.repair_balances(txns, 1000) == 0
    assert txns[3]["credit"] == 300 and txns[3]["debit"] is None and extract.broken_balances(txns, 1000) == []


def test_a_wrong_column_after_a_line_without_a_balance_is_put_right():
    txns = [t(debit=100), t(debit=200, balance=1100)]
    assert extract.repair_balances(txns, 1000) == 1
    assert txns[1]["credit"] == 200 and txns[1]["debit"] is None and extract.broken_balances(txns, 1000) == []


def test_on_a_card_a_charge_raises_what_is_owed():
    txns = [t(debit=120, balance=120), t(debit=80, balance=200), t(credit=500, balance=-300)]
    assert extract.broken_balances(txns, 0, owed=True) == [] and extract.broken_balances(txns, 0, owed=False) != []
    assert statement._follows(txns, 0, owed=True) == 3
    checks = {c["name"]: c["passed"] for c in extract.statement_checks(
        {"transactions": txns, "opening_balance": 0, "closing_balance": -300}, owed=True)}
    assert checks["running_balances"] is True and checks["balance_reconciles"] is True


def test_a_year_is_not_taken_for_the_number_of_items():
    assert statement._printed_total([{"text": "Total Withdrawals as at 2026 5,000.00"}], statement._TOTAL_OUT) == (5000.0, None)
    assert statement._printed_total([{"text": "Total Withdrawals : 163 507,593.51"}], statement._TOTAL_OUT) == (507593.51, 163)


# --- which kind of document ---------------------------------------------------------------------
def test_statements_are_told_apart():
    bank = "Bank Statement\nOpening Balance 1,000.00\n02/01 DuitNow transfer 50.00\nClosing Balance 950.00"
    assert extract.guess_type(bank) == "bank_statement"
    # a real statement does not always say "bank statement", and its lines talk of transfers
    pages = ("HLB PRIMEBIZ CURRENT ACCOUNT\nStatement Period / Tempoh Penyataan : 01/01/26 - 31/01/26\n"
             "Balance from previous statement 500,603.53\n02-01-2026 CIB Instant Transfer at DIO 563.00\n"
             "05-01-2026 Instant Transfer at KLM beneficiary reference no 1,160.00\n06-01-2026 Bulk DuitNow 17,565.00")
    assert extract.guess_type(pages) == "bank_statement"
    ledger = ("General Ledger\nAccount listing 4000 Sales\nOpening Balance 0.00\n02/01 INV-1 100.00\n"
              "Closing Balance 100.00\nTransaction listing")
    assert extract.guess_type(ledger) != "bank_statement"


# --- webhooks to internal addresses ----------------------------------------------------------------
def test_internal_addresses_are_recognised():
    for url in ("http://127.0.0.1/", "http://10.1.2.3/x", "http://169.254.169.254/latest", "http://[::1]/",
                "http://0.0.0.0/", "http://192.168.1.20:8080/hook", "http:///nohost"):
        assert resolves_to_internal(url), url
    assert not resolves_to_internal("https://8.8.8.8/hook")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.1.2.3/", "http://169.254.169.254/", "http://[::1]/",
                                 "http://0.0.0.0/"])
def test_a_webhook_may_not_point_inside(client, url):
    r = client.post("/admin/api/clients", json={"name": "inside", "webhook_url": url}, headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"] == "bad_webhook_url"


def test_a_public_webhook_is_taken_and_inside_ones_when_the_owner_allows(client, monkeypatch):
    assert client.post("/admin/api/clients", json={"name": "outside", "webhook_url": "https://8.8.8.8/hook"},
                       headers=ADMIN).status_code == 201
    made = client.post("/admin/api/clients", json={"name": "routes", "webhook_url": "https://8.8.8.8/hook"},
                       headers=ADMIN).json()
    inside = client.put(f"/admin/api/clients/{made['id']}/routes", headers=ADMIN,
                        json={"routes": {"invoice": "http://127.0.0.1:9000/hook"}})
    assert inside.status_code == 422 and inside.json()["error"] == "bad_webhook_url"  # routes are held to the same rule
    monkeypatch.setattr(get_settings(), "webhook_allow_internal", True)
    assert client.post("/admin/api/clients", json={"name": "same mac", "webhook_url": "http://127.0.0.1:3000/hook"},
                       headers=ADMIN).status_code == 201


def test_a_webhook_is_not_sent_to_an_address_that_turned_internal(monkeypatch):
    from app import webhooks
    sent = []
    monkeypatch.setattr(webhooks.httpx, "post", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)
    webhooks._deliver("http://127.0.0.1:9/hook", b"secret", {"id": "job_1"})
    assert sent == []


# --- the ceiling on a request, whatever it says its length is -----------------------------------------
def test_a_body_that_hides_its_length_is_still_capped(client, api_key, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)  # the ceiling is then 64 KiB of slack

    def body():
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
        for _ in range(40):
            yield b"x" * 8192
        yield b"\r\n--b--\r\n"

    r = client.post("/v1/ocr", content=body(), headers={**api_key, "Content-Type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


# --- TIFF files of many pages ----------------------------------------------------------------------------
def tiff(pages):
    ims = [synth.page_image(p) for p in pages]
    buf = io.BytesIO()
    ims[0].save(buf, "TIFF", save_all=True, append_images=ims[1:])
    return buf.getvalue()


def test_a_tiff_of_too_many_pages_is_refused_before_it_is_decoded(client, api_key, need_engine):
    cap = get_settings().max_pages
    r = client.post("/v1/jobs", files={"file": ("big.tif", tiff([["page"]] * (cap + 1)), "image/tiff")}, headers=api_key)
    body = r.json()
    if r.status_code == 202:  # queued: the refusal comes when the job is read
        from tests.test_api import wait_for
        job = wait_for(client, api_key, body["id"])
        assert job["status"] == "failed" and job["error"] == "too_many_pages"
    else:
        assert r.status_code == 422 and body["error"] == "too_many_pages"


def test_a_tiff_of_two_pages_reads_both(client, api_key, need_engine):
    r = client.post("/v1/ocr", files={"file": ("two.tif", tiff([LINES, ["Second page of the scan"]]), "image/tiff")},
                    headers=api_key)
    assert r.status_code == 200, r.text
    pages = r.json()["pages"]
    assert len(pages) == 2 and "INV-2026-00417" in pages[0]["text"] and "Second page" in pages[1]["text"]
