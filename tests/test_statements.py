"""What other banks' statements taught the reader (found on public sample statements), and exports."""
from app import extract, statement
from tests import synth
from tests.conftest import *  # noqa: F401,F403
from tests.test_extract import fake_model, table_page  # noqa: F401


def keys(got):
    return [(t["date"], t["debit"], t["credit"], t["balance"]) for t in got["transactions"]]


def test_a_serial_number_before_the_date_and_a_column_that_says_which_way():
    page = table_page([
        [("No.", 20), ("ID", 60), ("Value Date", 150), ("Description", 260), ("Cr/Dr", 400), ("Amount", 480, True), ("Balance", 580, True)],
        [("Opening Balance", 260), ("1,000.00", 580, True)],
        [("1", 20), ("S85388609", 60), ("01/01/2024", 150), ("RTGS OUT", 260), ("DR", 400), ("400.00", 480, True), ("600.00", 580, True)],
        [("2", 20), ("S93169172", 60), ("02/01/2024", 150), ("NEFT IN", 260), ("CR", 400), ("150.00", 480, True), ("750.00", 580, True)],
    ])
    got = statement.parse([page])
    assert keys(got) == [("2024-01-01", 400.0, None, 600.0), ("2024-01-02", None, 150.0, 750.0)]
    assert [t["description"] for t in got["transactions"]] == ["RTGS OUT", "NEFT IN"]
    assert extract.broken_balances(got["transactions"], got["opening_balance"]) == []


def test_a_card_statement_with_dates_without_a_year_and_a_summary_box_above():
    page = table_page([
        [("Statement Date 04 APR 25", 30)],
        [("Previous Balance", 30), ("Credit / Payment", 180), ("Debit / Fees", 330), ("Total Balance Due", 580, True)],
        [("15,824.45", 100, True), ("8,169.00", 250, True), ("985.66", 400, True), ("25,281.45", 580, True)],
        [("filler one", 30)], [("filler two", 30)], [("filler three", 30)], [("filler four", 30)],
        [("Transaction Date", 30), ("Transaction Description", 150), ("Transaction Amount", 580, True)],
        [("CREDIT LIMIT RM 98,500.00", 150)],
        [("PREVIOUS BAL", 150), ("4,502.19", 580, True)],
        [("25 MAR", 30), ("PAYMENT REC'D WITH THANKS", 150), ("955.63", 580, True), ("CR", 586)],
        [("04 APR", 30), ("RETAIL INTEREST", 150), ("58.24", 580, True)],
        [("SUB-TOTAL", 150), ("3,604.80", 480, True)],
    ])
    got = statement.parse([page], "dmy", True)
    assert keys(got) == [("2025-03-25", None, 955.63, None), ("2025-04-04", 58.24, None, None)]
    assert got["opening_balance"] == 4502.19


def test_a_watermark_in_front_of_a_date_does_not_lose_the_row():
    rows = [[("Txn Date", 30), ("Description", 150), ("Debit", 400, True), ("Credit", 480, True), ("Balance", 580, True)],
            [("Opening Balance", 150), ("1,000.00", 580, True)]]
    for i, (day, text, debit, balance) in enumerate([("01-01-2024", "RENT", "100.00", "900.00"), ("02-01-2024", "FEES", "50.00", "850.00"),
                                                      ("03-01-2024", "TOLL", "20.00", "830.00"), ("04-01-2024", "FUEL", "30.00", "800.00")]):
        row = [(day, 30), (text, 150), (debit, 400, True), (balance, 580, True)]
        rows.append(([("SAMPLE", 2)] + row) if i == 2 else row)
    got = statement.parse([table_page(rows)])
    assert [k[0] for k in keys(got)] == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
    assert extract.broken_balances(got["transactions"], got["opening_balance"]) == []


def test_a_currency_sign_before_an_amount_is_not_description():
    page = table_page([
        [("Date", 30), ("Description", 150), ("Debit", 420, True), ("Balance", 580, True)],
        [("01-01-2024", 30), ("PAID 12.50 USD TO SHOP", 150), ("Rs.", 370), ("100.00", 420, True), ("Rs.", 530), ("900.00", 580, True)],
    ])
    assert statement.parse([page])["transactions"][0]["description"] == "PAID 12.50 USD TO SHOP"


def test_a_statement_of_several_accounts_says_so(client, api_key, need_engine, fake_model):  # noqa: F811
    answers, _ = fake_model
    answers.append({"bank_name": None, "account_holder": None, "account_number": None, "currency": None,
                    "period_start": None, "period_end": None})
    lines = ["Date        Description        Withdrawal    Deposit    Balance", "Balance Brought Forward        1,000.00",
             "02/01/2026    FEES    10.00        990.00", "Balance Brought Forward        5,000.00",
             "03/01/2026    RENT    500.00        4,500.00"]
    r = client.post("/v1/extract", files={"file": ("m.png", synth.image_bytes(lines), "image/png")},
                    data={"type": "bank_statement"}, headers=api_key)
    body = r.json()
    if body.get("read_by") == "table":  # the picture's columns were found
        one = [c for c in body["checks"] if c["name"] == "one_account"]
        assert one and one[0]["passed"] is False and body["checks_passed"] is False


def test_transactions_in_the_layouts_accounting_systems_import():
    from app.routers.ocr import bank_csv
    result = {"fields": {"transactions": [
        {"date": "2026-01-02", "description": "RENT  JAN", "reference": None, "debit": 1200.0, "credit": None, "balance": 2800.0},
        {"date": "2026-01-05", "description": "SALARY", "reference": "X1", "debit": None, "credit": 3000.5, "balance": 5800.5}]}}
    assert bank_csv(result, "quickbooks-3").splitlines() == ["Date,Description,Amount", "02/01/2026,RENT JAN,-1200.00", "05/01/2026,SALARY,3000.50"]
    assert bank_csv(result, "quickbooks-4").splitlines() == ["Date,Description,Credit,Debit", "02/01/2026,RENT JAN,,1200.00", "05/01/2026,SALARY,3000.50,"]
    assert bank_csv(result, "generic").splitlines()[1] == "02/01/2026,RENT JAN,,1200.00,,2800.00"
