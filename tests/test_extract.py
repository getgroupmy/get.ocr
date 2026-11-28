"""Field extraction. The model is faked: these tests cover everything around it
(what it is sent, and how its answer is cleaned, checked and looked up)."""
import json

import pytest

from app import engine, extract, llm
from app.config import Settings, is_local_url
from tests import synth
from tests.conftest import ADMIN
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
    assert sources["total"]["box"] == [[1, 5], [9, 5], [9, 6], [1, 6]] and sources["total"]["page"] == 1
    assert "supplier_name" in sources and "confidence" in sources["total"]
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
    assert post_invoice(client, api_key, type="payslip").json()["error"] == "extraction_unavailable"
    h = client.get("/health").json()
    assert h["extraction"] is False and h["status"] == "ok"  # plain reading still works
    assert post_invoice(client, api_key, type="passport").json()["error"] == "bad_type"


def test_a_form_read_by_position_needs_no_model_server(client, api_key, need_engine):
    llm._status = (0.0, False)  # the model server is not running, as in every test that does not fake it
    card_lines = ["KAD PENGENALAN", "850412-10-5759", "LIM AH KOW", "NO 5 JALAN 3", "43000 KAJANG", "SELANGOR", "LELAKI"]
    for kind in ("mykad", "auto"):
        r = client.post("/v1/extract", files={"file": ("c.png", synth.image_bytes(card_lines), "image/png")}, data={"type": kind},
                        headers=api_key)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["document_type"], body["read_by"], body["model_calls"]) == ("mykad", "labels", 0)
        assert (body["fields"]["id_number"], body["fields"]["name"]) == ("850412-10-5759", "LIM AH KOW")
        assert body["fields"]["address"] == "NO 5 JALAN 3, 43000 KAJANG, SELANGOR" and body["fields"]["date_of_birth"] == "1985-04-12"


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


# --- the statement table, read without the model -----------------------------------
def piece(text, x, y, right=False, char=6.0):
    """A piece of text as the reader gives it; right=True ends it at x, as amounts are printed."""
    w = len(text) * char
    x0 = x - w if right else x
    return {"text": text, "confidence": 1.0, "box": [[x0, y], [x0 + w, y], [x0 + w, y + 10], [x0, y + 10]]}


def table_page(rows, number=1):
    lines = [piece(*cell) if isinstance(cell, tuple) else cell for y, row in enumerate(rows) for cell in
             [(c[0], c[1], 20 + 14 * y, *c[2:]) for c in row]]
    return {"page": number, "width": 600, "height": 800, "lines": lines, "text": engine.layout_text(lines)}


def test_statement_dates_and_amounts():
    from app import statement
    assert statement._date("02-01-2026", "dmy", None) == "2026-01-02"
    assert statement._date("02/01/26", "mdy", None) == "2026-02-01"
    assert statement._date("2 Jan 2026", "dmy", None) == "2026-01-02" and statement._date("15Mac", "dmy", 2026) == "2026-03-15"
    assert statement._date("03/04", "dmy", 2025) == "2025-04-03" and statement._date("03/04", "dmy", None) is None
    assert statement._date("668.55", "dmy", 2026) is None and statement._date("12.05", "dmy", 2026) is None
    assert statement._money("1,250.80") == (1250.8, "") and statement._money("1250.80CR") == (1250.8, "+")
    assert statement._money("(40.00)") == (40.0, "-") and statement._money("40.00-") == (40.0, "-")
    assert statement._money("2026") is None and statement._money("02-01-2026") is None


def test_statement_with_text_layer_is_read_exactly(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    pdf, truth = synth.bank_statement(pages=2)
    answers.append({"bank_name": "Hong Leong Bank Berhad", "account_holder": "DELTA KARGO SDN. BHD.",
                    "account_number": "02800999123", "currency": "MYR", "period_start": "2026-01-01",
                    "period_end": "2026-01-31"})
    r = client.post("/v1/extract", files={"file": ("s.pdf", pdf, "application/pdf")}, data={"type": "bank_statement"},
                    headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_by"] == "table" and body["model_calls"] == 1 and len(prompts) == 1
    assert body["fields"]["transactions"] == truth["transactions"] and len(truth["transactions"]) == 28
    assert body["fields"]["opening_balance"] == truth["opening_balance"]
    assert body["fields"]["closing_balance"] == truth["closing_balance"]
    assert body["checks_passed"] is True and body["not_found_in_text"] == []
    assert [c["name"] for c in body["checks"]] == ["running_balances", "balance_reconciles", "printed_totals"]
    assert body["fields"]["account_holder"] == "DELTA KARGO SDN. BHD." and body["sources"]["account_holder"]["box"]
    # the model is shown the top of the first page only, and none of the transactions on later pages
    assert "DELTA KARGO" in prompts[0][1] and "2 of 2" not in prompts[0][1] and "Closing Balance" not in prompts[0][1]


def test_scanned_statement_is_read_exactly(need_engine):
    from app import service, statement
    pdf, truth = synth.bank_statement(pages=1, per_page=12, seed=3)
    ocr = service.recognise(pdf, {"detail": "lines", "force_ocr": True}, 5)
    assert ocr["pages"][0]["source"] == "ocr"
    got = statement.parse(ocr["pages"])
    assert [(t["date"], t["debit"], t["credit"], t["balance"]) for t in got["transactions"]] == \
           [(t["date"], t["debit"], t["credit"], t["balance"]) for t in truth["transactions"]]
    assert got["closing_balance"] == truth["closing_balance"]
    assert got["printed"]["total_debits"] == round(sum(t["debit"] or 0 for t in truth["transactions"]), 2)
    wrong = {**got, "transactions": got["transactions"][:-1]}
    assert [c["passed"] for c in extract.statement_checks(wrong, got["printed"])] == [True, False, False]


def test_statement_with_one_amount_column_and_no_headings():
    from app import statement
    page = table_page([
        [("PENYATA AKAUN", 30)],
        [("BALANCE B/F", 100), ("1,000.00", 560, True)],
        [("01/04", 30), ("SALARY APRIL", 100), ("3,000.00CR", 440, True), ("4,000.00", 560, True)],
        [("02/04", 30), ("RENT", 100), ("1,200.00", 440, True), ("2,800.00", 560, True)],
        [("TRANSFER FEE", 100), ("0.50", 440, True), ("2,799.50", 560, True)],  # same day, date printed once
        [("05/04", 30), ("OVERDRAWN TEST", 100), ("3,000.00", 440, True), ("200.50DR", 560, True)],
        [("Statement date 30/04/2026", 30)],
    ])
    got = statement.parse([page])
    assert got["opening_balance"] == 1000.0
    assert [(t["date"], t["debit"], t["credit"], t["balance"]) for t in got["transactions"]] == [
        ("2026-04-01", None, 3000.0, 4000.0), ("2026-04-02", 1200.0, None, 2800.0),
        ("2026-04-02", 0.5, None, 2799.5), ("2026-04-05", 3000.0, None, -200.5)]
    assert extract.statement_checks({**got})[0]["passed"] is True


def test_statement_columns_are_proved_by_the_balance_when_headings_do_not_say():
    from app import statement
    rows = [[("Date", 30), ("Details", 100), ("Out", 380, True), ("In", 460, True), ("Balance", 560, True)],
            [("Opening balance", 100), ("500.00", 560, True)],
            [("03 Jan 2026", 30), ("CARD PAYMENT 12.50 USD", 100), ("60.00", 380, True), ("440.00", 560, True)],
            [("second line of description", 100)],
            [("04 Jan 2026", 30), ("REFUND", 100), ("10.00", 460, True), ("450.00", 560, True)],
            [("Total", 100), ("60.00", 380, True), ("10.00", 460, True)]]
    got = statement.parse([table_page(rows)])
    assert [(t["debit"], t["credit"], t["balance"]) for t in got["transactions"]] == [(60.0, None, 440.0), (None, 10.0, 450.0)]
    assert got["transactions"][0]["description"] == "CARD PAYMENT 12.50 USD second line of description"
    assert got["transactions"][1]["date"] == "2026-01-04"


def test_text_without_a_table_gives_no_transactions():
    from app import statement
    page = table_page([[("Dear customer, your balance on 01/04/2026 is fine.", 30)], [("Thank you.", 30)]])
    assert statement.parse([page])["transactions"] == []


# --- order lists and photos -----------------------------------------------------------
ORDER_LINES = ["** Order List **", "### BBV01/16541", "Table No: 01", "Guest : 2", "Date : 24/11/2026 9:04 PM",
               "Waiter : Aina", "** Dine In - New **", "1 *51.THAI FRAGRANT STEAMED RICE", "51.STEAMED RICE",
               "2 *THAI ICED TEA", "NO SUGAR"]
ORDER = {"title": "Order List", "order_number": "BBV01/16541", "table": "01", "guests": 2, "date": "2026-11-24",
         "time": "9:04 PM", "staff": "Aina", "service_type": "Dine In",
         "items": [{"quantity": 1, "name": "51.THAI FRAGRANT STEAMED RICE", "short_name": "51.STEAMED RICE", "notes": None},
                   {"quantity": 2, "name": "THAI ICED TEA", "short_name": None, "notes": "NO SUGAR"}]}


def test_order_list_is_told_from_a_receipt():
    assert extract.guess_type("\n".join(ORDER_LINES)) == "order"
    assert extract.guess_type("Table No: 01\nDine In\nNasi Lemak 12.50\nTotal 12.50") == "invoice"  # a bill has prices
    assert extract.guess_type("\n".join(INVOICE_LINES)) == "invoice"


def test_extracts_an_order_list(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    answers.append(ORDER)
    r = client.post("/v1/extract", files={"file": ("o.png", synth.image_bytes(ORDER_LINES), "image/png")}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "order" and body["model_calls"] == 1 and "order list" in prompts[0][1]
    assert body["fields"]["items"][1] == {"quantity": 2.0, "name": "THAI ICED TEA", "short_name": None, "notes": "NO SUGAR",
                                           "struck": False, "ticked": None}
    assert body["fields"]["date"] == "2026-11-24" and body["fields"]["table"] == "01"
    assert body["checks"] == [{"name": "item_quantities", "passed": True, "detail": "all 2 item(s) have a quantity"}]
    assert body["checks_passed"] is True and body["not_found_in_text"] == []
    assert body["sources"]["order_number"]["box"]


def test_order_items_without_a_quantity_fail_the_check():
    fields = {"items": [{"quantity": 1.0, "name": "A"}, {"quantity": None, "name": "B"}]}
    assert extract.order_checks(fields)[0]["passed"] is False and "[2]" in extract.order_checks(fields)[0]["detail"]
    assert extract.order_checks({"items": []})[0]["passed"] is None


def test_a_shadow_is_evened_out_and_a_clean_page_is_left_alone():
    import numpy as np
    page = np.asarray(synth.page_image(["Order 1", "Table 2", "1 *THAI ICED TEA", "2 *PLAIN WATER"]))[:, :, ::-1]
    assert engine.even_lighting(np.ascontiguousarray(page)) is None
    shaded = page.astype(float)
    shaded[page.shape[0] // 2:] *= 0.45  # a shadow over the lower half
    shaded = np.ascontiguousarray(shaded.astype("uint8"))
    evened = engine.even_lighting(shaded)
    assert evened is not None and evened.shape == shaded.shape
    paper = lambda img: float(np.percentile(img[page.shape[0] // 2 + 60:, :, 0], 90))  # noqa: E731
    assert paper(shaded) < 130 and paper(evened) > 200


# --- payslips, bills, other sales documents, and a caller's own fields -------------------
PAYSLIP_LINES = ["DELTA KARGO SDN BHD", "PAYSLIP January 2026", "Name: NUR AINA BINTI OMAR", "Basic Salary 3,000.00",
                 "Overtime 250.00", "EPF Employee 357.00", "SOCSO Employee 16.25", "PCB 40.00",
                 "EPF Employer 423.00", "Gross Pay 3,250.00", "Total Deductions 413.25", "Net Pay 2,836.75"]
PAYSLIP = {"employer_name": "DELTA KARGO SDN BHD", "employee_name": "NUR AINA BINTI OMAR", "employee_id": None,
           "id_number": None, "position": None, "pay_period": "January 2026", "pay_date": None, "currency": "MYR",
           "lines": [{"kind": "earning", "description": "Basic Salary", "amount": 3000.0},
                     {"kind": "earning", "description": "Overtime", "amount": 250.0},
                     {"kind": "deduction", "description": "EPF Employee", "amount": 357.0},
                     {"kind": "deduction", "description": "SOCSO Employee", "amount": 16.25},
                     {"kind": "deduction", "description": "PCB", "amount": 40.0},
                     {"kind": "employer", "description": "EPF Employer", "amount": 423.0}],
           "gross_pay": 3250.0, "total_deductions": 413.25, "net_pay": 2836.75}
BILL_LINES = ["TENAGA ELEKTRIK BERHAD", "Bil Elektrik", "No. Akaun: 220012345678", "Tarikh Bil: 05/01/2026",
              "Bayar Sebelum: 04/02/2026", "Penggunaan 412 kWh", "Tunggakan 50.00", "Caj Semasa 148.30",
              "Jumlah Perlu Dibayar 198.30"]
BILL = {"provider_name": "TENAGA ELEKTRIK BERHAD", "customer_name": None, "account_number": "220012345678",
        "bill_number": None, "bill_date": "2026-01-05", "due_date": "2026-02-04", "period_start": None,
        "period_end": None, "currency": "MYR", "usage": 412, "usage_unit": "kWh", "previous_balance": 50.0,
        "payments_received": None, "current_charges": 148.3, "tax": None, "rounding": None, "total_due": 198.3}


def extract_image(client, key, lines, **data):
    return client.post("/v1/extract", files={"file": ("d.png", synth.image_bytes(lines), "image/png")}, data=data,
                       headers=key)


def test_kinds_are_told_apart():
    assert extract.guess_type("\n".join(PAYSLIP_LINES)) == "payslip"
    assert extract.guess_type("\n".join(BILL_LINES)) == "utility_bill"
    assert extract.guess_type("\n".join(INVOICE_LINES)) == "invoice"


def test_payslip_checks():
    fields = extract._clean(PAYSLIP, extract.PAYSLIP_SCHEMA, [])
    assert [c["passed"] for c in extract.payslip_checks(fields)] == [True, True, True]
    fields["lines"][0]["amount"] = 2000.0  # a misread salary shows in the sum of earnings
    assert [c["passed"] for c in extract.payslip_checks(fields)] == [False, True, True]
    bare = {**fields, "gross_pay": None, "total_deductions": None, "lines": []}
    assert [c["passed"] for c in extract.payslip_checks(bare)] == [None, None, None]


def test_extracts_a_payslip(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    answers.append(PAYSLIP)
    r = extract_image(client, api_key, PAYSLIP_LINES)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "payslip" and body["checks_passed"] is True and body["model_calls"] == 1
    assert body["fields"]["net_pay"] == 2836.75 and len(body["fields"]["lines"]) == 6
    assert body["not_found_in_text"] == [] and "payslip" in prompts[0][1]


def test_extracts_a_utility_bill_and_rereads_when_the_total_is_off(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    answers += [{**BILL, "current_charges": 143.3}, BILL]
    r = extract_image(client, api_key, BILL_LINES)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "utility_bill" and body["model_calls"] == 2 and "total_due" in prompts[1][1]
    assert body["checks"][0]["passed"] is True and body["fields"]["current_charges"] == 148.3
    assert body["fields"]["due_date"] == "2026-02-04" and body["fields"]["usage"] == 412.0
    assert body["not_found_in_text"] == []


def test_other_sales_documents_use_the_invoice_fields(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers.append({**INVOICE, "document_type": "other"})
    body = post_invoice(client, api_key, type="quotation").json()
    assert body["document_type"] == "quotation" and body["fields"]["total"] == 135.54


def test_a_callers_own_fields(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    lines = ["MOTOR INSURANCE COVER NOTE", "Policy No: MX-778812", "Sum Insured RM 45,000.00", "Expiry 31/12/2026"]
    answers.append({"policy_number": "MX-778812", "sum_insured": "45,000.00", "expiry": "2026-12-31", "insurer": "ZURICH"})
    wanted = {"policy_number": "string", "sum_insured": {"type": "number", "description": "amount covered"},
              "expiry": "date", "insurer": "string"}
    r = extract_image(client, api_key, lines, type="custom", fields=json.dumps(wanted))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "custom" and body["checks"] == [] and body["checks_passed"] is None
    assert body["fields"] == {"policy_number": "MX-778812", "sum_insured": 45000.0, "expiry": "2026-12-31",
                              "insurer": "ZURICH"}
    assert body["not_found_in_text"] == ["insurer"]  # the model made it up; the date is rewritten and not looked up
    assert "- sum_insured (number): amount covered" in prompts[0][1] and "- expiry (date)" in prompts[0][1]
    assert set(prompts[0][2]["properties"]) == set(wanted)


def test_custom_fields_are_validated(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    assert extract.clean_fields("policy_number, expiry_date") == {
        "policy_number": {"type": "string", "description": None}, "expiry_date": {"type": "date", "description": None}}
    for bad in ("", "Bad Name", '{"a": "money"}', '{"a": 1}', "{not json", json.dumps({f"f{i}": "string" for i in range(41)})):
        r = extract_image(client, api_key, INVOICE_LINES, type="custom", fields=bad)
        assert r.status_code == 422 and r.json()["error"] == "bad_fields", bad
    assert prompts == []


# --- QR codes ---------------------------------------------------------------------------
def test_qr_codes_are_read_when_asked(client, api_key, need_engine):
    png = synth.qr_page("TRACK-99812", ["Consignment", "Parcel 1 of 1"])
    plain = client.post("/v1/ocr", files={"file": ("q.png", png, "image/png")}, headers=api_key).json()
    assert "codes" not in plain["pages"][0]
    r = client.post("/v1/ocr", files={"file": ("q.png", png, "image/png")}, data={"codes": "true"}, headers=api_key)
    code = r.json()["pages"][0]["codes"][0]
    assert code["kind"] == "qr" and code["text"] == "TRACK-99812" and len(code["box"]) == 4


def test_extraction_reports_the_einvoice_link(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers.append(INVOICE)
    link = "https://myinvois.hasil.gov.my/8A1B2C3D4E5F6G7H8J9K0M1N2P/share/ABCDEF123456"
    png = synth.qr_page(link, INVOICE_LINES)
    body = client.post("/v1/extract", files={"file": ("i.png", png, "image/png")}, headers=api_key).json()
    assert body["codes"][0]["text"] == link and body["codes"][0]["page"] == 1
    assert body["einvoice"] == {"uuid": "8A1B2C3D4E5F6G7H8J9K0M1N2P", "url": link, "page": 1}
    assert body["fields"]["total"] == 135.54


# --- several documents in one file, and how good the picture is ---------------------------
def test_pages_are_split_into_documents():
    inv = "\n".join(INVOICE_LINES)
    assert extract.split_pages([inv, inv]) == [[0], [1]]  # the first one printed its total
    assert extract.split_pages(["Invoice INV-1\nPage 1 of 2\nWidget 2 50.00", "Page 2 of 2\nTotal 100.00",
                                "Page 1 of 1\nReceipt\nTotal 5.00"]) == [[0, 1], [2]]
    assert extract.split_pages([inv, "", "\n".join(ORDER_LINES)]) == [[0, 1], [2]]  # a blank back page, then another kind
    assert extract.split_pages(["Dear Sir, we refer to the above.", "Yours faithfully"]) == [[0, 1]]


def test_a_bundle_is_extracted_document_by_document(client, api_key, need_engine, fake_model):
    answers, prompts = fake_model
    answers += [INVOICE, ORDER]
    pdf = synth.scanned_pdf([INVOICE_LINES, ORDER_LINES])
    r = client.post("/v1/extract", files={"file": ("b.pdf", pdf, "application/pdf")}, data={"split": "true"},
                    headers=api_key)
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert [(d["pages"], d["document_type"]) for d in docs] == [([1], "invoice"), ([2], "order")]
    assert docs[0]["fields"]["total"] == 135.54 and docs[1]["fields"]["table"] == "01" and len(prompts) == 2
    assert r.json()["page_count"] == 2 and "fields" not in r.json()


def test_picture_quality_is_reported(client, api_key, need_engine):
    import numpy as np
    page = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(INVOICE_LINES), "image/png")},
                       headers=api_key).json()["pages"][0]
    assert page["quality"]["sharpness"] > 100 and "blurry" not in page["quality"]["issues"]
    sharp = np.ascontiguousarray(np.asarray(synth.page_image(INVOICE_LINES))[:, :, ::-1])
    import cv2
    assert "blurry" in engine.quality(cv2.GaussianBlur(sharp, (0, 0), 6))["issues"]
    assert "dark" in engine.quality((sharp * 0.15).astype("uint8"))["issues"]


# --- the second reader: a vision model -------------------------------------------------------
models: list = []  # which model each call of the stand-in vision reader was for


@pytest.fixture
def fake_vision(monkeypatch):
    """Stands in for the vision model: records what it is sent and answers with prepared text."""
    seen = []
    models.clear()

    def read_image(prompt, image, model=None):
        seen.append((prompt, image))
        models.append(model)
        return "```markdown\nKepada Encik Ali,\nBayaran RM 250.00 diterima.\n```"

    monkeypatch.setattr(llm, "vision_ready", lambda *a, **k: True)
    monkeypatch.setattr(llm, "read_image", read_image)
    return seen


def test_vision_reader_is_off_until_its_model_is_installed(client, api_key, need_engine):
    llm._vision_status = {}
    r = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(["hello"]), "image/png")},
                    data={"engine": "glm"}, headers=api_key)
    assert r.status_code == 503 and r.json()["error"] == "reader_unavailable" and "ollama pull glm-ocr" in r.json()["message"]
    assert client.get("/health").json()["vision_reader"] is False
    bad = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(["hello"]), "image/png")},
                      data={"engine": "tesseract"}, headers=api_key)
    assert bad.status_code == 422 and bad.json()["error"] == "bad_engine"


def test_vision_reader_reads_the_picture_of_each_page(client, api_key, need_engine, fake_vision):
    r = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(["Kepada Encik Ali,", "Bayaran RM 250.00 diterima."]), "image/png")},
                    data={"engine": "glm"}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    page = body["pages"][0]
    assert body["engine"] == "glm-ocr" and page["source"] == "glm" and page["confidence"] is None and page["lines"] == []
    assert page["text"] == "Kepada Encik Ali,\nBayaran RM 250.00 diterima."  # the code fence is taken off
    prompt, image = fake_vision[0]
    assert prompt == "Text Recognition:" and image[:3] == b"\xff\xd8\xff"  # a JPEG


def test_vision_reader_reads_a_text_pdf_as_a_picture_and_takes_a_task(client, api_key, need_engine, fake_vision):
    pdf = synth.text_pdf("Perjanjian sewa kenderaan 2026")
    r = client.post("/v1/ocr", files={"file": ("a.pdf", pdf, "application/pdf")},
                    data={"engine": "glm", "task": "table"}, headers=api_key)
    assert r.status_code == 200 and r.json()["pages"][0]["source"] == "glm" and fake_vision[0][0] == "Table Recognition:"
    bad = client.post("/v1/ocr", files={"file": ("a.pdf", pdf, "application/pdf")}, data={"engine": "glm", "task": "poem"},
                      headers=api_key)
    assert bad.status_code == 422 and bad.json()["error"] == "bad_task"


def test_fields_are_extracted_from_what_the_vision_reader_read(client, api_key, need_engine, fake_vision, fake_model):
    answers, prompts = fake_model
    answers.append({"payee": "Encik Ali", "amount": 250, "made_up": "x"})
    r = client.post("/v1/extract", files={"file": ("a.png", synth.image_bytes(["scribble"]), "image/png")},
                    data={"engine": "glm", "type": "custom", "fields": '{"payee": "string", "amount": "number", "made_up": "string"}'},
                    headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields"]["amount"] == 250.0 and body["not_found_in_text"] == ["made_up"]
    assert "Bayaran RM 250.00 diterima." in prompts[0][1] and body["pages"][0]["source"] == "glm"


def test_what_is_sent_to_the_model_server_for_a_picture(monkeypatch):
    sent = {}

    class Reply:
        status_code = 200

        def json(self):
            return {"message": {"role": "assistant", "content": "ok"}, "done": True}

    def post(url, json, timeout):
        sent.update(url=url, body=json)
        return Reply()

    monkeypatch.setattr(llm.httpx, "post", post)
    assert llm.read_image("Text Recognition:", b"\xff\xd8\xffabc") == "ok"
    msg = sent["body"]["messages"][0]
    assert sent["url"].endswith("/api/chat") and sent["body"]["model"] == "glm-ocr" and sent["body"]["stream"] is False
    assert msg["content"] == "Text Recognition:" and msg["images"] == ["/9j/YWJj"] and msg["role"] == "user"


# --- duplicates of a document, CSV, where rows are ---------------------------------------------
def test_the_same_invoice_in_another_file_is_reported(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    mine = {**INVOICE, "document_number": "INV-2026-00999"}
    lines = [ln.replace("INV-2026-00417", "INV-2026-00999") for ln in INVOICE_LINES]
    answers += [mine, mine]
    first = client.post("/v1/extract", files={"file": ("i1.png", synth.image_bytes(lines), "image/png")},
                        headers=api_key).json()
    other_scan = synth.image_bytes(lines + ["(second copy)"])
    again = client.post("/v1/extract", files={"file": ("i2.png", other_scan, "image/png")}, headers=api_key).json()
    assert first["duplicate"]["same_document_first_seen"] is None
    assert again["duplicate"]["same_document_first_seen"].endswith("Z") and again["duplicate_file_first_seen"] is None


def test_rows_as_csv_and_where_they_are(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers += [INVOICE, INVOICE]
    body = post_invoice(client, api_key).json()
    at = body["sources"]["line_items"]
    assert len(at) == 2 and at[0]["page"] == 1 and len(at[0]["box"]) == 4 and 0 < at[0]["confidence"] <= 1
    assert 0 < body["sources"]["document_number"]["confidence"] <= 1
    r = client.post("/v1/extract", files={"file": ("i.png", synth.image_bytes(INVOICE_LINES), "image/png")},
                    headers={**api_key, "Accept": "text/csv"})
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines() == ["description,quantity,unit_price,amount", "Widget,2.0,50.0,100.0", "Gasket,1.0,25.5,25.5"]


def test_get_ocr_vision_is_a_second_vision_reader(client, api_key, need_engine, fake_vision, monkeypatch):
    png = synth.image_bytes(["hello"])
    r = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "vision"}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "get-ocr-vision" and body["pages"][0]["source"] == "vision" and body["pages"][0]["lines"] == []
    assert models[-1] == "get-ocr-vision" and fake_vision[-1][0] == "Text Recognition:"
    # the first reader is still the first
    client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "glm"}, headers=api_key)
    assert models[-1] == "glm-ocr"
    # what only the second reader was trained to do
    client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "vision", "task": "seal"}, headers=api_key)
    assert fake_vision[-1][0] == "Seal Recognition:"
    client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "vision", "task": "chart"}, headers=api_key)
    assert fake_vision[-1][0] == "Chart to Table:"
    bad = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "glm", "task": "seal"}, headers=api_key)
    assert bad.status_code == 422 and "engine=vision" in bad.json()["message"]
    assert client.get("/health").json()["vision"] is True


def test_get_ocr_vision_is_off_until_it_is_installed(client, api_key, need_engine):
    llm._vision_status = {}
    r = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(["hello"]), "image/png")},
                    data={"engine": "vision"}, headers=api_key)
    assert r.status_code == 503 and r.json()["error"] == "reader_unavailable" and "get_vision_model.sh" in r.json()["message"]
    assert client.get("/health").json()["vision"] is False


def test_a_table_in_cell_marks_becomes_html():
    from app import vision
    plain = "<otsl><fcel>Item<fcel>Qty<fcel>Price<nl><fcel>Kertas A4<fcel>10<fcel>125.50<nl><fcel>Jumlah<ecel><fcel>365.50<nl></otsl>"
    assert vision.otsl_to_html(plain) == ("<table><tr><td>Item</td><td>Qty</td><td>Price</td></tr><tr><td>Kertas A4</td><td>10</td>"
                                           "<td>125.50</td></tr><tr><td>Jumlah</td><td></td><td>365.50</td></tr></table>")
    # a heading over two columns, a cell over two rows, and text that must not become markup
    merged = "<ched>Amount (RM)<lcel><ched>Note<nl><fcel>Debit<fcel>Credit<fcel>a < b<nl><fcel>1.00<fcel>2.00<ucel><nl>"
    html = vision.otsl_to_html(merged)
    assert '<th colspan="2">Amount (RM)</th><th>Note</th>' in html and '<td rowspan="2">a &lt; b</td>' in html
    assert html.count("<tr>") == 3 and html.endswith("<td>1.00</td><td>2.00</td></tr></table>")
    assert vision.otsl_to_html("no table here") == "no table here"
    # the service's own reader of merged cells understands the result
    from app import tablegrid
    cells = tablegrid.cells_of(html)
    assert any(c["text"] == "Amount (RM)" and c["colspan"] == 2 for c in cells)


# --- words one reader left out, taken from the standard reader --------------------------------
def test_vision_reading_gets_the_printed_lines_the_model_left_out(client, api_key, need_engine, fake_vision):
    png = synth.image_bytes(["MINISTRY OF TRANSPORT", "Kepada Encik Ali,", "Bayaran RM 250.00 diterima."])
    r = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "vision"}, headers=api_key)
    assert r.status_code == 200, r.text
    page = r.json()["pages"][0]
    assert [a["text"] for a in page["added_lines"]] == ["MINISTRY OF TRANSPORT"]
    assert page["text"].split("\n") == ["MINISTRY OF TRANSPORT", "Kepada Encik Ali,", "Bayaran RM 250.00 diterima."]


def test_nothing_is_added_when_the_vision_model_read_it_all(client, api_key, need_engine, fake_vision):
    png = synth.image_bytes(["Kepada Encik Ali,", "Bayaran RM 250.00 diterima."])
    page = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "vision"},
                       headers=api_key).json()["pages"][0]
    assert "added_lines" not in page and page["text"].split("\n") == ["Kepada Encik Ali,", "Bayaran RM 250.00 diterima."]


def test_a_line_the_model_read_a_little_differently_is_not_added_twice():
    from app import gaps
    lines = [{"text": "Bayaran RM 25O.00 diterima", "confidence": 0.99, "box": [[0, 0], [300, 0], [300, 20], [0, 20]]},
             {"text": "ab", "confidence": 0.99, "box": [[0, 40], [30, 40], [30, 60], [0, 60]]},
             {"text": "TIDAK PASTI", "confidence": 0.6, "box": [[0, 80], [200, 80], [200, 100], [0, 100]]}]
    assert gaps.fill("Bayaran RM 250.00 diterima.", lines) == ("Bayaran RM 250.00 diterima.", [])


def test_words_in_a_picture_on_a_text_pdf_are_read_too(client, api_key, need_engine):
    pdf = synth.letterhead_pdf(["NORTHWIND TRADING"], "Invoice 2026-0042 for the goods delivered")
    r = client.post("/v1/ocr", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=api_key)
    assert r.status_code == 200, r.text
    page = r.json()["pages"][0]
    assert page["source"] == "text_layer" and [a["text"] for a in page["added_lines"]] == ["NORTHWIND TRADING"]
    assert page["text"].split("\n") == ["NORTHWIND TRADING", "Invoice 2026-0042 for the goods delivered"]


def test_a_text_pdf_without_pictures_is_left_as_it_is(client, api_key, need_engine):
    page = client.post("/v1/ocr", files={"file": ("a.pdf", synth.text_pdf("Invoice 2026-0042 for the goods delivered"),
                                                  "application/pdf")}, headers=api_key).json()["pages"][0]
    assert "added_lines" not in page and page["text"] == "Invoice 2026-0042 for the goods delivered"


def test_a_text_pdf_gives_the_place_of_each_line_and_of_the_added_ones(client, api_key, need_engine):
    pdf = synth.letterhead_pdf(["NORTHWIND TRADING"], "Invoice 2026-0042 for the goods delivered")
    page = client.post("/v1/ocr", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=api_key).json()["pages"][0]
    stored = [ln for ln in page["lines"] if not ln.get("added")]
    assert [ln["text"] for ln in stored] == ["Invoice 2026-0042 for the goods delivered"] and stored[0]["confidence"] is None
    assert 45 < stored[0]["box"][0][0] < 55 and 270 < stored[0]["box"][0][1] < 300  # points from the top left
    added = [ln for ln in page["lines"] if ln.get("added")]
    assert [ln["text"] for ln in added] == ["NORTHWIND TRADING"]
    assert 40 < added[0]["box"][0][0] < 300 and 40 < added[0]["box"][0][1] < 140  # inside the picture at the top
    brief = client.post("/v1/ocr", files={"file": ("a.pdf", pdf, "application/pdf")}, data={"detail": "text"},
                        headers=api_key).json()["pages"][0]
    assert "lines" not in brief and brief["added_lines"][0]["text"] == "NORTHWIND TRADING"


def test_pieces_far_apart_and_off_the_same_baseline_are_separate_lines():
    from app.documents import _apart

    def piece(text, x0, y0, x1, y1):
        return {"text": text, "confidence": None, "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}
    address = [piece("68100 KAJANG", 19, 222.5, 120, 229.1), piece("KETUA PENGARAH", 323, 224.0, 391, 229.6)]
    assert _apart("68100 KAJANG KETUA PENGARAH", address, 419) == "68100 KAJANG\nKETUA PENGARAH"
    row = [piece("Widget A4", 19, 222.5, 120, 229.1), piece("RM 120.00", 323, 222.5, 391, 229.1)]  # a table row
    assert _apart("Widget A4 RM 120.00", row, 419) == "Widget A4 RM 120.00"


def test_a_table_read_by_the_vision_model_lists_what_else_is_on_the_page(client, api_key, need_engine, monkeypatch):
    monkeypatch.setattr(llm, "vision_ready", lambda *a, **k: True)
    monkeypatch.setattr(llm, "read_image", lambda *a, **k: "<fcel>Widget<fcel>120.00<nl>")
    png = synth.image_bytes(["NORTHWIND TRADING", "Widget 120.00"])
    page = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"engine": "vision", "task": "table"},
                       headers=api_key).json()["pages"][0]
    assert page["text"].startswith("<table>") and "NORTHWIND" not in page["text"]
    assert [a["text"] for a in page["other_lines"]] == ["NORTHWIND TRADING"] and "added_lines" not in page


# --- the vehicle ownership certificate, read by its labels ------------------------------------
CERTIFICATE = ["SIJIL PEMILIKAN KENDERAAN", "JABATAN PENGANGKUTAN JALAN", "No. Pendaftaran : WXY1234",
               "No. ID : 800101145678", "Nama Pemunya Berdaftar : AHMAD BIN ALI", "Alamat : 12 JALAN MERANTI 3",
               "No. Chasis/No. Enjin : PM2B301S009912345 / 1NR7654", "Buatan/Nama Model : PERODUA / MYVI 1.5",
               "Keupayaan Enjin : 1496", "Bahan Bakar : PETROL", "Status Asal : PEMASANGAN TEMPATAN",
               "Kelas Kegunaan : PERSENDIRIAN", "Jenis Badan/Tahun Dibuat : MOTOKAR / 2021",
               "Tarikh Pendaftaran : 05/03/2021", "B.D.M/B.G.K/BTM : -", "Syarat Pendaftaran 1 : -"]


def test_the_vehicle_certificate_is_read_by_its_labels_without_the_model(client, api_key, need_engine, fake_model):
    r = client.post("/v1/extract", files={"file": ("geran.png", synth.image_bytes(CERTIFICATE), "image/png")}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    ex = body.get("extraction") or body
    f = ex["fields"]
    assert ex["document_type"] == "vehicle_registration" and ex["read_by"] == "labels" and ex["model_calls"] == 0
    assert (f["plate_number"], f["owner_id_number"], f["owner_name"]) == ("WXY1234", "800101145678", "AHMAD BIN ALI")
    assert (f["chassis_number"], f["engine_number"]) == ("PM2B301S009912345", "1NR7654")
    assert (f["make"], f["model"], f["body_type"], f["year_made"]) == ("PERODUA", "MYVI 1.5", "MOTOKAR", 2021)
    assert (f["engine_capacity_cc"], f["fuel_type"], f["registration_date"]) == (1496, "PETROL", "2021-03-05")
    assert f["weights"] is None and f["registration_conditions"] is None and f["colour"] is None
    assert ex["checks_passed"] is True


def test_the_older_certificate_with_a_label_for_each_value_is_read_too():
    from app import vehicle

    def line(text, x, y):
        return {"text": text, "confidence": 0.99, "box": [[x, y], [x + 12 * len(text), y], [x + 12 * len(text), y + 24], [x, y + 24]]}
    rows = [("SIJIL PEMILIKAN KENDERAAN", None), ("No. Pendaftaran", "BKV 8821"), ("Nama Pemunya Berdaftar", ": SITI BINTI OMAR"),
            ("Alamat", ": 4 LORONG KENANGA"), (None, "81200 JOHOR BAHRU JOHOR"), ("No.Enjin", ": 4G15P123456"),
            ("No. Casis", ": PL1C22SNL8B123456"), ("Buatan", ": PROTON"), ("Nama Model", ": SAGA 1.3"),
            ("Tahun Dibuat", ": 2008"), ("Tarikh Pendaftaran", ": 21/07/2008"),
            ("SYARAT-SYARAT PENDAFTARAN", None), ("1. Tidak boleh tukar milik dalam 4 tahun", None)]
    lines = []
    for n, (label, value) in enumerate(rows):
        if label:
            lines.append(line(label, 260, 60 + 40 * n))
        if value:
            lines.append(line(value, 620, 60 + 40 * n))
    lines += [line("No. ID : 850505015432", 800, 100), line("0012345", 1220, 60)]
    f = vehicle.parse([{"page": 1, "width": 1400, "lines": lines}])["fields"]
    assert (f["plate_number"], f["owner_id_number"], f["certificate_serial"]) == ("BKV8821", "850505015432", "0012345")
    assert f["owner_address"] == "4 LORONG KENANGA, 81200 JOHOR BAHRU JOHOR"
    assert (f["engine_number"], f["chassis_number"], f["make"], f["model"], f["year_made"]) == \
        ("4G15P123456", "PL1C22SNL8B123456", "PROTON", "SAGA 1.3", "2008")
    assert f["registration_conditions"] == "1. Tidak boleh tukar milik dalam 4 tahun"
    assert vehicle.parse([{"page": 1, "width": 1400, "lines": [line("INVOICE", 100, 100)]}]) is None


# --- the road tax disc and the identity card, read by position --------------------------------
def _at(text, x, y, h=24):
    return {"text": text, "confidence": 0.99, "box": [[x, y], [x + 12 * len(text), y], [x + 12 * len(text), y + h], [x, y + h]]}


ROAD_TAX = [_at("LESEN KENDERAAN MOTOR", 190, 150, 60), _at("2024", 790, 460, 80), _at("WXY1234", 320, 620, 60),
            _at("30 JUN", 790, 620, 60), _at("0116001 I8imwY3 01072023", 267, 727), _at("01072023 30062024", 783, 723),
            _at("1496SP AB RM90.00", 782, 759), _at("SEMENANJUNG", 267, 798), _at("PERSENDIRIAN", 783, 795),
            _at("998877665", 1724, 474), _at("77665", 1062, 521)]


def test_the_road_tax_disc_is_read_by_shape_and_its_expiry_is_checked():
    from datetime import date
    from app import roadtax
    out = roadtax.parse([{"page": 1, "width": 2000, "lines": ROAD_TAX}], today=date(2024, 1, 15))
    f = out["fields"]
    assert (f["plate_number"], f["period_start"], f["expiry_date"]) == ("WXY1234", "2023-07-01", "2024-06-30")
    assert (f["amount"], f["currency"], f["engine_capacity_cc"]) == ("90.00", "MYR", "1496")
    assert (f["region"], f["usage_class"], f["serial_number"]) == ("SEMENANJUNG", "PERSENDIRIAN", "998877665")
    assert out["checks"][0]["name"] == "expiry_matches" and out["checks"][0]["passed"] is True
    assert out["extra"] == {"expired": False}
    assert roadtax.parse([{"page": 1, "width": 2000, "lines": ROAD_TAX}], today=date(2024, 7, 1))["extra"] == {"expired": True}
    altered = [ln if ln["text"] != "30 JUN" else _at("30 DIS", 790, 620, 60) for ln in ROAD_TAX]
    assert roadtax.parse([{"page": 1, "width": 2000, "lines": altered}])["checks"][0]["passed"] is False
    assert roadtax.parse([{"page": 1, "width": 2000, "lines": [_at("INVOICE 2024", 10, 10)]}]) is None


MYKAD = [_at("KAD PENGENALAN", 120, 22), _at("MyKad", 754, 42), _at("900412 10 5678", 32, 167),
         _at("TAN MEI LING", 34, 531), _at("NO 8 JALAN SS 2/4", 36, 607), _at("TAMAN SEA", 37, 641),
         _at("47300 PETALING JAYA", 37, 675), _at("SELANGOR", 36, 709), _at("WARGANEGARA", 900, 694),
         _at("PEREMPUAN", 1000, 721)]


def test_the_identity_card_is_read_by_position_and_checked_against_its_number():
    from datetime import date
    from app import mykad
    out = mykad.parse([{"page": 1, "width": 1280, "lines": MYKAD}], today=date(2026, 1, 1))
    f = out["fields"]
    assert (f["id_number"], f["name"], f["gender"], f["citizenship"]) == ("900412-10-5678", "TAN MEI LING", "female", "WARGANEGARA")
    assert f["address"] == "NO 8 JALAN SS 2/4, TAMAN SEA, 47300 PETALING JAYA, SELANGOR"
    assert (f["postcode"], f["city"], f["state"]) == ("47300", "PETALING JAYA", "SELANGOR")
    assert (f["date_of_birth"], f["place_of_birth"]) == ("1990-04-12", "Selangor")
    assert {c["name"]: c["passed"] for c in out["checks"]} == {"number_holds_a_date": True, "gender_matches_number": True,
                                                               "postcode_matches_state": True} and out["extra"] == {}
    # a stroke lost in the name is put back, and what was read is kept beside it
    lost = [ln if ln["text"] != "TAN MEI LING" else _at("RAVI AL MUTHU", 34, 531) for ln in MYKAD]
    out = mykad.parse([{"page": 1, "width": 1280, "lines": lost}])
    assert out["fields"]["name"] == "RAVI A/L MUTHU" and out["extra"] == {"name_as_read": "RAVI AL MUTHU"}
    assert out["checks"][1]["passed"] is True  # 8 is even, the card prints a woman
    wrong = [ln if ln["text"] != "PEREMPUAN" else _at("LELAKI", 1000, 721) for ln in MYKAD]
    assert mykad.parse([{"page": 1, "width": 1280, "lines": wrong}])["checks"][1]["passed"] is False
    assert mykad.born("051301105678") is None and mykad.born("050228105678", date(2026, 1, 1)) == "2005-02-28"
    assert mykad.parse([{"page": 1, "width": 1280, "lines": [_at("KAD PELAJAR", 10, 10)]}]) is None


def card(name, address, right=("WARGANEGARA", "LELAKI"), number="850412-10-5759", x=40, y=20, extra=()):
    """A made-up MyKad as the reader sees it: the lines of the name, then of the address, which ends at the
    card's foot whatever its length, with the citizenship and the gender beside its last rows."""
    foot = y + 722  # a name of two lines over an address of five leaves 36 between them, the lines 26 to 30 apart
    rows = [foot - 30 * (len(address) - n) for n in range(len(address))]
    lines = [_at("KAD PENGENALAN", x + 90, y), _at("MyKad", x + 720, y + 20), _at(number, x, y + 145)]
    lines += [_at(t, x + 2, y + 510 + 26 * n) for n, t in enumerate(name)]
    lines += [_at(t, x + 3, row) for t, row in zip(address, rows)]
    lines += [_at(t, x + 760 + 100 * n, foot - 60 + 28 * n) for n, t in enumerate(right)]
    return lines + list(extra)


def test_a_name_that_runs_on_to_a_second_line_stays_out_of_the_address():
    from app import mykad

    def read(lines, width=1280):
        return mykad.parse([{"page": 1, "width": width, "lines": lines}])["fields"]

    long = ["NO 70 LADANG", "TANJUNG BARU", "JALAN SINTOK", "06010 CHANGLUN", "KEDAH"]
    # the name is broken after A/L, and with five lines of address the two blocks nearly touch
    f = read(card(["RAVI A/L", "MUTHUSAMY"], long))
    assert f["name"] == "RAVI A/L MUTHUSAMY" and f["address"] == ", ".join(long)
    assert (f["postcode"], f["city"], f["state"], f["citizenship"], f["gender"]) == ("06010", "CHANGLUN", "KEDAH", "WARGANEGARA", "male")
    # the same card filling a part of a larger picture, as on a table or inside a phone's screen
    inside = card(["RAVI A/L", "MUTHUSAMY"], long, x=900, y=1400, extra=[_at("11:13", 150, 60), _at("id_front_179462350383.jpg", 200, 220)])
    assert read(inside, width=2400) == f
    short = ["LOT 12 KAMPUNG BARU", "15000 KOTA BHARU", "KELANTAN"]
    # the line above ends on a joining word, or the line itself starts with one
    assert read(card(["MUHAMMAD AMIRUL HAKIM BIN", "ABDUL RAHMAN"], short))["name"] == "MUHAMMAD AMIRUL HAKIM BIN ABDUL RAHMAN"
    f = read(card(["NUR AISYAH HUMAIRA", "BINTI ZULKIFLI"], short, right=("WARGANEGARA", "ISLAM", "PEREMPUAN")))
    assert (f["name"], f["address"], f["religion"]) == ("NUR AISYAH HUMAIRA BINTI ZULKIFLI", ", ".join(short), "ISLAM")
    # a single word under the name, with the address starting under it
    assert read(card(["SITI NORHALIZA BINTI MOHD", "ZULKIFLI"], short))["name"] == "SITI NORHALIZA BINTI MOHD ZULKIFLI"
    # more than three lines above the postcode: an address has three lines of its own at most
    f = read(card(["WONG SIEW LING", "MARY ANNE"], ["NO 1 JALAN 2", "TAMAN MELATI", "BANDAR BARU", "43000 KAJANG", "SELANGOR"]))
    assert f["name"] == "WONG SIEW LING MARY ANNE" and f["address"].startswith("NO 1 JALAN 2, TAMAN MELATI")
    # an address that begins without a number is no part of the name
    for first in ("KAMPUNG PAYA BESAR", "TANJUNG GENTING", "PUCHONG"):
        f = read(card(["LIM AH KOW"], [first, "06000 JITRA", "KEDAH"]))
        assert (f["name"], f["address"]) == ("LIM AH KOW", first + ", 06000 JITRA, KEDAH")
    f = read(card(["LIM AH KOW"], ["TAMAN SEA", "JALAN SS 2", "47300 PETALING JAYA", "SELANGOR"]))
    assert (f["name"], f["state"]) == ("LIM AH KOW", "SELANGOR")
    # what stands under the state is not the card's, nor is the tiny print of its background
    f = read(card(["LIM AH KOW"], short, extra=[_at("Shot on a phone", 42, 900), _at("KERAJAANMALAYSIAKERAJAAN", 44, 600)]))
    assert (f["name"], f["address"]) == ("LIM AH KOW", ", ".join(short))
    # the large number could not be read and the small one under the photo was: the number and what follows from
    # it are given, and the name and the address, which there is nothing left to place by, are left out
    faded = card(["LIM AH KOW"], short, number="85O4I2-1O-57S9") + [_at("850412-10-5759", 760, 600, 12)]
    f = read(faded)
    assert (f["id_number"], f["date_of_birth"], f["gender"], f["citizenship"]) == ("850412-10-5759", "1985-04-12", "male", "WARGANEGARA")
    assert "name" not in f and "address" not in f


def _span(text, x0, x1, y, h=44):
    return {"text": text, "confidence": 0.99, "box": [[x0, y], [x1, y], [x1, y + h], [x0, y + h]]}


def new_card(name, address, beside, number="900412-10-5678"):
    """A made-up new-generation card (from September 2026), in the places a reader gave for the published
    specimen: the photo at the left, the number, the name and the address right of it under one another, and
    `beside`, the lines a reader made of the citizenship, religion and gender under the photo (alone, or run
    together with the address's line on their row)."""
    lines = [_span("KADPENGENALAN", 487, 1089, 111), _span("MALAYSIA", 491, 1094, 145, 80), _span("MyKad", 1212, 1391, 149, 60),
             _span("IDENTITYCARD", 495, 1090, 212), _span(number, 483, 1018, 325, 70)]
    lines += [_span(t, 483, 483 + 33 * len(t), 535 + 48 * n, 55) for n, t in enumerate(name)]
    rows = [900 - 43 * (len(address) - 1 - n) for n in range(len(address))]
    lines += [_span(t, 484, 484 + 24 * len(t), row) for t, row in zip(address, rows) if t]
    return lines + list(beside)


ONE = ["NO 5 JALAN 3", "43000 KAJANG", "SELANGOR"]
TWO = ["LOT 12 KAMPUNG BARU", "MUKIM PAYA", "15000 KOTA BHARU", "KELANTAN"]
THREE = ["NO 70 LADANG", "TANJUNG BARU", "JALAN SINTOK", "06010 CHANGLUN", "KEDAH"]
SHAPES = [  # the name's lines, the address's lines, the name they give
    (["AHMAD FAIZAL BIN", "ABDUL RAHMAN"], ONE, "AHMAD FAIZAL BIN ABDUL RAHMAN"),
    (["AHMAD FAIZAL", "BIN ABDUL RAHMAN"], TWO, "AHMAD FAIZAL BIN ABDUL RAHMAN"),
    (["NUR AISYAH BINTI MOHD", "ZAINAL ABIDIN"], ONE, "NUR AISYAH BINTI MOHD ZAINAL ABIDIN"),
    (["NUR AISYAH BINTI MOHD", "ZAINAL ABIDIN"], TWO, "NUR AISYAH BINTI MOHD ZAINAL ABIDIN"),
    (["NUR AISYAH BINTI MOHD", "ZAINAL ABIDIN"], THREE, "NUR AISYAH BINTI MOHD ZAINAL ABIDIN"),
    (["TAN AH KOW @", "TAN KIM SENG"], ONE, "TAN AH KOW @ TAN KIM SENG"),
    (["TAN AH KOW", "@ TAN KIM SENG"], ONE, "TAN AH KOW @ TAN KIM SENG"),
    (["KAVITHA A/P", "RAMASAMY"], TWO, "KAVITHA A/P RAMASAMY"),
    (["KAVITHA AP", "RAMASAMY"], TWO, "KAVITHA A/P RAMASAMY"),
    (["RAVI A L", "MUTHU KUMAR"], ONE, "RAVI A/L MUTHU KUMAR"),
    (["JUGAH ANAK", "BARIENG"], ONE, "JUGAH ANAK BARIENG"),
    (["JUGAH AK", "BARIENG"], ONE, "JUGAH AK BARIENG"),
    (["TENGKU MUHAMMAD FAIZ PETRA IBNI", "ALMARHUM SULTAN ISMAIL", "PETRA"], ONE, "TENGKU MUHAMMAD FAIZ PETRA IBNI ALMARHUM SULTAN ISMAIL PETRA"),
    (["SUPARMAN"], TWO, "SUPARMAN"),
    (["HARJIT SINGH A/L", "GURDEV SINGH"], THREE, "HARJIT SINGH A/L GURDEV SINGH"),
    (["SITI AMINAH BT", "ABU BAKAR"], ONE, "SITI AMINAH BT ABU BAKAR"),
    (["NIK NUR A'ISYAH BINTI", "NIK ABDUL-AZIZ"], ONE, "NIK NUR A'ISYAH BINTI NIK ABDUL-AZIZ"),
    (["LIM AH KOW"], ["SEKOLAH KEBANGSAAN SERI", "JALAN 2", "TAMAN MELATI", "43000 KAJANG", "SELANGOR"], "LIM AH KOW"),
    (["LIM AH KOW"], ["ESPEK TANJUNG", "06010 CHANGLUN", "KEDAH"], "LIM AH KOW"),
    (["LIM AH KOW"], ["RUMAH PANJANG ENTAWAU", "SUNGAI ASSAN", "ULU OYA", "96000 SIBU", "SARAWAK"], "LIM AH KOW"),
    (["LIM AH KOW"], ["SUBANG", "NO 5 JALAN 3", "47500 SUBANG JAYA", "SELANGOR"], "LIM AH KOW"),
    (["DAYANG NURFAIZAH BINTI", "AWANG"], ["PETI SURAT 123", "89007 KENINGAU", "SABAH"], "DAYANG NURFAIZAH BINTI AWANG"),
    (["LIM AH KOW"], ["D/A KEDAI RUNCIT AH SENG", "KAMPUNG BARU", "28400 MENTAKAB", "PAHANG"], "LIM AH KOW"),
    (["LIM AH KOW"], ["8 JALAN 10 LEMBAH JAYA", "68000AMPANG", "SELANGOR"], "LIM AH KOW"),
    (["LIM AH KOW"], ["8 JALAN 10 LEMBAH JAYA", "68000 AMPANG"], "LIM AH KOW"),
    (["LIM AH KOW BIN", "ABDULLAH"], ["68000 AMPANG", "SELANGOR"], "LIM AH KOW BIN ABDULLAH"),
    (["LIM AH KOW"], ["NO 70 LADANG", "O6010 CHANGLUN", "KEDAH"], "LIM AH KOW"),
    (["WONG SIEW LING"], ["12-3A", "JALAN TELAWI", "59100 KUALA LUMPUR", "W. PERSEKUTUAN(KL)"], "WONG SIEW LING"),
]


def test_names_and_addresses_of_many_shapes_on_both_designs():
    import re
    from app import mykad
    under_photo = [_span("WARGANEGARA", 67, 330, 857), _span("LELAKI", 65, 216, 900)]
    for name, address, whole in SHAPES:
        for lines in (card(name, address), new_card(name, address, under_photo, number="850412-10-5759")):
            f = mykad.parse([{"page": 1, "width": 1562, "lines": lines}])["fields"]
            printed = [re.sub(r"^(\d{5})(?=[A-Z])", r"\1 ", line) for line in address]  # a postcode run into its town gets its space
            assert (f["name"], f["address"].split(", ")) == (whole, printed), (name, address, f)
            assert (f["gender"], f["citizenship"]) == ("male", "WARGANEGARA")
    # photographed at an angle: the card's left edge slants, each line starting further left than the one above
    for name, address, whole in SHAPES[:6]:
        slanted = [{**ln, "box": [[x - 0.13 * y, y] for x, y in ln["box"]]} for ln in card(name, address, x=200)]
        f = mykad.parse([{"page": 1, "width": 1562, "lines": slanted}])["fields"]
        assert (f["name"], f["address"].split(", "), f["gender"]) == (whole, address, "male"), (name, address, f)
    # a card left on its head (each line is read the right way round, only the places are turned): nothing is
    # read from it that way up, and the page turned over gives the whole card, asked for by name or not
    upright = card(["RAVI A/L", "MUTHUSAMY"], THREE)
    on_its_head = {"page": 1, "width": 1280, "height": 800, "text": "\n".join(ln["text"] for ln in upright),
                   "lines": [{**ln, "box": [[1280 - x, 800 - y] for x, y in ln["box"]]} for ln in upright]}
    assert mykad.parse([on_its_head]) is None
    for wanted in ("mykad", "auto"):
        out = extract.run([on_its_head], [on_its_head["text"]], wanted)
        assert (out["document_type"], out["read_by"], out["model_calls"]) == ("mykad", "labels", 0)
        assert (out["fields"]["name"], out["fields"]["address"]) == ("RAVI A/L MUTHUSAMY", ", ".join(THREE))
    # a second card lower in the picture, its lines starting where the first one's do, is not read into the first
    two = card(["LIM AH KOW"], ONE) + card(["TAN MEI LING"], TWO, y=900, number="900412-10-5678")
    f = mykad.parse([{"page": 1, "width": 1280, "lines": two}])["fields"]
    assert (f["id_number"], f["name"], f["address"]) == ("850412-10-5759", "LIM AH KOW", ", ".join(ONE))
    # the words at the lower right run together, and a letter for a native of Sabah or Sarawak beside them
    f = mykad.parse([{"page": 1, "width": 1280, "lines": card(["LIM AH KOW"], ONE, right=("WARGANEGARA", "ISLAMLELAKI", "K"))}])["fields"]
    assert (f["citizenship"], f["religion"], f["gender"]) == ("WARGANEGARA", "ISLAM", "male")


def test_a_card_whose_heading_the_hologram_spoils_is_still_read():
    from app import mykad

    def page(lines):
        return {"page": 1, "width": 1280, "height": 800, "lines": lines, "text": "\n".join(ln["text"] for ln in lines)}

    sarawak = ["NO 11 LOT 16 LORONG 1", "JALAN SIBU BARAT 2", "TAMAN TUNKU", "98000MIRI", "SARAWAK"]
    whole = card(["LIM AH KOW"], sarawak, right=("WARGANEGARA", "LELAKI", "K"))
    # a letter of the heading lost, and nothing else of it read
    spoiled = [ln for ln in whole if ln["text"] not in ("KAD PENGENALAN", "MyKad")] + [_at("MALAYSTN", 130, 20), _at("IENTITYCARD", 130, 60)]
    assert mykad.is_card(spoiled) and extract.identify([page(spoiled)], [page(spoiled)["text"]]) == "mykad"
    f = mykad.parse([page(spoiled)])["fields"]
    assert (f["name"], f["postcode"], f["city"], f["state"]) == ("LIM AH KOW", "98000", "MIRI", "SARAWAK")
    assert f["address"] == "NO 11 LOT 16 LORONG 1, JALAN SIBU BARAT 2, TAMAN TUNKU, 98000 MIRI, SARAWAK"  # the postcode with its space
    # no heading read at all: not named a card by this service itself, but read for a caller who says it is one
    bare = [ln for ln in whole if ln["text"] not in ("KAD PENGENALAN", "MyKad")]
    assert not mykad.is_card(bare) and extract.identify([page(bare)], [page(bare)["text"]]) is None
    out = extract.run([page(bare)], [page(bare)["text"]], "mykad")
    assert (out["read_by"], out["model_calls"], out["fields"]["name"], out["fields"]["state"]) == ("labels", 0, "LIM AH KOW", "SARAWAK")
    on_its_head = page([{**ln, "box": [[1280 - x, 800 - y] for x, y in ln["box"]]} for ln in bare])
    assert extract.run([on_its_head], [on_its_head["text"]], "mykad")["fields"]["name"] == "LIM AH KOW"
    # a paper that is no card, though it holds a number of twelve digits alone on a line, is not read as one
    slip = page([_at("TRANSFER SLIP", 40, 20), _at("850412105759", 40, 80), _at("AMOUNT RM 50.00", 40, 140)])
    assert mykad.parse([slip]) is None
    # the postcode is held against the state: a digit misread shows, and a town on a border does not
    by = lambda lines: {c["name"]: c for c in mykad.parse([page(lines)])["checks"]}  # noqa: E731
    assert by(whole)["postcode_matches_state"]["passed"] is True
    wrong = card(["LIM AH KOW"], sarawak[:3] + ["88000 MIRI", "SARAWAK"])
    assert by(wrong)["postcode_matches_state"]["passed"] is False and "Sabah" in by(wrong)["postcode_matches_state"]["detail"]
    assert by(card(["LIM AH KOW"], ["NO 5 JALAN 3", "68000 AMPANG", "W. PERSEKUTUAN(KL)"]))["postcode_matches_state"]["passed"] is True
    assert by(card(["LIM AH KOW"], ["NO 5 JALAN 3", "34950 BANDAR BAHARU", "KEDAH"]))["postcode_matches_state"]["passed"] is True
    assert "postcode_matches_state" not in by(card(["LIM AH KOW"], ["NO 5 JALAN 3", "43000 KAJANG"]))  # no state printed: nothing to hold it against


def test_what_stands_under_a_card_is_not_read_into_it_and_a_page_that_is_no_card_is_not_read_as_one():
    from app import mykad

    def page(lines, width=1562, height=1100):
        return {"page": 1, "width": width, "height": height, "lines": lines, "text": "\n".join(ln["text"] for ln in lines)}

    def read(lines):
        return mykad.parse([page(lines)])["fields"]

    under_photo = [_span("WARGANEGARA", 67, 330, 857), _span("LELAKI", 65, 216, 900)]
    # a phone's mark, the card's back copied under its front, a note under a photocopy: a name of two lines with no
    # joining word between them stays whole, on both designs, whatever stands under the card
    for name in (["NUR AISYAH BINTI MOHD", "ZAINAL ABIDIN"], ["WONG SIEW LING", "MARY ANNE"]):
        for address in (ONE, TWO):
            for alone, under in ((card(name, address), [_at("SHOT ON PHONE 12", 42, 900), _at("AI QUAD CAMERA", 42, 940)]),
                                 (card(name, address), [_at("850412-10-5759-01-01", 42, 1000), _at("KETUA PENGARAH PENDAFTARAN NEGARA", 42, 1100)]),
                                 (new_card(name, address, under_photo, number="850412-10-5759"), [_span("Shot on a phone", 484, 844, 1100)])):
                f = read(alone + under)
                assert f == read(alone) and (f["name"], f["address"]) == (" ".join(name), ", ".join(address)), (name, address, f)
    # a territory printed on the postcode's own line has no state under it: a line further down is not the state
    territory = new_card(["LIM AH KOW"], ["NO. 20", "PERSIARAN PERDANA", "62551 W.P PUTRAJAYA"], under_photo, number="850412-16-5759")
    f = read(territory + [_span("SHOT ON PHONE 12", 484, 900, 1060)])
    assert (f["address"], f["state"]) == ("NO. 20, PERSIARAN PERDANA, 62551 W.P PUTRAJAYA", "W.P PUTRAJAYA")
    # a line under the card that starts with five digits is not the card's postcode
    f = read(card(["LIM AH KOW"], ONE) + [_at("50450 KUALA LUMPUR", 43, 1000), _at("MALAYSIA", 43, 1030)])
    assert (f["address"], f["postcode"], f["state"]) == (", ".join(ONE), "43000", "SELANGOR")
    # a house number of five digits has the address's other lines under it
    f = read(card(["LIM AH KOW"], ["10234 JALAN 5", "TAMAN MELATI", "43000 KAJANG", "SELANGOR"]))
    assert (f["postcode"], f["city"], f["state"]) == ("43000", "KAJANG", "SELANGOR")
    # a house number alone on the address's first line, and a name's last line of two letters, are the card's own
    for first in ("12", "5", "8A"):
        for lines in (card(["LIM AH KOW"], [first, "JALAN TELAWI 3", "59100 KUALA LUMPUR", "W. PERSEKUTUAN(KL)"]),
                      new_card(["LIM AH KOW"], [first, "JALAN TELAWI 3", "59100 KUALA LUMPUR", "W.P KUALA LUMPUR"], under_photo, number="850412-10-5759")):
            assert read(lines)["address"].startswith(first + ", JALAN TELAWI 3, 59100 KUALA LUMPUR")
    for lines in (card(["ALEXANDER CHRISTOPHER BENJAMIN", "NG"], ONE), new_card(["ALEXANDER CHRISTOPHER BENJAMIN", "NG"], TWO, under_photo, number="850412-10-5759")):
        assert read(lines)["name"] == "ALEXANDER CHRISTOPHER BENJAMIN NG"
    # the place "ANAK BUKIT" over the postcode is the address's one line of its own; a father's name carried on is the name's
    f = read(card(["LIM AH KOW"], ["ANAK BUKIT", "06550 ALOR SETAR", "KEDAH"]))
    assert (f["name"], f["address"]) == ("LIM AH KOW", "ANAK BUKIT, 06550 ALOR SETAR, KEDAH")
    assert read(card(["JUGAH", "ANAK BARIENG"], ONE))["name"] == "JUGAH ANAK BARIENG"
    # the name's own line not read: no name is given, not the address's first line for one
    lost = [ln for ln in card(["LIM AH KOW"], ["NO 29", "JALAN WATAN 11", "68000 AMPANG", "SELANGOR"]) if ln["text"] != "LIM AH KOW"]
    f = read(lost)
    assert "name" not in f and f["address"] == "NO 29, JALAN WATAN 11, 68000 AMPANG, SELANGOR"
    # the card prints capitals: a line of small letters where the name's column starts is not its print, a letter read small is
    f = read(card(["LIM AH KOW"], ONE) + [_at("Registration", 42, 480)])
    assert (f["name"], f["address"]) == ("LIM AH KOW", ", ".join(ONE))
    out = mykad.parse([page(card(["LIM AH KoW"], ONE))])
    assert out["fields"]["name"] == "LIM AH KOW" and out["extra"] == {"name_as_read": "LIM AH KoW"}
    # an island of Pahang whose post goes through a town of Johor
    checks = {c["name"]: c["passed"] for c in mykad.parse([page(card(["LIM AH KOW"], ["KAMPUNG TEKEK", "PULAU TIOMAN", "86800 MERSING", "PAHANG"]))])["checks"]}
    assert checks["postcode_matches_state"] is True

    # --- pages that are not the card
    def not_a_card(lines, named=None):
        pg = page(lines)
        assert extract.identify([pg], [pg["text"]]) == named and not mykad.is_card(lines)
        assert mykad.parse([pg]) is None and mykad.parse(extract._upside_down([pg])) is None

    # "Pengenalan" alone is a chapter's title or half of a column's heading, not the card's: a staff list is no card
    for text in ("PENGENALAN", "Pengenalan", "1. PENGENALAN", "PENGENALAN:", "KAD PENGENAL", "MKAD", "Kad Pengenalan (IC)", "New Identity Card",
                 "No. Kad Pengenalan", "MyKid"):
        assert not mykad._is_heading(text), text
    for text in ("KAD PENGENALAN", "IENTITYCARD", "IDENTITY CARD", "KADPENGENALAN", "KAD PENGENALAM", "MyKad", "MALAYSIA IDENTITY CARD"):
        assert mykad._is_heading(text), text
    not_a_card([_at("SENARAI PEKERJA SYARIKAT MAJU JAYA", 60, 40, 34), _at("No. Kad", 520, 133), _at("Nama", 60, 150), _at("Jantina", 900, 150),
                _at("Jawatan", 1150, 150), _at("Pengenalan", 520, 167), _at("LIM AH KOW", 60, 240), _at("850412-10-5759", 520, 240),
                _at("LELAKI", 900, 240), _at("PEMANDU", 1150, 240)])
    # a slip and an app's page that list what a card says, each value beside or under its label
    slip = [("Nama", "LIM AH KOW"), ("Jenis ID", "MyKad"), ("No. ID", "850412-10-5759"), ("Jantina", "LELAKI"), ("Warganegara", "MALAYSIA"),
            ("No. Telefon", "012-345 6789"), ("Pelan", "PRABAYAR 35")]
    not_a_card([_at("PENDAFTARAN PRABAYAR", 60, 40, 34)] + [ln for n, (label, value) in enumerate(slip)
                                                          for ln in (_at(label, 60, 120 + 44 * n), _at(value, 420, 120 + 44 * n))])
    profile = ["Profil saya", "Nama penuh", "LIM AH KOW", "MyKad", "850412-10-5759", "Jantina", "Lelaki", "Tarikh lahir", "12/04/1985", "Alamat",
               "NO 5 JALAN 3, 43000 KAJANG", "E-mel", "lim@example.com"]
    not_a_card([_at(t, 60, 40 + 60 * n) for n, t in enumerate(profile)])
    # a clinic's card with a person's details under one another and no label, for a caller who says it is a MyKad
    not_a_card([_at(t, 60, 40 + 50 * n) for n, t in enumerate(["KLINIK SIHAT", "KAD PENDAFTARAN PESAKIT", "LIM AH KOW", "850412-10-5759", "LELAKI",
                                                               "012-345 6789", "NO 5 JALAN 3", "43000 KAJANG"])])
    # the child's card has the same build and is another card: its holder is under twelve, which the reader does not reckon with
    kid = [ln if ln["text"] != "MyKad" else _at("MyKid", 760, 40) for ln in card(["NUR AISYAH BINTI AHMAD"], ONE, number="180305-10-5758")]
    not_a_card(kid)
    # a permanent resident's card is of the card's own family: read, with what it prints for the citizenship
    f = read(card(["SITI BINTI SUPARMAN"], ONE, right=("PEMASTAUTIN TETAP", "PEREMPUAN"), number="850412-10-5758"))
    assert (f["name"], f["citizenship"], f["gender"]) == ("SITI BINTI SUPARMAN", "PEMASTAUTIN TETAP", "female")


def test_a_stamp_laid_across_a_card_is_not_read_as_its_name():
    """An app stamps "GET RIDE / Registration" in red across the photo of a card it keeps. Read as a line where
    the name's column starts, "Registration" became the name and the name the first line of the address."""
    import math
    from app import headings, mykad

    def slanted(text, x, y, degrees, h=60):  # a line as a reader gives it when it runs across the page at an angle
        w, a = 30 * len(text), math.radians(degrees)
        across, down = (w * math.cos(a), w * math.sin(a)), (-h * math.sin(a), h * math.cos(a))
        return {"text": text, "confidence": 0.99, "box": [[x, y], [x + across[0], y + across[1]], [x + across[0] + down[0], y + across[1] + down[1]],
                                                          [x + down[0], y + down[1]]]}

    address = ["NO 29", "JALAN WATAN 11", "TAMAN SRI WATAN", "68000 AMPANG", "SELANGOR"]
    plain = card(["LIM AH KOW"], address)
    stamped = plain + [slanted("GETRIDE", 150, 400, -15), slanted("Registration", 70, 495, -15)]
    page = {"page": 1, "width": 1280, "height": 800, "lines": stamped, "text": "\n".join(ln["text"] for ln in stamped)}
    assert [ln["text"] for ln in headings.level(stamped)] == [ln["text"] for ln in plain]
    assert mykad.parse([page])["fields"]["name"] == "LIM AH KOW"  # the card's own lines slope as its number does
    for wanted in ("mykad", "auto"):
        f = extract.run([page], [page["text"]], wanted)["fields"]
        assert (f["name"], f["address"]) == ("LIM AH KOW", ", ".join(address))
    assert extract.identify([page], [page["text"]]) == "mykad"
    # a page that leans as a whole loses nothing; nor does a line too short to tell its slope by; a page turned over reads the same
    leaning = [slanted(t, 40, 100 + 60 * n, 10, 30) for n, t in enumerate(["KAD PENGENALAN", "850412-10-5759", "LIM AH KOW", "NO 5 JALAN 3"])]
    assert headings.level(leaning) == leaning
    short = plain + [slanted("K", 1200, 700, -40), slanted("OK", 600, 300, 35)]
    assert headings.level(short) == short
    over = [{**ln, "box": [[1280 - x, 800 - y] for x, y in ln["box"]]} for ln in stamped]
    assert sorted(ln["text"] for ln in headings.level(over)) == sorted(ln["text"] for ln in plain)
    # two slopes in numbers are two things in one picture (a disc lying level in a tilted holder), not a stamp: nothing is left out
    both = [slanted("LINE %d OF THE HOLDER" % n, 40, 60 * n, 22, 30) for n in range(6)] + [_at("LINE %d OF THE DISC" % n, 300, 500 + 40 * n) for n in range(5)]
    assert headings.level(both) == both
    # the card's background print, read as a line with a letter wrong, is no line of the card
    assert all(mykad._is_background(t) for t in ("AKERAJAANMALAY", "NMALA", "AKERAJAAI", "KERAJAANMALAYSIAKERAJAAN"))
    assert not any(mykad._is_background(t) for t in ("MALAYSIAN AIRLINES", "LIM AH KOW", "JALAN RAJA", "KERA", "TAMAN MALAYSIA RAYA"))


def test_a_name_is_put_right_only_where_the_card_itself_says_how():
    from datetime import date
    from app import mykad
    tidy = mykad._tidy_name
    # AL is A/L with its stroke lost only on a card without ISLAM and in a name without BIN or BINTI: it is also a
    # word of Arabic names, and those cards have one or the other
    assert tidy("RAVI AL MUTHU", muslim=False) == "RAVI A/L MUTHU" and tidy("KAVITHA AIP RAMASAMY", muslim=False) == "KAVITHA A/P RAMASAMY"
    for name in ("SYED AHMAD AL ATTAS", "NUR AL HUDA BINTI MOHD YUSOF", "SHARIFAH NUR ATIQAH BINTI SYED MOHD FAIZAL AL IDRUS"):
        assert tidy(name, muslim=True) == name
    assert tidy("MOHAMAD AL AMIN BIN ISMAIL", muslim=False) == "MOHAMAD AL AMIN BIN ISMAIL"  # ISLAM not read, BIN is
    # a space the reader lost where the card's own words show it, and no other
    assert tidy("ALEXANDER WONG KAH WAI @WONG KA WAI", False) == "ALEXANDER WONG KAH WAI @ WONG KA WAI"
    assert tidy("RAVI A/LMUTHUSAMY", False) == "RAVI A/L MUTHUSAMY"
    assert tidy("SITI NORHALIZABINTI ZULKIFLI", True) == "SITI NORHALIZA BINTI ZULKIFLI"
    assert tidy("AHMAD ZULKIFLIBIN ABDULLAH", True) == "AHMAD ZULKIFLI BIN ABDULLAH"
    for name in ("TAN ROBIN LEE", "MUHIBBIN BIN ALI", "AHMAD MUBIN", "JUGAH AKBARIENG"):  # a word that ends so, or a name with its own BIN
        assert tidy(name, True) == name
    assert tidy("MARY MAGDALENE @ MARIAM BINTI PETRUS GUNS\u00c1LAM", False) == "MARY MAGDALENE @ MARIAM BINTI PETRUS GUNSALAM"  # plain capitals

    def read(lines):
        return mykad.parse([{"page": 1, "width": 1562, "lines": lines}], today=date(2026, 11, 28))

    f = read(card(["SYED AHMAD AL", "ATTAS"], ONE, right=("WARGANEGARA", "ISLAM", "LELAKI")))["fields"]
    assert (f["name"], f["religion"]) == ("SYED AHMAD AL ATTAS", "ISLAM")
    out = read(card(["AHMAD ZULKIFLIBIN", "ABDULLAH"], THREE, right=("WARGANEGARA", "ISLAM", "LELAKI")))
    assert out["fields"]["name"] == "AHMAD ZULKIFLI BIN ABDULLAH" and out["extra"] == {"name_as_read": "AHMAD ZULKIFLIBIN ABDULLAH"}
    assert out["fields"]["address"] == ", ".join(THREE)
    # the holder is twelve at least: that says which century the number's two digits of a year mean
    assert mykad.born("250101085012", date(2026, 11, 28)) == "1925-01-01" and mykad.born("140101105012", date(2026, 11, 28)) == "2014-01-01"
    assert mykad.born("150101105012", date(2026, 11, 28)) == "1915-01-01" and mykad.born("000229105012", date(2026, 11, 28)) == "2000-02-29"
    # the place of birth: a state, a country or part of the world, or nothing where the code says nothing
    place = lambda code: read(card(["LIM AH KOW"], ONE, number="850412-%s-5759" % code))["fields"]["place_of_birth"]  # noqa: E731
    assert [place(c) for c in ("13", "71", "75", "82", "98", "99", "70", "00")] == ["Sarawak", "outside Malaysia", "outside Malaysia", None, None, None, None, None]
    # a town on a state's border with the postcode of the town across it is right as printed; a state written in older ways is known
    check = lambda address: {c["name"]: c["passed"] for c in read(card(["LIM AH KOW"], address))["checks"]}.get("postcode_matches_state")  # noqa: E731
    assert check(["PULAU SEBANG", "73000 TAMPIN", "MELAKA"]) is True and check(["GEMAS BAHARU", "73400 GEMAS", "JOHOR"]) is True
    assert check(["HULU BERNAM", "35900 TANJONG MALIM", "SELANGOR"]) is True and check(["NO 5 JALAN 3", "73000 TAMPIN", "KEDAH"]) is False
    assert check(["NO 5 JALAN 3", "21000 KUALA TERENGGANU", "TRENGGANU"]) is True and check(["NO 5 JALAN 3", "50000 KUALA LUMPUR", "W. PERSEKUTUAN(KL"]) is True
    assert check(["NO 5 JALAN 3", "50000 KUALA LUMPUR", "W. PERSEKUTUAN"]) is None  # which territory is not printed: nothing to hold it against


def test_the_new_generation_card_is_read_with_its_photo_at_the_left():
    from app import mykad

    def read(lines):
        return mykad.parse([{"page": 1, "width": 1562, "height": 1003, "lines": lines}])["fields"]

    # as a reader gave the specimen: the words under the photo run into the address's lines on their rows
    f = read(new_card(["TAN MEI LING"], ["NO.20", "JALAN MERANTI 3", "TAMAN SEA, SEKSYEN 2", "", ""],
                      [_span("WARGANEGARA K BANDAR BARU SELATAN", 67, 994, 857), _span("PEREMPUAN", 65, 280, 895),
                       _span("ISLAM47300 W.P KUALA LUMPUR", 330, 960, 894)]))
    assert (f["name"], f["citizenship"], f["religion"], f["gender"]) == ("TAN MEI LING", "WARGANEGARA", "ISLAM", "female")
    assert f["address"] == "NO. 20, JALAN MERANTI 3, TAMAN SEA, SEKSYEN 2, BANDAR BARU SELATAN, 47300 W.P KUALA LUMPUR"
    assert (f["postcode"], f["city"], f["state"]) == ("47300", "W.P KUALA LUMPUR", "W.P KUALA LUMPUR")  # a territory is its own town
    # each read on its own, with the state on a line under the postcode, and a word with a letter lost
    apart = [_span("WARGANEGARA", 67, 330, 857), _span("K", 400, 425, 857), _span("LELAKI", 65, 216, 900), _span("ISAM", 330, 478, 900)]
    f = read(new_card(["RAVI A/L", "MUTHUSAMY"], ["NO 7 JALAN 2", "43000 KAJANG", "SELANGOR"], apart, number="850412-10-5759"))
    assert (f["name"], f["address"]) == ("RAVI A/L MUTHUSAMY", "NO 7 JALAN 2, 43000 KAJANG, SELANGOR")
    assert (f["state"], f["citizenship"], f["religion"], f["gender"]) == ("SELANGOR", "WARGANEGARA", "ISLAM", "male")
    # the words under the photo read with a letter wrong where they run into the address, and the letter of Sarawak
    # run into the postcode's line
    woman = dict(number="900412-10-5678")
    for beside, rows in (
            ([_span("WARGANEGARA", 67, 330, 857), _span("PEREMPUAN", 65, 290, 900), _span("IS'AM SELANGOR", 330, 680, 900)], ["NO 7 JALAN 2", "TAMAN MELATI", "43000 KAJANG", ""]),
            ([_span("WARGANEGARA", 67, 330, 857), _span("PEREMPUAM ISLAM SELANGOR", 65, 680, 900)], ["NO 7 JALAN 2", "TAMAN MELATI", "43000 KAJANG", ""]),
            ([_span("WARGANEGAPA 43000 KAJANG", 67, 780, 857), _span("PEREMPUAN ISLAM", 65, 460, 900)], ["NO 7 JALAN 2", "TAMAN MELATI", "", "SELANGOR"])):
        f = read(new_card(["NUR AISYAH BINTI", "ZULKIFLI"], rows, beside, **woman))
        assert f["address"] == "NO 7 JALAN 2, TAMAN MELATI, 43000 KAJANG, SELANGOR", (beside, f)
        assert (f["name"], f["citizenship"], f["religion"], f["gender"]) == ("NUR AISYAH BINTI ZULKIFLI", "WARGANEGARA", "ISLAM", "female")
    f = read(new_card(["NUR AISYAH BINTI", "ZULKIFLI"], ["NO 11 LORONG 3", "TAMAN TUNKU", "", "SARAWAK"],
                      [_span("WARGANEGARA", 67, 392, 857), _span("K 93050 KUCHING", 428, 800, 857), _span("PEREMPUAN ISLAM", 65, 460, 900)], **woman))
    assert (f["address"], f["postcode"], f["state"]) == ("NO 11 LORONG 3, TAMAN TUNKU, 93050 KUCHING, SARAWAK", "93050", "SARAWAK")
    # a line of the address that begins as those words do stays the address's: it starts where the column does
    f = read(new_card(["LIM AH KOW"], ["ISLAMIC CENTRE QUARTERS", "JALAN PERDANA", "50480 KUALA LUMPUR", "W.P KUALA LUMPUR"],
                      [_span("WARGANEGARA", 67, 330, 857), _span("LELAKI", 65, 216, 900)], number="850412-10-5759"))
    assert f["address"].startswith("ISLAMIC CENTRE QUARTERS, JALAN PERDANA") and "religion" not in f and f["gender"] == "male"


def test_forms_read_by_position_are_recognised_on_auto(fake_model):
    pages = [{"page": 1, "width": 1280, "height": 800, "lines": MYKAD, "text": "\n".join(ln["text"] for ln in MYKAD)}]
    out = extract.run(pages, [pages[0]["text"]], "auto")
    assert out["document_type"] == "mykad" and out["read_by"] == "labels" and out["model_calls"] == 0
    assert out["fields"]["id_number"] == "900412-10-5678" and out["checks_passed"] is True
    pages = [{"page": 1, "width": 2000, "height": 1500, "lines": ROAD_TAX, "text": "\n".join(ln["text"] for ln in ROAD_TAX)}]
    out = extract.run(pages, [pages[0]["text"]], "auto")
    assert out["document_type"] == "road_tax" and out["fields"]["amount"] == 90.0 and "expired" in out["fields"]


# --- driving and vocational licences, read by their labels ------------------------------------
DRIVING = [_at("LESEN MEMANDU", 240, 80), _at("DRIVING LICENCE", 240, 110), _at("MALAYSIA", 790, 80, 30),
           _at("LIM AH KOW BIN", 400, 170, 30), _at("ABDULLAH", 400, 204, 30),
           _at("Warganegara / Nationality", 400, 250, 16), _at("No. Pengenalan / Identity No.", 740, 250, 16),
           _at("MALAYSIA", 400, 270), _at("850203075271", 740, 270), _at("Kelas / Class", 400, 310, 16),
           _at("Tarikh Lahir / Date Of Birth", 740, 310, 16), _at("B2 D", 400, 330), _at("03/02/1985", 740, 330),
           _at("Tempoh / Validity", 400, 370, 16), _at("26/01/2024 - 01/02/2025", 400, 395),
           _at("Alamat / Address", 400, 440, 16), _at("43 JALAN SHAIK MADAR", 400, 470), _at("11500 AYER ITAM", 400, 500),
           _at("PULAU PINANG", 400, 530)]
VOCATIONAL = [_at("LESEN VOKASIONAL MALAYSIA", 180, 40, 30), _at("MALAYSIAN VOCATIONAL LICENCE", 260, 75),
              _at("Nama / Name :", 300, 125, 16), _at("RAVI A/L MUTHU", 298, 150), _at("No. K.P. / I.D. : 900716055765", 296, 242),
              _at("MAL", 722, 250), _at("Tempoh / Valid Until :", 296, 280, 16), _at("07/09/2022 - 16/07/2024", 292, 305),
              _at("Jenis / Type : Kelas / Class :", 292, 345, 16), _at("PSV", 288, 370), _at("D:TEKSI/TEKSI MEWAH/", 410, 370),
              _at("KERETA SEWA/E-HAILING", 436, 400), _at("GDL", 288, 430), _at("E:BTM > 7500 KG (BERAT KEJUR)", 410, 430),
              _at("Ketua Pengarah Pengangkutan Jalan", 30, 510, 14)]


def test_the_driving_licence_is_read_by_its_labels():
    from datetime import date
    from app import licence
    out = licence.parse([{"page": 1, "width": 1136, "lines": DRIVING}], today=date(2024, 6, 1))
    f = out["fields"]
    assert out["kind"] == "driving_licence" and f["holder_name"] == "LIM AH KOW BIN ABDULLAH"  # a name over two lines
    assert (f["id_number"], f["nationality"], f["classes"]) == ("850203075271", "MALAYSIA", "B2 D")
    assert (f["issue_date"], f["expiry_date"], f["date_of_birth"]) == ("2024-01-26", "2025-02-01", "1985-02-03")
    assert f["address"] == "43 JALAN SHAIK MADAR, 11500 AYER ITAM, PULAU PINANG"
    assert out["checks"] == [{"name": "birth_date_matches_number", "passed": True,
                              "detail": "the number gives 1985-02-03, the card prints 1985-02-03"}]
    assert out["extra"] == {"expired": False}
    # the older card prints no date of birth: it is worked out from the number
    older = [ln for ln in DRIVING if ln["text"] not in ("Tarikh Lahir / Date Of Birth", "03/02/1985")]
    out = licence.parse([{"page": 1, "width": 1136, "lines": older}], today=date(2026, 1, 1))
    assert out["fields"]["date_of_birth"] == "1985-02-03" and out["checks"] == [] and out["extra"] == {"expired": True}


def test_the_vocational_licence_gives_its_types_and_whether_it_covers_e_hailing():
    from datetime import date
    from app import licence
    out = licence.parse([{"page": 1, "width": 855, "lines": VOCATIONAL}], today=date(2024, 1, 1))
    f = out["fields"]
    assert out["kind"] == "vocational_licence" and (f["holder_name"], f["id_number"], f["nationality"]) == \
        ("RAVI A/L MUTHU", "900716055765", "MAL")
    assert (f["issue_date"], f["expiry_date"], f["licence_types"]) == ("2022-09-07", "2024-07-16", "PSV, GDL")
    assert f["classes"] == "D:TEKSI/TEKSI MEWAH/KERETA SEWA/E-HAILING; E:BTM > 7500 KG (BERAT KEJUR)"
    assert out["extra"] == {"expired": False, "e_hailing": True}
    # the reader may give a type and its class as one line
    joined = [ln for ln in VOCATIONAL if ln["text"] not in ("PSV", "D:TEKSI/TEKSI MEWAH/", "GDL", "E:BTM > 7500 KG (BERAT KEJUR)")]
    joined.append(_at("PSV D:TEKSI/TEKSI MEWAH/", 288, 370))
    out = licence.parse([{"page": 1, "width": 855, "lines": joined}])
    assert out["fields"]["licence_types"] == "PSV" and out["extra"]["e_hailing"] is True
    assert licence.parse([{"page": 1, "width": 855, "lines": [_at("LESEN KENDERAAN MOTOR", 10, 10)]}]) is None


def test_licences_are_recognised_on_auto_and_nothing_is_reported_as_not_found(fake_model):
    for lines, kind in ((DRIVING, "driving_licence"), (VOCATIONAL, "vocational_licence"), (MYKAD, "mykad")):
        pages = [{"page": 1, "width": 1136, "height": 720, "lines": lines, "text": "\n".join(ln["text"] for ln in lines)}]
        out = extract.run(pages, [pages[0]["text"]], "auto")
        assert out["document_type"] == kind and out["read_by"] == "labels" and out["not_found_in_text"] == []
        assert out["checks_passed"] is True and "expired" in out["fields"] or kind == "mykad"


def test_a_driving_licence_with_a_label_unread_and_background_letters_is_still_read():
    from app import licence
    # a blurred photo: the nationality and number labels are not read, a label is misread, the class
    # letters of the card's background are picked up near the name, and the classes run together
    lines = [ln for ln in DRIVING if ln["text"] not in ("Warganegara / Nationality", "No. Pengenalan / Identity No.",
                                                        "Kelas / Class", "B2 D", "Tarikh Lahir / Date Of Birth", "03/02/1985")]
    lines += [_at("B1", 800, 140), _at("88", 700, 150), _at("A", 600, 225), _at("Kelas / Cless", 400, 310, 16), _at("B2D", 400, 330),
              _at("NO1", 400, 455)]
    f = licence.parse([{"page": 1, "width": 1136, "lines": lines}])["fields"]
    assert f["holder_name"] == "LIM AH KOW BIN ABDULLAH" and (f["id_number"], f["nationality"]) == ("850203075271", "MALAYSIA")
    assert f["classes"] == "B2 D" and f["address"].startswith("NO 1, 43 JALAN SHAIK MADAR")


def test_a_long_pack_is_named_by_its_first_pages():
    schedule = ("PRIVATE CAR SCHEDULE\nPolicy No. AB123\nSum Insured 26,000.00\nNCD 30%\nGross Premium 589.49\n"
                "Certificate of Insurance\nThe Insured: A PERSON")
    wording = "Bil elektrik dan bil air anda. Tunggakan bil. Caj semasa. Jumlah perlu dibayar. Meter reading. kWh."
    assert extract.guess_type(schedule) == "motor_insurance"
    assert extract._guess_pack([schedule] + [wording] * 30) == "motor_insurance"
    assert extract._guess_pack(["nothing that names a document", "nor here", "nor here", schedule]) == "motor_insurance"


def test_a_licence_in_a_wallet_under_another_card_keeps_its_name():
    from app import licence
    wallet = [_at("0105011", 900, 5), _at("Jenis / Type : Kelas / Class :", 650, 20, 16), _at("No SIRI: G 902030", 240, 40)]
    f = licence.parse([{"page": 1, "width": 1136, "lines": wallet + DRIVING}])["fields"]
    assert f["holder_name"] == "LIM AH KOW BIN ABDULLAH" and f["classes"] == "B2 D"


def test_a_vocational_licence_is_read_when_its_labels_are_misread():
    from app import licence
    # as real cards come out: the small labels garbled, the number glued to its label, the type unread
    lines = [_at("ESEN VOKASIONAL MALAYSIA", 180, 40, 30), _at("Na/ Nang", 300, 125, 16), _at("RAVI A/L MUTHU", 298, 150),
             _at("Ne.KP/1.0.:900716055765", 296, 242), _at("MAL.", 722, 250), _at("Tempoh / Vald Untif:", 296, 280, 16),
             _at("07//09/2022.- 16/07/2024", 292, 305), _at("Janis | TypeKalas / Cless", 292, 345, 16),
             _at("D:TEKSI/TEKSI MEWAH/", 410, 370), _at("ERETA SEWA/E-HAILING", 380, 400),
             _at("Ketua Pengarah Pengangkutan Jalan", 30, 510, 14), _at("No. SIRI: J 484702", 300, 700)]
    out = licence.parse([{"page": 1, "width": 855, "lines": lines}])
    f = out["fields"]
    assert (f["holder_name"], f["id_number"], f["nationality"]) == ("RAVI A/L MUTHU", "900716055765", "MAL")
    assert (f["issue_date"], f["expiry_date"], f["licence_types"]) == ("2022-09-07", "2024-07-16", "PSV")
    assert f["classes"] == "D:TEKSI/TEKSI MEWAH/ERETA SEWA/E-HAILING" and out["extra"]["e_hailing"] is True


# --- the motor insurance schedule, read by its labels and confirmed by its arithmetic ----------
def _schedule(shift=0):
    """A schedule as insurers lay it out: labels at the left with values beside or under them, and
    a premium column at the right. `shift` moves the amounts off their labels' level, as a photo does."""
    left = [("PRIVATE CAR SCHEDULE / JADUAL KERETA PERSENDIRIAN", 200, 40), ("Northwind General Insurance Berhad", 60, 10),
            ("The Insured / Pemegang Polisi", 60, 100), ("AHMAD BIN ALI", 60, 130), ("I.C. No. / No.Kad Pengenalan", 60, 200),
            ("800101145678", 60, 230), ("Registration No. /", 60, 300), ("No.Pendaftaran", 60, 325), ("WXY1234", 60, 355),
            ("Engine/Motor No. / No. Enjin/Motor", 60, 420), ("1NR7654321", 60, 450), ("Chassis No. / No. Casis", 60, 500),
            ("PM2B301S009912345", 60, 530), ("Sum Insured / Jumlah", 420, 300), ("Diinsuranskan (RM)", 420, 325),
            ("26,000.00", 420, 355), ("Policy No. / No. Polisi", 700, 100), ("JVE4495625", 700, 130),
            ("Period of Insurance / Tempoh Insurans", 700, 170), ("From / Dari 00:00:01 AM 31-10-2023 To / Hingga 30-10-2024", 700, 200),
            ("Optional Cover", 700, 420), ("Private Hire Car / Kereta Sewa Persendirian", 700, 445), ("(e-Hailing)", 700, 470)]
    column = [("Premium / Premium", 831.42, 260), ("NCD / Diskaun Tanpa Tuntutan 30.00%", 249.43, 300),
              ("Gross Premium / Premium Kasar", 589.49, 520), ("Service Tax / Cukai Perkhidmatan 6%", 35.37, 560),
              ("Stamp Duty / Duti Setem", 10.00, 600), ("Total Due / Jumlah Berbayar", 634.86, 700)]
    lines = [_at(t, x, y) for t, x, y in left]
    for label, amount, y in column:
        lines += [_at(label, 700, y), _at("%.2f" % amount, 1250, y + shift)]
    return [{"page": 1, "width": 1400, "height": 800, "lines": lines, "text": "\n".join(ln["text"] for ln in lines)}]


def test_the_insurance_schedule_is_read_by_its_labels():
    from datetime import date
    from app import insurance
    out = insurance.parse(_schedule(), today=date(2024, 1, 1))
    f = out["fields"]
    assert (f["policy_number"], f["insured_name"], f["id_number"]) == ("JVE4495625", "AHMAD BIN ALI", "800101145678")
    assert (f["plate_number"], f["engine_number"], f["chassis_number"]) == ("WXY1234", "1NR7654321", "PM2B301S009912345")
    assert (f["period_start"], f["period_end"], f["sum_insured"]) == ("2023-10-31", "2024-10-30", "26000.00")
    assert (f["gross_premium"], f["service_tax"], f["stamp_duty"], f["total_payable"]) == ("589.49", "35.37", "10.00", "634.86")
    assert f["ncd_percent"] == "30.00" and f["insurer_name"] == "Northwind General Insurance Berhad"
    assert out["extra"] == {"e_hailing": True, "expired": False}


def test_amounts_off_their_labels_level_are_found_by_what_they_must_add_up_to():
    from app import insurance
    f = insurance.parse(_schedule(shift=34))["fields"]  # a photo: every amount stands a line lower than its label
    assert (f["gross_premium"], f["service_tax"], f["stamp_duty"], f["total_payable"]) == ("589.49", "35.37", "10.00", "634.86")
    # amounts that cannot be confirmed are left out, not guessed
    page = _schedule(shift=34)
    page[0]["lines"] = [ln for ln in page[0]["lines"] if ln["text"] not in ("35.37", "634.86")]
    f = insurance.parse(page)["fields"]
    assert "service_tax" not in f and "total_payable" not in f


def test_an_insurance_schedule_is_recognised_on_auto_without_the_model(fake_model):
    pages = _schedule()
    out = extract.run(pages, [pages[0]["text"]], "auto")
    assert out["document_type"] == "motor_insurance" and out["read_by"] == "labels" and out["model_calls"] == 0
    assert out["fields"]["total_payable"] == 634.86 and out["fields"]["e_hailing"] is True and out["checks_passed"] is True
    # an invoice that mentions insurance is still an invoice
    other = [{"page": 1, "width": 800, "height": 600, "lines": [_at("INVOICE 123", 10, 10), _at("Insurance premium 100.00", 10, 50)],
              "text": "INVOICE 123\nInsurance premium 100.00"}]
    from app import insurance
    assert insurance.parse(other) is None


# --- the e-hailing vehicle permit and the Puspakom inspection ----------------------------------
PERMIT = [_at("PERMIT KENDERAAN E-HAILING", 600, 70, 50), _at("AGENSI PENGANGKUTAN AWAM DARAT", 600, 190, 40),
          _at("Pemegang Lesen Perniagaan Pengantaraan", 550, 420, 40), _at("NORTHWIND RIDES SDN BHD (202201099999)", 400, 500, 45),
          _at("Nama pemilik kenderaan", 110, 620), _at("AHMAD BIN ALI", 730, 620), _at("No. pendaftaran kenderaan", 110, 673),
          _at("WXY1234", 730, 674), _at("Muatan tempat duduk", 110, 729), _at("5", 730, 729), _at("Jenis kenderaan", 110, 779),
          _at("PERSENDIRIAN", 730, 781), _at("No. rujukan permit (Induk)", 110, 836), _at("TE-WXY1234", 730, 836),
          _at("No. rujukan permit (SUB)", 110, 890), _at("NWR001170", 730, 890), _at("Tarikh mula permit", 110, 944),
          _at("13-Oct-2023", 730, 944), _at("Tarikh sahlaku permit", 110, 996), _at("12-Oct-2024", 730, 998),
          _at("Permit ini merupakan perakuan bahawa kenderaan ini didaftarkan sebagai kenderaan e-hailing", 160, 1100)]


def test_the_e_hailing_vehicle_permit_is_read_by_its_labels():
    from datetime import date
    from app import permits
    out = permits.evp([{"page": 1, "width": 1800, "lines": PERMIT}], today=date(2024, 1, 1))
    f = out["fields"]
    assert (f["permit_type"], f["holder_name"], f["plate_number"]) == ("EVP", "AHMAD BIN ALI", "WXY1234")
    assert (f["permit_number"], f["sub_permit_number"], f["vehicle_type"], f["seating_capacity"]) == \
        ("TE-WXY1234", "NWR001170", "PERSENDIRIAN", "5")
    assert (f["issue_date"], f["expiry_date"]) == ("2023-10-13", "2024-10-12") and out["extra"] == {"expired": False}
    assert (f["operator_name"], f["operator_registration"]) == ("NORTHWIND RIDES SDN BHD", "202201099999")
    assert permits.evp([{"page": 1, "width": 800, "lines": [_at("E-HAILING INSURANCE PREMIUM", 10, 10)]}]) is None


def test_the_inspection_report_and_the_windscreen_slip_are_read():
    from datetime import date
    from app import permits
    report = [_at("LAPORAN PEMERIKSAAN KENDERAAN", 400, 290, 60), _at("PUSPAKOM", 170, 400), _at("NO. KEPUTUSAN: A 3053923", 1260, 446),
              _at("PEMILIK", 166, 508), _at("AHMAD BIN ALI", 383, 506), _at("PEMBAWA:AHMAD BIN ALI", 1019, 499),
              _at("NO PENDAFTARAN", 167, 635), _at("WXY1234", 456, 632), _at("NO. CASIS TRELER #", 1021, 625),
              _at("NO. ENJIN/MOTOR #", 167, 676), _at("S4PHVK6058", 457, 674), _at("BUATAN", 1024, 665), _at("PROTON", 1337, 661),
              _at("NO. CASIS #", 167, 718), _at("PL1BH6STRJG032754", 457, 715), _at("MODEL", 1023, 705), _at("PERSONA 1.6L CVT", 1338, 701),
              _at("TAHUN DIPERBUAT", 1026, 746), _at("2017", 1340, 742), _at("KEUPAYAAN ENJIN/MOTOR 1597", 171, 838),
              _at("TARIKH PEMERIKSAAN AKHIR", 1029, 825), _at("TARIKH PEMERIKSAAN", 1030, 907), _at("24-DEC-2023", 1344, 900),
              _at("JENIS PEMERIKSAAN", 1032, 948), _at("BERKALA SEMULA", 1347, 940), _at("KOD PUSAT", 1032, 988),
              _at("J01 PUSPAKOM JOHOR BAHRU", 1348, 980), _at("KEPUTUSAN: LULUS", 170, 1300)]
    f = permits.inspection([{"page": 1, "width": 1920, "lines": report}])["fields"]
    assert (f["report_number"], f["owner_name"], f["plate_number"]) == ("A 3053923", "AHMAD BIN ALI", "WXY1234")
    assert (f["engine_number"], f["chassis_number"], f["make"], f["model"]) == ("S4PHVK6058", "PL1BH6STRJG032754", "PROTON", "PERSONA 1.6L CVT")
    assert (f["year_made"], f["engine_capacity_cc"], f["inspection_date"]) == ("2017", "1597", "2023-12-24")
    assert (f["inspection_type"], f["centre_name"], f["result"]) == ("BERKALA SEMULA", "J01 PUSPAKOM JOHOR BAHRU", "LULUS")
    slip = [_at("PERAKUAN", 30, 180), _at("PEMERIKSAAN", 220, 70), _at("W031683303", 150, 204), _at("WXY1234", 190, 237), _at("PUSPA", 340, 232),
            _at("SAH SEHINGGA/VALID UNTIL", 96, 285), _at("BERKALA-05-NOV-2024", 65, 311), _at("METER TEKSI-28-0CT-2028", 25, 342),
            _at("RM 50.00", 54, 376), _at("1TRA670040", 166, 391), _at("PN1JW3EMQ03030791", 121, 426), _at("28-OCT-2023", 162, 463)]
    out = permits.inspection([{"page": 1, "width": 480, "lines": slip}], today=date(2024, 1, 1))
    f = out["fields"]
    assert (f["report_number"], f["plate_number"], f["inspection_type"]) == ("W031683303", "WXY1234", "BERKALA")
    assert (f["expiry_date"], f["meter_expiry_date"], f["inspection_date"], f["fee"]) == ("2024-11-05", "2028-10-28", "2023-10-28", "50.00")
    assert (f["engine_number"], f["chassis_number"]) == ("1TRA670040", "PN1JW3EMQ03030791") and out["extra"] == {"expired": False}


def test_permit_and_inspection_are_recognised_on_auto_without_the_model(fake_model):
    pages = [{"page": 1, "width": 1800, "height": 1200, "lines": PERMIT, "text": "\n".join(ln["text"] for ln in PERMIT)}]
    out = extract.run(pages, [pages[0]["text"]], "auto")
    assert out["document_type"] == "driver_permit" and out["read_by"] == "labels" and out["model_calls"] == 0
    assert out["fields"]["expiry_date"] == "2024-10-12" and out["fields"]["seating_capacity"] == 5 and "expired" in out["fields"]


def test_a_page_photographed_upside_down_is_still_read_by_position(fake_model):
    lines = [{**ln, "box": [[1136 - x, 720 - y] for x, y in ln["box"]]} for ln in DRIVING]  # every place turned half a circle
    pages = [{"page": 1, "width": 1136, "height": 720, "lines": lines, "text": "\n".join(ln["text"] for ln in lines)}]
    out = extract.run(pages, [pages[0]["text"]], "auto")
    assert out["document_type"] == "driving_licence" and out["fields"]["holder_name"] == "LIM AH KOW BIN ABDULLAH"
    assert out["fields"]["address"] == "43 JALAN SHAIK MADAR, 11500 AYER ITAM, PULAU PINANG"


def test_a_certificate_of_insurance_without_a_premium_column_is_read():
    from app import insurance
    lines = [_at("Northwind General Insurance Berhad", 60, 10), _at("CERTIFICATE OF INSURANCE / SIJIL INSURANS", 300, 50),
             _at("Certificate No. / No. Sijil", 60, 110), _at("JVE4495625", 60, 140),
             _at("1. Index Mark and Registration Number of Vehicle", 60, 180), _at("WXY1234", 60, 210),
             _at("2. Name of Insured / Nama Pemegang Polisi", 60, 250), _at("AHMAD BIN ALI", 60, 280),
             _at("3. Period of Insurance / Tarikh Insurans", 60, 320), _at("From / Dari 00:00:01 AM 31-10-2023 To / Hingga 30-10-2024", 60, 350)]
    lines += [_at("Clause %d of the certificate, which says who may drive." % n, 60, 400 + 30 * n) for n in range(6)]
    f = insurance.parse([{"page": 1, "width": 1400, "lines": lines}])["fields"]
    assert (f["policy_number"], f["plate_number"], f["insured_name"]) == ("JVE4495625", "WXY1234", "AHMAD BIN ALI")
    assert (f["period_start"], f["period_end"]) == ("2023-10-31", "2024-10-30") and "total_payable" not in f


def test_a_certificate_heading_split_or_cut_is_still_a_heading():
    from app import vehicle
    assert vehicle._is_heading("PEMILIKAN KENDERAAN") and vehicle._is_heading("SPEMILIKAN KENDERAAN")
    assert vehicle._is_heading("PERANUAN PENDAFTARAN KENDERAAN") and vehicle._is_heading("PERAKUAN PENDAFTARAN KENDER")
    assert not vehicle._is_heading("No Perakuan Pendaftaran Kenderaan : A/R") and not vehicle._is_heading("JABATAN PENGANGKUTAN JALAN")


# --- every document is named by its kind, and that is kept in its record ------------------------
def test_a_document_is_named_from_the_reading_itself(fake_model):
    def named(lines):
        return extract.identify([{"page": 1, "width": 1400, "height": 800, "lines": lines}], ["\n".join(ln["text"] for ln in lines)])
    assert named(DRIVING) == "driving_licence" and named(VOCATIONAL) == "vocational_licence" and named(MYKAD) == "mykad"
    assert named(ROAD_TAX) == "road_tax" and named(PERMIT) == "driver_permit" and named(_schedule()[0]["lines"]) == "motor_insurance"
    assert named([_at("TAX INVOICE", 10, 10), _at("Invoice No. 2026-0042, total RM 120.00", 10, 50)]) == "invoice"
    assert named([_at("VISITOR PARKING", 10, 10), _at("PARK AT YOUR OWN RISK", 10, 50)]) is None  # no document this service knows
    assert named([]) is None
    assert extract.label("driving_licence") == "Driving licence" and extract.label(None) is None


def test_every_reading_says_what_the_document_is_and_keeps_it_in_its_record(client, api_key, need_engine):
    png = synth.image_bytes(CERTIFICATE)
    r = client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"external_ref": "driver-17"}, headers=api_key)
    assert r.status_code == 200, r.text
    assert (r.json()["document_type"], r.json()["document_label"]) == ("vehicle_registration", "Vehicle registration card (VOC, geran)")
    other = client.post("/v1/ocr", files={"file": ("b.png", synth.image_bytes(["hello there, this is a note"]), "image/png")},
                        data={"detail": "text"}, headers=api_key).json()
    assert other["document_type"] is None and other["document_label"] is None
    mine = client.get("/v1/documents", headers=api_key).json()["documents"]
    assert [(d["document_type"], d["document_label"], d["how"], d["external_ref"]) for d in mine[:2]] == [
        (None, "Not recognised", "sync", None), ("vehicle_registration", "Vehicle registration card (VOC, geran)", "sync", "driver-17")]
    unnamed = client.get("/v1/documents?document_type=none", headers=api_key).json()["documents"]
    assert unnamed and all(d["document_type"] is None for d in unnamed)
    assert len(client.get("/v1/documents?external_ref=driver-17", headers=api_key).json()["documents"]) == 1
    seen = client.get("/admin/api/documents", headers=ADMIN).json()
    assert any(k["document_type"] == "vehicle_registration" and k["count"] >= 1
               and k["document_label"] == "Vehicle registration card (VOC, geran)" for k in seen["by_type"])
    assert seen["documents"][0]["client"] and "text" not in seen["documents"][0]


def test_a_job_keeps_the_kind_of_its_document(client, api_key, need_engine):
    r = client.post("/v1/jobs", files={"file": ("a.png", synth.image_bytes(CERTIFICATE), "image/png")},
                    data={"external_ref": "car-9"}, headers=api_key)
    assert r.status_code in (200, 201, 202), r.text
    job = wait_for(client, api_key, r.json()["id"])
    assert job["status"] == "completed" and job["document_type"] == "vehicle_registration"
    assert job["result"]["document_label"] == "Vehicle registration card (VOC, geran)"
    rows = client.get("/v1/documents?external_ref=car-9", headers=api_key).json()["documents"]
    assert [(d["how"], d["job_id"], d["document_type"]) for d in rows] == [("job", r.json()["id"], "vehicle_registration")]


def test_custom_fields_do_not_hide_what_the_document_is(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers.append({"plate": "WXY1234"})
    r = extract_image(client, api_key, CERTIFICATE, type="custom", fields='{"plate": "string"}', external_ref="custom-1")
    assert r.status_code == 200, r.text
    body = r.json()
    # document_type stays the way the fields were read; the document itself is named beside it
    assert (body["document_type"], body["identified_type"]) == ("custom", "vehicle_registration")
    assert body["document_label"] == "Vehicle registration card (VOC, geran)" and body["fields"] == {"plate": "WXY1234"}
    rows = client.get("/v1/documents?external_ref=custom-1", headers=api_key).json()["documents"]
    assert [(d["document_type"], d["pages"]) for d in rows] == [("vehicle_registration", 1)]
    # read as what it is, an extraction names it once and adds no second name
    plain = extract_image(client, api_key, CERTIFICATE).json()
    assert plain["document_type"] == "vehicle_registration" and "identified_type" not in plain
    assert plain["document_label"] == "Vehicle registration card (VOC, geran)"


def test_a_file_of_several_documents_gets_a_record_line_for_each(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers += [INVOICE, ORDER]
    pdf = synth.scanned_pdf([INVOICE_LINES, ORDER_LINES])
    r = client.post("/v1/extract", files={"file": ("b.pdf", pdf, "application/pdf")},
                    data={"split": "true", "external_ref": "bundle-7"}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "document_type" not in body and "document_label" not in body  # the file as a whole has no one kind
    assert [(d["document_type"], d["document_label"]) for d in body["documents"]] == [("invoice", "Invoice"), ("order", "Order list")]
    rows = client.get("/v1/documents?external_ref=bundle-7", headers=api_key).json()["documents"]
    assert sorted((d["document_type"], d["pages"]) for d in rows) == [("invoice", 1), ("order", 1)]


def test_one_clients_record_is_not_shown_to_another(client, api_key, need_engine):
    client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(CERTIFICATE), "image/png")},
                data={"external_ref": "only-mine"}, headers=api_key)
    made = client.post("/admin/api/clients", json={"name": "another app"}, headers=ADMIN).json()
    other = {"X-API-Key": made["api_key"]}
    assert client.get("/v1/documents", headers=other).json() == {"documents": []}
    assert client.get("/v1/documents?external_ref=only-mine", headers=other).json() == {"documents": []}
    assert client.get("/v1/documents").status_code in (401, 403) and client.get("/admin/api/documents").status_code == 401
