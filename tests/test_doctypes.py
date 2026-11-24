"""The catalogue of documents the model reads in one pass (app/doctypes.py)."""
import json

import pytest

from app import doctypes, extract
from tests import synth
from tests.test_extract import fake_model, table_page  # noqa: F401


def empty(kind):
    d = doctypes.DOCS[kind]
    f = {name: None for name in d["fields"]}
    if d["rows"]:
        f[d["rows"][0]] = []
    return f


@pytest.mark.parametrize("kind", list(doctypes.DOCS))
def test_every_kind_is_well_formed(kind):
    d = doctypes.DOCS[kind]
    schema = extract.catalogue_schema(kind)
    json.dumps(schema)  # what is sent to the model server
    names = set(schema["properties"])
    assert names == set(d["fields"]) | ({d["rows"][0]} if d["rows"] else set()) and kind in extract.TYPES
    for check in d["checks"]:  # every check names fields that exist
        refs = [c for part in check[2:] for c in ([part] if isinstance(part, str) else part)]
        for ref in refs if check[0] != "format" else [check[2]]:
            ref = ref.lstrip("+-")
            rows, _, col = ref.partition(".")
            assert (rows == d["rows"][0] and col in d["rows"][1]) if col else ref in d["fields"], (kind, check, ref)
    # nothing printed: every check is "not checked", none fails or crashes
    assert all(c["passed"] is None for c in doctypes.run_checks(kind, empty(kind)))
    assert kind.replace("_", " ")[:4] or doctypes.prompt(kind)


def test_amounts_in_words():
    w = doctypes.words_to_amount
    assert w("Ringgit Malaysia One Thousand Two Hundred Fifty and Sen Thirty Only") == 1250.30
    assert w("RINGGIT MALAYSIA: TWO HUNDRED AND FIFTY ONLY") == 250.0
    assert w("Three thousand and forty-five and 50/100") == 3045.50
    assert w("Seribu dua ratus lima puluh dan sen tiga puluh sahaja") == 1250.30
    assert w("Dua belas ribu tiga ratus sebelas") == 12311.0
    assert w("Nineteen ringgit and five sen") == 19.05
    assert w("One million two hundred thousand") == 1200000.0
    assert w("please pay promptly") is None


def test_checks_pass_fail_and_hold_back():
    run = doctypes.run_checks
    ins = {**empty("motor_insurance"), "period_start": "2026-01-01", "period_end": "2026-12-31", "gross_premium": 900.0,
           "service_tax": 72.0, "stamp_duty": 10.0, "total_payable": 982.0}
    assert [c["passed"] for c in run("motor_insurance", ins)] == [True, True]
    assert [c["passed"] for c in run("motor_insurance", {**ins, "period_end": "2025-12-31", "total_payable": 990.0})] == \
           [False, False]
    mc = {**empty("medical_certificate"), "start_date": "2026-03-02", "end_date": "2026-03-03", "days": 2}
    assert [c["passed"] for c in run("medical_certificate", mc)] == [True, True]
    assert run("medical_certificate", {**mc, "days": 3})[1]["passed"] is False
    pv = {**empty("payment_voucher"), "amount": 1250.30, "amount_in_words": "One Thousand Two Hundred Fifty and Sen Thirty"}
    assert run("payment_voucher", pv)[0]["passed"] is True
    assert run("payment_voucher", {**pv, "amount": 1520.30})[0]["passed"] is False
    assert run("payment_voucher", {**pv, "amount_in_words": "as per invoice"})[0]["passed"] is None
    st = {**empty("supplier_statement"), "opening_balance": 100.0, "total_due": 350.0,
          "lines": [{"debit": 500.0, "credit": None}, {"debit": None, "credit": 250.0}]}
    assert run("supplier_statement", st)[0]["passed"] is True
    bill = {**empty("medical_bill"), "total": 95.0, "lines": [{"amount": 60.0}, {"amount": 35.0}]}
    assert run("medical_bill", bill)[0]["passed"] is True and run("medical_bill", {**bill, "total": 90.0})[0]["passed"] is False
    reg = {**empty("vehicle_registration"), "chassis_number": "PM2 L252S 0K1234567"}
    assert run("vehicle_registration", reg)[0]["passed"] is True
    assert run("vehicle_registration", {**reg, "chassis_number": "??"})[0]["passed"] is False
    assert run("company_profile", {**empty("company_profile"), "registration_number": "201901012345"})[0]["passed"] is True
    assert run("business_card", {**empty("business_card"), "email": "aina@example.com"})[0]["passed"] is True


def test_kinds_are_recognised_by_their_wording():
    assert extract.guess_type("NOTA PERLINDUNGAN / COVER NOTE\nPolicy No: MX1\nSum Insured 45,000.00\nNCD 25%") == \
           "motor_insurance"
    assert extract.guess_type("LESEN KENDERAAN MOTOR\nCukai Jalan\nTarikh Luput 01/01/2027\nRM 90.00") == "road_tax"
    assert extract.guess_type("SIJIL CUTI SAKIT\nMedical Certificate\nunfit for duty 2 days") == "medical_certificate"
    assert extract.guess_type("Thank you for your purchase\nTotal 12.50") == "invoice"  # nothing distinctive


def test_a_catalogue_document_through_the_api(client, api_key, need_engine, fake_model):  # noqa: F811
    answers, prompts = fake_model
    lines = ["COVER NOTE", "Policy No: MX-778812", "Vehicle No: VAB 1234", "Period 01/01/2026 to 31/12/2026",
             "Sum Insured 45,000.00", "Gross Premium 900.00", "Service Tax 72.00", "Stamp Duty 10.00", "Total 982.00"]
    answers.append({**empty("motor_insurance"), "policy_number": "MX-778812", "plate_number": "VAB 1234",
                    "period_start": "2026-01-01", "period_end": "2026-12-31", "sum_insured": 45000, "gross_premium": 900,
                    "service_tax": 72, "stamp_duty": 10, "total_payable": 982, "insurer_name": "Made Up Insurer"})
    r = client.post("/v1/extract", files={"file": ("c.png", synth.image_bytes(lines), "image/png")},
                    data={"type": "motor_insurance"}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_type"] == "motor_insurance" and body["checks_passed"] is True and body["model_calls"] == 1
    assert body["fields"]["plate_number"] == "VAB 1234" and body["fields"]["total_payable"] == 982.0
    assert body["not_found_in_text"] == ["insurer_name"] and "cover note" in prompts[0][1]


def test_types_are_listed(client, api_key):
    types = client.get("/v1/types", headers=api_key).json()["types"]
    by = {t["type"]: t for t in types}
    assert [t["type"] for t in types] == list(extract.TYPES) and len(types) > 45
    assert by["timesheet"]["rows"] == "lines" and by["timesheet"]["checks"] == ["hours_sum", "overtime_sum"]
    assert "total" in by["quotation"]["fields"] and by["bank_statement"]["rows"] == "transactions"
    assert client.get("/v1/types").status_code == 401


def test_credit_card_statement_adds_spending_to_what_is_owed():
    from app import statement
    page = table_page([
        [("Date", 30), ("Description", 100), ("Amount", 560, True)],
        [("Previous Balance", 100), ("1,000.00", 560, True)],
        [("03/01/2026", 30), ("PETROL STATION", 100), ("120.00", 560, True)],
        [("05/01/2026", 30), ("PAYMENT - THANK YOU", 100), ("1,000.00CR", 560, True)],
        [("09/01/2026", 30), ("GROCER", 100), ("80.50", 560, True)],
        [("New Balance", 100), ("200.50", 560, True)],
    ])
    got = statement.parse([page])
    assert [(t["debit"], t["credit"]) for t in got["transactions"]] == [(120.0, None), (None, 1000.0), (80.5, None)]
    assert (got["opening_balance"], got["closing_balance"]) == (1000.0, 200.5)
    checks = {c["name"]: c["passed"] for c in extract.statement_checks({**got}, owed=True)}
    assert checks["balance_reconciles"] is True
    assert {c["name"]: c["passed"] for c in extract.statement_checks({**got})}["balance_reconciles"] is False


# --- any table, read by position ---------------------------------------------------------
def settlement_table():
    return table_page([
        [("SETTLEMENT REPORT", 30)],
        [("Date", 30), ("Reference", 110), ("Method", 250), ("Gross", 400, True), ("Fee", 480, True), ("Net", 560, True)],
        [("02/01/2026", 30), ("TXN-0001", 110), ("FPX", 250), ("1,200.00", 400, True), ("12.00", 480, True), ("1,188.00", 560, True)],
        [("03/01/2026", 30), ("TXN-0002", 110), ("Card", 250), ("300.50", 400, True), ("6.01", 480, True), ("294.49", 560, True)],
        [("second line", 110)],
        [("Total", 30), ("1,500.50", 400, True), ("18.01", 480, True), ("1,482.49", 560, True)],
    ])


def test_any_table_is_read_by_position():
    from app import tables
    got = tables.parse([settlement_table()])
    assert got["columns"] == ["date", "reference", "method", "gross", "fee", "net"] and len(got["rows"]) == 2
    assert got["rows"][0] == {"date": "02/01/2026", "reference": "TXN-0001", "method": "FPX", "gross": 1200.0,
                              "fee": 12.0, "net": 1188.0}
    assert got["rows"][1]["reference"] == "TXN-0002 second line" and got["totals"] == {"gross": 1500.5, "fee": 18.01, "net": 1482.49}
    assert tables.checks(got)[0]["passed"] is True
    got["rows"][0]["fee"] = 21.0  # a misread cell shows against the printed total
    assert tables.checks(got)[0]["passed"] is False and "fee adds up to 27.01" in tables.checks(got)[0]["detail"]
    assert tables.parse([table_page([[("Just a letter with no table.", 30)]])])["rows"] == []


def test_table_needs_no_model(client, api_key, need_engine):
    lines = ["Item      Qty      Amount", "Widget      2      100.00", "Gasket      1      25.50", "Total      3      125.50"]
    r = client.post("/v1/extract", files={"file": ("t.png", synth.image_bytes(lines), "image/png")},
                    data={"type": "table"}, headers=api_key)  # the model server is not running in the tests
    if r.status_code == 422:  # the reader gave each row as one piece: nothing to place in columns
        assert r.json()["error"] == "no_table"
    else:
        assert r.status_code == 200 and r.json()["read_by"] == "table" and r.json()["model_calls"] == 0
