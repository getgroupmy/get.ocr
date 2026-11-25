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


def test_a_statement_of_several_accounts_is_split_and_each_is_checked():
    page = table_page([
        [("Date", 30), ("Description", 150), ("Withdrawal", 400, True), ("Deposit", 480, True), ("Balance", 580, True)],
        [("Savings account", 30)],
        [("Balance Brought Forward", 150), ("1,000.00", 580, True)],
        [("02/01/2026", 30), ("FEES", 150), ("10.00", 400, True), ("990.00", 580, True)],
        [("03/01/2026", 30), ("INTEREST", 150), ("5.00", 480, True), ("995.00", 580, True)],
        [("Closing Balance", 150), ("995.00", 580, True)],
        [("Current account", 30)],
        [("Balance Brought Forward", 150), ("5,000.00", 580, True)],
        [("03/01/2026", 30), ("RENT", 150), ("500.00", 400, True), ("4,500.00", 580, True)],
        [("Closing Balance", 150), ("4,500.00", 580, True)],
    ])
    got = statement.parse([page])
    assert got["accounts"] == 2 and got["by_account"] == [
        {"opening_balance": 1000.0, "closing_balance": 995.0, "first_transaction": 1, "transactions": 2},
        {"opening_balance": 5000.0, "closing_balance": 4500.0, "first_transaction": 3, "transactions": 1}]
    # read as one list the balances cannot follow; account by account they do
    assert extract.broken_balances(got["transactions"], got["opening_balance"]) == [3]
    for a in got["by_account"]:
        part = got["transactions"][a["first_transaction"] - 1:a["first_transaction"] - 1 + a["transactions"]]
        checks = extract.statement_checks({"transactions": part, "opening_balance": a["opening_balance"],
                                           "closing_balance": a["closing_balance"]})
        assert [c["passed"] for c in checks] == [True, True]


def test_transactions_in_the_layouts_accounting_systems_import():
    from app.routers.ocr import bank_csv
    result = {"fields": {"transactions": [
        {"date": "2026-01-02", "description": "RENT  JAN", "reference": None, "debit": 1200.0, "credit": None, "balance": 2800.0},
        {"date": "2026-01-05", "description": "SALARY", "reference": "X1", "debit": None, "credit": 3000.5, "balance": 5800.5}]}}
    assert bank_csv(result, "quickbooks-3").splitlines() == ["Date,Description,Amount", "02/01/2026,RENT JAN,-1200.00", "05/01/2026,SALARY,3000.50"]
    assert bank_csv(result, "quickbooks-4").splitlines() == ["Date,Description,Credit,Debit", "02/01/2026,RENT JAN,,1200.00", "05/01/2026,SALARY,3000.50,"]
    assert bank_csv(result, "generic").splitlines()[1] == "02/01/2026,RENT JAN,,1200.00,,2800.00"


def test_a_dutch_statement_with_european_numbers_and_one_signed_column():
    rows = [[("Balance as of 01.05.2025:", 30), ("15.320,00", 330, True), ("€", 345)],
            [("Balance as of 31.05.2025:", 30), ("15.216,28", 330, True), ("€", 345)],
            [("Total incoming:", 30), ("114,83", 330, True), ("€", 345)],
            [("Total outgoing:", 30), ("218,55", 330, True), ("€", 345)],
            [("Date", 30), ("Interest Date", 90), ("Counterparty", 170), ("Description", 300), ("Amount", 560, True)],
            [("1 mei", 30), ("01.05", 90), ("THOMAS DE GROOT", 170), ("OVERBOEKING", 300), ("114,83", 550, True), ("€", 562)],
            [("NL17THOM6025479805", 170)],
            [("7 mei", 30), ("07.05", 90), ("Jumbo", 170), ("SEPA DEBIT", 300), ("-218,55", 550, True), ("€", 562)]]
    got = statement.parse([table_page(rows)])
    assert [(t["date"], t["debit"], t["credit"]) for t in got["transactions"]] == [
        ("2025-05-01", None, 114.83), ("2025-05-07", 218.55, None)]  # no minus: money in
    assert got["transactions"][0]["description"] == "THOMAS DE GROOT OVERBOEKING NL17THOM6025479805"  # no value date, no sign
    assert got["opening_balance"] == 15320.0 and got["closing_balance"] == 15216.28
    assert got["printed"]["total_credits"] == 114.83 and got["printed"]["total_debits"] == 218.55
    assert [c["passed"] for c in extract.statement_checks(got, got["printed"])] == [None, True, True]


def test_a_french_statement_with_spaced_thousands_and_month_names():
    rows = [[("Solde D'ouverture 1 janvier 2025", 30), ("10", 520, True), ("750,00", 556, True), ("$", 565)],
            [("Total Crédits (1)", 30), ("+", 500), ("1", 520, True), ("010,33", 556, True), ("$", 565)],
            [("Total Débits (1)", 30), ("-", 500), ("151,30", 556, True), ("$", 565)],
            [("Solde De Fermeture 31 janvier 2025", 30), ("=", 500), ("11", 520, True), ("609,03", 556, True), ("$", 565)],
            [("Détails", 30), ("Chèques et débits", 330, True), ("Dépôts et crédits ($)", 450, True), ("Solde ($)", 565, True)],
            [("02 janv. 25", 30), ("SKIP PLATS", 110), ("151,30", 310, True), ("$", 320),
             ("10", 520, True), ("598,70", 556, True), ("$", 565)],
            [("03 févr. 25", 30), ("DEPOT PAIE", 110), ("1", 404, True), ("010,33", 440, True), ("$", 450),
             ("11", 520, True), ("609,03", 556, True), ("$", 565)]]
    got = statement.parse([table_page(rows)])
    assert [(t["date"], t["description"], t["debit"], t["credit"], t["balance"]) for t in got["transactions"]] == [
        ("2025-01-02", "SKIP PLATS", 151.3, None, 10598.7), ("2025-02-03", "DEPOT PAIE", None, 1010.33, 11609.03)]
    assert got["opening_balance"] == 10750.0 and got["closing_balance"] == 11609.03
    assert got["printed"] == {"total_debits": 151.3, "debit_count": 1, "total_credits": 1010.33, "credit_count": 1}
    assert [c["passed"] for c in extract.statement_checks(got, got["printed"])] == [True, True, True]
    assert statement._money("1.250,80") == (1250.8, "") and statement._money("1'250.80") == (1250.8, "")
    assert statement._date("15 août 2025", "dmy", None) == "2025-08-15" and statement._date("3 mrt", "dmy", 2025) == "2025-03-03"


def test_accounts_are_told_apart_by_the_title_over_each_table():
    head = [("Date", 30), ("Transaction Details", 100), ("Deposit", 380, True), ("Withdrawal", 460, True), ("Balance", 560, True)]
    one = [[("HKD Current Account — 817-890692-838", 30)], head,
           [("1 Sep 2025", 30), ("Faster payment", 100), ("153.01", 460, True), ("42,426.99", 560, True)]]
    two = [[("HKD Current Account — 817-890692-838 (Continued)", 30)], head,
           [("2 Sep 2025", 30), ("Fps payment", 100), ("1,932.00", 460, True), ("40,494.99", 560, True)],
           [("USD Savings Account — 817-890692-002", 30)], head,
           [("20 Sep 2025", 30), ("Fps transfer", 100), ("181.04", 460, True), ("149,818.96", 560, True)],
           [("21 Sep 2025", 30), ("Interest", 100), ("1.04", 380, True), ("149,820.00", 560, True)]]
    got = statement.parse([table_page(one), table_page(two, 2)])
    assert got["accounts"] == 2 and [(a["account_number"], a["name"], a["first_transaction"], a["transactions"]) for a in got["by_account"]] == [
        ("817-890692-838", "HKD Current Account", 1, 2), ("817-890692-002", "USD Savings Account", 3, 2)]
    # one account number over every page is one account
    same = statement.parse([table_page(one), table_page(two[:3], 2)])
    assert same["accounts"] == 1 and same["by_account"] == []


def test_a_card_statement_with_us_dates_a_minus_for_payments_and_note_rows():
    rows = [[("Previous Balance", 30), ("1,847.32", 560, True)],
            [("New Balance", 30), ("2,370.36", 560, True)],
            [("Opening/Closing Date 09/01/2025 - 09/30/2025", 30)],
            [("Post Date", 30), ("Trans Date", 130), ("Merchant Name or Transaction Description", 230), ("$ Amount", 560, True)],
            [("09/01", 30), ("09/01", 130), ("UBER TRIP", 230), ("531.56", 560, True)],
            [("Chevron station #0441 premium", 230), ("0.00", 560, True)],  # a note under the line, with a zero beside it
            [("09/12", 30), ("09/11", 130), ("REWARDS REDEMPTION", 230), ("-13.84", 560, True)],
            [("09/13", 30), ("09/13", 130), ("STARBUCKS COFFEE", 230), ("5.32", 560, True)]]
    got = statement.parse([table_page(rows)], "dmy", owed=True)
    # 09/13 can only be month/day, so the whole page is; a minus on a card is a payment
    assert [(t["date"], t["debit"], t["credit"]) for t in got["transactions"]] == [
        ("2025-09-01", 531.56, None), ("2025-09-12", None, 13.84), ("2025-09-13", 5.32, None)]
    assert got["transactions"][0]["description"] == "UBER TRIP Chevron station #0441 premium"
    checks = extract.statement_checks(got, got["printed"], owed=True)
    assert checks[1]["name"] == "balance_reconciles" and checks[1]["passed"] is True


def test_a_currency_glued_to_an_amount_is_still_an_amount():
    assert statement._money("Rs.19,429.50") == (19429.5, "") and statement._money("S$1,250.00") == (1250.0, "")
    assert statement._money("HK$88.00") == (88.0, "") and statement._money("Rs.") is None


def test_a_rebate_brought_forward_is_not_a_second_account():
    rows = [[("Date", 30), ("Description", 100), ("Withdrawal", 380, True), ("Deposit", 460, True), ("Balance", 560, True)],
            [("Balance from previous statement", 100), ("500.00", 560, True)],
            [("03-01-2026", 30), ("CARD PAYMENT", 100), ("60.00", 380, True), ("440.00", 560, True)],
            [("04-01-2026", 30), ("REFUND", 100), ("10.00", 460, True), ("450.00", 560, True)],
            [("Rebates Brought Forward", 30), ("100.00", 300, True)]]
    got = statement.parse([table_page(rows)])
    assert got["accounts"] == 1 and got["by_account"] == [] and len(got["transactions"]) == 2
