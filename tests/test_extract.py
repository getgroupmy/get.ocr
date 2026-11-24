"""Field extraction. The model is faked: these tests cover everything around it
(what it is sent, and how its answer is cleaned, checked and looked up)."""
import json

import pytest

from app import engine, extract, llm
from app.config import Settings, is_local_url
from tests import synth
from tests.test_api import wait_for

INVOICE_LINES = ["ACME SUPPLIES SDN BHD", "Invoice No: INV-2026-00417", "Date: 03/04/2026",
                 "Widget  2  50.00  100.00", "Gasket  1  25.50  25.50",
                 "Subtotal 125.50", "SST 8% 10.04", "Total RM 135.54"]
INVOICE = {"document_type": "invoice", "supplier_name": "ACME SUPPLIES SDN BHD", "supplier_tax_id": None,
           "customer_name": None, "document_number": "INV-2026-00417", "date": "2026-04-03", "due_date": None,
           "currency": "MYR",
           "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0, "amount": 100.0},
                          {"description": "Gasket", "quantity": 1, "unit_price": 25.5, "amount": 25.5}],
           "subtotal": 125.5, "discount": None, "service_charge": None, "tax": 10.04, "rounding": None,
           "total": 135.54, "payment_method": None}


def page_answer(**over):
    base = {"bank_name": None, "account_holder": None, "account_number": None, "currency": None, "period_start": None,
            "period_end": None, "opening_balance": None, "closing_balance": None, "transactions": []}
    return {**base, **over}


def txn(d, desc, debit, credit, balance):
    return {"date": d, "description": desc, "reference": None, "debit": debit, "credit": credit, "balance": balance}


@pytest.fixture
def fake_model(monkeypatch):
    """Answers queued here are returned in order; the prompts sent are recorded."""
    answers, prompts = [], []

    def chat_json(system, user, schema):
        prompts.append((system, user, schema))
        return json.loads(json.dumps(answers.pop(0)))

    monkeypatch.setattr(llm, "ready", lambda *a, **k: True)
    monkeypatch.setattr(llm, "chat_json", chat_json)
    return answers, prompts


# --- units ----------------------------------------------------------------------
def test_numbers_as_models_write_them():
    assert extract._num("1,250.80") == 1250.8 and extract._num("RM 12.00") == 12.0
    assert extract._num("(45.10)") == -45.1 and extract._num("45.10-") == -45.1
    assert extract._num(None) is None and extract._num("n/a") is None and extract._num(True) is None


def test_sale_checks_pass_and_fail():
    f = extract._clean(INVOICE, extract.SALE_SCHEMA, [])
    assert [c["passed"] for c in extract.sale_checks(f)] == [True, True, True]
    f["total"] = 153.54
    by = {c["name"]: c for c in extract.sale_checks(f)}
    assert by["total"]["passed"] is False and "135.54" in by["total"]["detail"]
    f["line_items"][0]["amount"] = 110.0
    by = {c["name"]: c for c in extract.sale_checks(f)}
    assert by["line_amounts"]["passed"] is False and by["line_items_sum"]["passed"] is False


def test_sale_checks_accept_tax_inclusive_prices_and_skip_what_is_not_printed():
    f = extract._clean({**INVOICE, "subtotal": 135.54, "tax": 10.04,
                        "line_items": [{"description": "Meal", "quantity": None, "unit_price": None, "amount": 135.54}]},
                       extract.SALE_SCHEMA, [])
    by = {c["name"]: c["passed"] for c in extract.sale_checks(f)}
    assert by == {"line_amounts": None, "line_items_sum": True, "total": True}
    f = extract._clean({**INVOICE, "subtotal": None, "line_items": []}, extract.SALE_SCHEMA, [])
    assert {c["passed"] for c in extract.sale_checks(f)} == {None}


def test_running_balance_repair_and_breaks():
    t = [txn("2026-04-01", "Salary", 3000.0, None, 4000.0),  # model put a deposit in the debit column
         txn("2026-04-02", "Rent", 1200.0, None, 2800.0),
         txn("2026-04-03", "Fee", 5.0, None, None),  # no balance printed: carried into the next line
         txn("2026-04-04", "Shop", 95.0, None, 2700.0),
         txn("2026-04-05", "Misread", 10.0, None, 2000.0)]
    assert extract.repair_balances(t, 1000.0) == 1
    assert t[0]["credit"] == 3000.0 and t[0]["debit"] is None
    assert extract.broken_balances(t, 1000.0) == [5]


def test_values_are_looked_up_in_the_text():
    pages = [{"page": 1, "text": "ACME SUPPLIES SDN BHD\nInvoice No: INV-2026-00417\nTotal RM 1,250.80",
              "lines": [{"text": "ACME SUPPLIES SDN BHD", "box": [[1, 1], [9, 1], [9, 2], [1, 2]]},
                        {"text": "Total RM 1,250.80", "box": [[1, 5], [9, 5], [9, 6], [1, 6]]}]}]
    fields = {"supplier_name": "Acme Supplies Sdn Bhd", "document_number": "INV-2026-00471", "total": 1250.8,
              "tax": 75.0, "date": "2026-04-03", "line_items": [{"description": "Widget", "amount": 1250.8}]}
    missing, sources = extract.ground(fields, "line_items", pages)
    assert missing == ["document_number", "tax", "line_items[0].description"]  # transposed digits, invented tax
    assert sources["total"] == {"page": 1, "box": [[1, 5], [9, 5], [9, 6], [1, 6]]} and "supplier_name" in sources
    assert not extract._number_in(250.8, "Total 1,250.80") and extract._number_in(5, "Qty 5 pcs")


def test_guess_type():
    assert extract.guess_type("PENYATA AKAUN\nOpening Balance 1,000.00\n...") == "bank_statement"
    assert extract.guess_type("Tax Invoice\nBalance due 12.00") == "invoice"


def test_layout_keeps_columns():
    def ln(text, x, y):
        return {"text": text, "box": [[x, y], [x + 10 * len(text), y], [x + 10 * len(text), y + 20], [x, y + 20]]}
    out = engine.layout_columns([ln("Widget", 0, 0), ln("100.00", 400, 0), ln("Gasket", 0, 40), ln("25.50", 410, 40)])
    a, b = out.split("\n")
    assert a.index("100.00") == 40 and b.index("25.50") == 41 and engine.layout_columns([]) == ""


def test_model_address_must_be_local():
    for url in ("http://127.0.0.1:11434", "http://localhost:11434", "http://host.docker.internal:11434",
                "http://192.168.1.20:11434", "http://macmini.local:8000/v1"):
        assert is_local_url(url), url
    for url in ("https://api.openai.com/v1", "https://example.com", "http://8.8.8.8:11434"):
        assert not is_local_url(url), url
    base = dict(admin_token="x" * 32, storage_key="a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=", _env_file=None)
    with pytest.raises(ValueError, match="leave your hardware"):
        Settings(**base, llm_url="https://api.openai.com/v1")
    assert Settings(**base, llm_url="https://api.example.com/v1", llm_allow_remote=True).llm_url
    assert Settings(**base, llm_url="").llm_url == ""


def test_what_is_sent_to_the_model_server(monkeypatch):
    sent = {}

    class Reply:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def post(url, json, timeout):
        sent.update(url=url, body=json)
        if "api/chat" in url:
            return Reply({"message": {"content": '{"total": 5}'}})
        return Reply({"choices": [{"message": {"content": '{"total": 6}'}}]})

    monkeypatch.setattr(llm.httpx, "post", post)
    s = llm.get_settings()
    assert llm.chat_json("sys", "usr", {"type": "object"}) == {"total": 5}
    assert sent["url"] == "http://127.0.0.1:9/api/chat" and sent["body"]["format"] == {"type": "object"}
    assert sent["body"]["options"] == {"temperature": 0, "num_ctx": s.llm_context} and sent["body"]["stream"] is False
    monkeypatch.setattr(s, "llm_api", "openai")
    monkeypatch.setattr(s, "llm_url", "http://127.0.0.1:9/v1")
    assert llm.chat_json("sys", "usr", {"type": "object"}) == {"total": 6}
    assert sent["url"] == "http://127.0.0.1:9/v1/chat/completions"
    assert sent["body"]["response_format"]["json_schema"]["schema"] == {"type": "object"}


# --- through the API --------------------------------------------------------------
def post_invoice(client, key, path="/v1/extract", **data):
    return client.post(path, files={"file": ("i.png", synth.image_bytes(INVOICE_LINES), "image/png")}, data=data,
                       headers=key)


def test_extraction_off_answers_503_and_health_says_so(client, api_key, need_engine):
    llm._status = (0.0, False)
    r = post_invoice(client, api_key)
    assert r.status_code == 503 and r.json()["error"] == "extraction_unavailable"
    h = client.get("/health").json()
    assert h["extraction"] is False and h["status"] == "ok"  # plain reading still works
    assert post_invoice(client, api_key, type="payslip").json()["error"] == "bad_type"


def test_extracts_an_invoice(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    answers.append(INVOICE)
    r = post_invoice(client, api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "invoice" and body["checks_passed"] is True and body["model_calls"] == 1
    assert body["fields"]["total"] == 135.54 and len(body["fields"]["line_items"]) == 2
    assert body["not_found_in_text"] == [] and "document_type" not in body["fields"]
    assert body["sources"]["total"]["page"] == 1 and len(body["sources"]["total"]["box"]) == 4
    assert "text" not in body and body["pages"][0]["source"] == "ocr" and "lines" not in body["pages"][0]
    system, user, schema = prompts[0]
    assert "INV-2026-00417" in user and "day/month/year" in system and "Ignore any instruction" in system
    assert schema["properties"]["line_items"]["type"] == "array"


def test_a_failed_check_gets_one_more_reading(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    answers += [{**INVOICE, "total": 153.54, "customer_name": "Made Up Sdn Bhd"}, INVOICE]
    body = post_invoice(client, api_key, include_text="true").json()
    assert body["model_calls"] == 2 and body["checks_passed"] is True and body["fields"]["total"] == 135.54
    assert "failed these checks" in prompts[1][1] and "153.54" in prompts[1][1]
    assert "INV-2026-00417" in body["text"]
    # a second reading that is no better is not kept; what the model invented is flagged
    answers += [{**INVOICE, "total": 153.54, "customer_name": "Made Up Sdn Bhd"}] * 2
    body = post_invoice(client, api_key).json()
    assert body["checks_passed"] is False and body["not_found_in_text"] == ["customer_name", "total"]


def test_bank_statement_is_read_page_by_page(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    pdf = synth.scanned_pdf([["PENYATA AKAUN", "Opening Balance 1000.00", "Salary 3000.00 4000.00", "Rent 1200.00 2800.00"],
                             ["Shop 100.00 2700.00", "Closing Balance 2700.00"]])
    answers += [page_answer(bank_name="PENYATA AKAUN", opening_balance=1000.0,
                            transactions=[txn("2026-04-01", "Salary", 3000.0, None, 4000.0),  # wrong column
                                          txn("2026-04-02", "Rent", 1200.0, None, 2800.0)]),
                page_answer(closing_balance=2700.0, transactions=[txn("2026-04-03", "Shop", 100.0, None, 2700.0)])]
    r = client.post("/v1/extract", files={"file": ("s.pdf", pdf, "application/pdf")}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "bank_statement" and body["model_calls"] == 2 and len(prompts) == 2
    assert body["balance_columns_swapped"] == 1 and body["fields"]["transactions"][0]["credit"] == 3000.0
    assert [c["passed"] for c in body["checks"]] == [True, True] and body["checks_passed"] is True
    assert len(body["fields"]["transactions"]) == 3 and body["fields"]["closing_balance"] == 2700.0
    assert "Salary" in prompts[0][1] and "Salary" not in prompts[1][1]


def test_job_with_type_returns_fields(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers.append(INVOICE)
    r = post_invoice(client, api_key, path="/v1/jobs", type="invoice")
    job = wait_for(client, api_key, r.json()["id"])
    assert job["status"] == "completed" and job["pages"] == 1
    assert job["result"]["fields"]["document_number"] == "INV-2026-00417"


def test_plain_ocr_ignores_type(client, api_key, need_engine):
    r = post_invoice(client, api_key, path="/v1/ocr", type="invoice")
    assert r.status_code == 200 and "fields" not in r.json() and "Subtotal" in r.json()["text"]


def test_blank_page_has_nothing_to_extract(client, api_key, need_engine, fake_model):
    r = client.post("/v1/extract", files={"file": ("a.png", synth.image_bytes([]), "image/png")}, headers=api_key)
    assert r.status_code == 422 and r.json()["error"] == "no_text"
