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
    h = client.get("/health").json()
    assert h["extraction"] is False and h["status"] == "ok"  # plain reading still works
    assert post_invoice(client, api_key, type="passport").json()["error"] == "bad_type"


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
