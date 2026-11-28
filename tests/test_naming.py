"""A document's name: given only on what a page itself shows, kept in a record of its own, and
never what a plain job's announcement is routed by. Every paper here is made up."""
import time
from datetime import timedelta

from app import engine, extract, insurance, licence, mykad, permits, roadtax, service, vehicle
from app.config import get_settings
from app.db import DocumentRecord, session_scope, utcnow
from tests import synth
from tests.conftest import ADMIN
from tests.test_api import wait_for
from tests.test_extract import (CERTIFICATE, DRIVING, INVOICE_LINES, MYKAD, PERMIT, ROAD_TAX, VOCATIONAL, _at, _schedule,
                                extract_image, fake_model, fake_vision)  # noqa: F401 - the last two are fixtures


def cells(table, widths=(50, 560, 1000)):
    """Positioned lines of a form: a str is a line of its own, a tuple a label with its value(s) in cells beside it."""
    return [_at(c, x, 50 + 60 * n) for n, row in enumerate(table)
            for x, c in zip(widths, [row] if isinstance(row, str) else row) if c]


def page(lines, n=1, w=1400, h=1200):
    return {"page": n, "width": w, "height": h, "lines": lines, "text": "\n".join(ln["text"] for ln in lines)}


def named(*pages_of_lines):
    pages = [page(lines, n + 1) for n, lines in enumerate(pages_of_lines)]
    return extract.identify(pages, [engine.layout_columns(p["lines"]) for p in pages])


# --- a paper named by its heading ---------------------------------------------------------------
def test_a_heading_names_a_sales_paper_and_the_papers_it_quotes_do_not():
    assert named(cells(["ACME SUPPLIES SDN BHD", "TAX INVOICE", "Invoice No: INV-17", "Delivery Order No: DO-5521",
                        "Your Purchase Order: PO-7781", "Quotation Ref: Q-118", "Total RM 100.00"])) == "invoice"
    assert named(cells(["DELIVERY ORDER", "D/O No: DO-5521", "Invoice No: INV-17"])) == "delivery_order"
    assert named(cells(["CREDIT NOTE", "Invoice No: INV-17", "Total RM 20.00"])) == "credit_note"
    assert named(cells(["QUOTATION", "Quotation No: Q-118",
                        "Delivery within 14 days after your Purchase Order is confirmed."])) == "quotation"
    assert named(cells(["DELTA KARGO SDN BHD", "PURCHASE ORDER", "P.O. No: PO-7781",
                        "Please quote this number on your delivery order and invoice."])) == "purchase_order"
    assert named(cells(["NORTHWIND AUTO SERVICE", "INVOIS CUKAI / TAX INVOICE", "No. Invois: SV-0042",
                        "Cukai Jualan / Sales Tax 10% 18.00", "Jumlah / Total 202.80"])) == "invoice"  # the heading in two languages
    assert named(cells(["PETRONAS DAGANGAN BERHAD", "RECEIPT 004512", "RON95 25.32 LITRE", "TOTAL RM 52.00"])) == "receipt"
    assert named(cells(["DELTA KARGO SDN BHD", "PAYSLIP January 2026", "Basic Salary 3,000.00"])) == "payslip"
    assert named(cells(["** Order List **", "Table No: 01", "1 Thai Iced Tea"])) == "order"


def test_a_sales_paper_without_a_heading_is_known_by_its_own_number():
    assert named(cells(INVOICE_LINES)) == "invoice"  # "Invoice No: INV-2026-00417" and no heading
    assert named(cells(INVOICE_LINES[:3] + ["Purchase Order No: PO-7781"] + INVOICE_LINES[3:])) == "invoice"
    assert named(cells(["NORTHWIND TRADING", ("No. Resit", ": 0042"), "Jumlah RM 20.00"])) == "receipt"
    # the same words over a column of a statement carry no number of their own
    assert named(cells(["ACME SUPPLIES SDN BHD", "STATEMENT OF ACCOUNT", "To: DELTA KARGO SDN BHD",
                        ("Invoice No", "Date", "Amount"), ("INV-2026-00417", "03/03/2026", "135.54"), "Balance B/F 500.00"])) is None


def test_a_word_in_a_sentence_or_a_label_names_nothing():
    assert named(cells(["Dear Sir,", "We acknowledge receipt of your letter dated 3 April 2026."])) is None
    assert named(cells(["MEMO", "Please hand every supplier invoice to the accounts department by Friday."])) is None
    assert named(cells(["VISITOR PARKING", "PARK AT YOUR OWN RISK"])) is None
    # words alone, which only choose how a file sent for extraction is read, give no name
    assert named(cells(["TENAGA ELEKTRIK BERHAD", "Bil Elektrik", "No. Akaun: 220012345678", "Due Date: 03/05/2026",
                        "Amount Due RM 135.54", "Meter 004512 kWh 320"])) is None
    assert named(cells(["BORANG EA", "PENYATA SARAAN DARIPADA PENGGAJIAN SWASTA", "Caruman KWSP 4,620.00",
                        "Caruman PERKESO 236.40", "Potongan Cukai Bulanan (PCB) 1,200.00"])) is None
    assert named(cells(["NORTHWIND CLINIC", "Appointment card", "Dr Tan", "Next visit 12/04/2026"])) is None


def test_a_bank_statement_is_a_banks_paper_of_an_account():
    top = ["NORTHWIND BANK BERHAD", "PENYATA AKAUN", "Akaun Simpanan 1640-1234", "Baki Awal 1,000.00", "Baki Akhir 2,800.00"]
    assert named(cells(top)) == "bank_statement"
    assert named(cells(["PRIMEBIZ CURRENT ACCOUNT", "Statement Period : 01/01/26 - 31/01/26",
                        "Balance from previous statement 500.00"])) == "bank_statement"  # no bank named, three of its marks
    assert named(cells(["ACME SUPPLIES SDN BHD", "STATEMENT OF ACCOUNT", "Balance B/F 500.00", "Total Due 135.54"])) is None
    assert named(cells(["NORTHWIND BANK BERHAD", "PENYATA AKAUN SEWA BELI", "Baki Awal 12,000.00", "Baki Akhir 11,500.00"])) is None
    assert named(cells(["NORTHWIND BANK BERHAD", "CREDIT CARD STATEMENT", "Opening Balance 100.00", "Closing Balance 200.00"])) is None


# --- a form named as the form it is, and nothing that merely mentions it --------------------------
def test_the_forms_are_named_and_the_papers_around_them_are_not():
    assert [named(f) for f in (DRIVING, VOCATIONAL, MYKAD, ROAD_TAX, PERMIT)] == \
        ["driving_licence", "vocational_licence", "mykad", "road_tax", "driver_permit"]
    assert named(_schedule()[0]["lines"]) == "motor_insurance"
    lookalikes = {
        "a car rental agreement": ["NORTHWIND CAR RENTAL SDN BHD", "CAR RENTAL AGREEMENT", ("Name of hirer", "AHMAD BIN ALI"),
                                   ("Driving Licence No.", "800101145678"), ("Registration No.", "WXY 1234"),
                                   ("Rate per day", "RM 120.00"), "Signature of hirer"],
        "a police report": ["POLIS DIRAJA MALAYSIA", "LAPORAN POLIS", ("No. Repot", "PJ/001234/26"),
                            ("Nama Pengadu", "AHMAD BIN ALI"), ("No. Lesen Memandu", "800101145678"),
                            ("No. Kenderaan", "WXY 1234"), "Tandatangan Pengadu"],
        "an operator's letter about the licence": ["NORTHWIND RIDES SDN BHD", "Dear AHMAD BIN ALI",
                                                   "RE: VOCATIONAL LICENCE (PSV) REQUIREMENT", "NRIC 800101145678",
                                                   "Please upload it before 1 April 2026."],
        "a form with a section about the licence": ["BORANG PERMOHONAN PEMANDU", "LESEN MEMANDU", ("No. Kad Pengenalan", "800101-14-5678"),
                                                    ("Kelas", "D"), ("Alamat", "12 JALAN MERANTI 3")],
        "a season pass": ["VISITOR PARKING", "WXY 1234", "VALID UNTIL 31 DEC 2026"],
        "a gift voucher": ["NORTHWIND PETROLEUM", "GIFT VOUCHER", "RM 50", "VALID UNTIL 31 DEC 2026"],
        "a parking compound": ["MAJLIS BANDARAYA PETALING JAYA", "NOTIS KESALAHAN SERTA TAWARAN KOMPAUN",
                               ("No. Kenderaan", "WXY 1234"), ("Kadar Kompaun", "RM 30.00"),
                               "Tawaran kompaun ini sah sehingga 26/03/2026"],
        "an SSM certificate of registration": ["SURUHANJAYA SYARIKAT MALAYSIA", "BORANG D", "PERAKUAN PENDAFTARAN",
                                               "AKTA PENDAFTARAN PERNIAGAAN 1956", "NORTHWIND RIDES ENTERPRISE",
                                               "No. Pendaftaran : 202203123456 (003123456-X)", "PENDAFTAR PERNIAGAAN"],
        "a bank's release letter": ["NORTHWIND BANK BERHAD", "SURAT PELEPASAN TUNTUTAN HAK MILIK",
                                    ("Nama Pemunya Berdaftar", ": AHMAD BIN ALI"), ("No. Pendaftaran Kenderaan", ": WXY 1234"),
                                    ("No. Casis", ": PM2B301S009912345"), ("No. Enjin", ": 1NR7654"),
                                    ("Buatan / Model", ": PERODUA MYVI 1.5")],
        "a summons for an expired road tax": ["JABATAN PENGANGKUTAN JALAN MALAYSIA", "NOTIS SAMAN", ("No. Saman", "AA12345678"),
                                              ("No. Kenderaan", "WXY 1234"), "Lesen Kenderaan Motor tamat tempoh",
                                              ("Kadar Kompaun", "RM 150.00")],
        "a letter about applying for the permit": ["NORTHWIND RIDES SDN BHD", "Kepada: AHMAD BIN ALI",
                                                   "PERMOHONAN PERMIT KENDERAAN E-HAILING (EVP)",
                                                   ("No. Pendaftaran Kenderaan", ": WXY 1234"), ("Status", ": DALAM PROSES")],
        "a SIM registration slip": ["NORTHWIND MOBILE", "PREPAID REGISTRATION", ("Name", "AHMAD BIN ALI"), ("ID Type", "MyKad"),
                                    ("ID No.", "800101145678"), ("Mobile No.", "012-345 6789")],
        "a membership form": ["BORANG KEAHLIAN", ("No. Kad Pengenalan", ""), ("Warganegara", ""), ("Jantina", "LELAKI / PEREMPUAN")],
        "a houseowner's schedule": ["NORTHWIND GENERAL INSURANCE BERHAD", "HOUSEOWNER POLICY SCHEDULE", ("Policy No.", "FHO2026001234"),
                                    ("The Insured", "AHMAD BIN ALI"), ("Risk address", "12 JALAN MERANTI 3"),
                                    ("Period of Insurance", "From 01/04/2026 To 31/03/2027"), ("Building Sum Insured", "350,000.00"),
                                    ("Premium", "420.00"), ("Service Tax 8%", "33.60"), ("Stamp Duty", "10.00"),
                                    ("Total Premium", "463.60"), "Subject to the policy wording"],
        "a motor claim form": ["NORTHWIND GENERAL INSURANCE BERHAD", "MOTOR ACCIDENT CLAIM FORM", ("Policy / Cover Note No.", "JVE4495625"),
                               ("Claim No.", "CL/2026/00123"), ("Name of Insured", "AHMAD BIN ALI"),
                               ("Vehicle Registration No.", "WXY 1234"), ("Date of Accident", "12/03/2026"),
                               ("Place of Accident", "JALAN SS 2/24"), ("Chassis No.", "PM2B301S009912345"),
                               ("Engine No.", "1NR7654"), "Signature of insured", "For office use"],
    }
    wrong = {what: named(cells(lines)) for what, lines in lookalikes.items()}
    assert {what: kind for what, kind in wrong.items() if kind is not None} == {}


def test_each_readers_own_test_of_a_page():
    assert vehicle.is_certificate(cells(CERTIFICATE)) and not vehicle.is_certificate(cells(["No. Pendaftaran Kenderaan : WXY 1234"]))
    assert licence.card_kind(DRIVING) == "driving_licence" and licence.card_kind(VOCATIONAL) == "vocational_licence"
    assert mykad.is_card(MYKAD) and roadtax.is_disc(ROAD_TAX) and permits.is_permit(PERMIT)
    assert insurance.is_motor(_schedule()[0]["lines"]) and not insurance.is_endorsement(_schedule()[0]["lines"])
    # a heading with a letter misread or its end cut off is still the heading; a label that holds the words is not
    half = [ln for ln in DRIVING if ln["text"] not in ("LESEN MEMANDU", "DRIVING LICENCE")] + [_at("LESEN MEMAN", 240, 80), _at("DRIVING LICE", 240, 110)]
    assert licence.card_kind(half) == "driving_licence"
    from app import headings
    assert headings.is_title("SPEMILIKAN KENDERAAN", vehicle.TITLES) and headings.is_title("PERAKUAN PENDAFTARAN KENDER", vehicle.TITLES)
    assert not any(headings.is_title(t, vehicle.TITLES) for t in ("PERAKUAN PENDAFTARAN", "No. Pendaftaran Kenderaan",
                                                                 "PEMILIK KENDERAAN", "Pendaftaran Kenderaan", "SIJIL PEMILIKAN"))
    assert not any(headings.is_title(t, licence.DRIVING_TITLES) for t in ("Driving Licence No.", "No. Lesen Memandu",
                                                                          "Kelas Lesen Memandu", "holds a valid driving licence"))


def test_a_disc_in_an_insurers_holder_and_the_licence_on_a_screen_are_road_tax():
    holder = ROAD_TAX + [_at("Northwind General Insurance Berhad", 200, 900), _at("INSURANCE PROGRAM", 200, 940)]
    assert named(holder) == "road_tax"  # the holder's print does not make the disc another paper
    screen = cells(["Semakan Lesen", "Kenderaan Motor (LKM)", "Keputusan Carian", ("Nama", "AHMAD BIN ALI"),
                    ("No Kenderaan", "WXY1234"), "NORTHWIND TAKAFUL AM BHD", ("Tarikh Mula", "20/11/2025"),
                    ("Tarikh Luput", "19/11/2026"), "WXY1234", ("Tarikh Mula", "20/11/2025"), ("Tarikh Luput", "19/11/2026")])
    assert named(screen) == "road_tax"
    f = roadtax.parse([page(screen)])["fields"]
    assert (f["plate_number"], f["period_start"], f["expiry_date"]) == ("WXY1234", "2025-11-20", "2026-11-19")


def test_an_endorsement_is_not_a_schedule():
    endorsement = cells(["NORTHWIND GENERAL INSURANCE BERHAD", "MOTOR ENDORSEMENT SCHEDULE", ("Endorsement No.", "E-001"),
                         ("Policy No.", "JVE4495625"), ("Vehicle No.", "WXY 1234"), ("Chassis No.", "PM2B301S009912345"),
                         ("Additional Premium", "50.00"), ("Service Tax", "4.00"), ("Total", "54.00"),
                         "It is hereby declared and agreed that the policy is extended.", "Subject otherwise to the terms.",
                         "Authorised signature"])
    assert named(endorsement) == "insurance_endorsement"
    # a schedule that lists which endorsements apply, under a column headed with the word, stays a schedule
    listed = _schedule()[0]["lines"] + [_at("ENDORSEMENT", 60, 760), _at("89, 87 (AGREED VALUE)", 60, 790)]
    assert named(listed) == "motor_insurance"


def test_the_first_page_that_says_what_it_is_names_a_file():
    schedule, certificate = _schedule()[0]["lines"], cells(CERTIFICATE)
    assert named(schedule, certificate) == "motor_insurance" and named(certificate, schedule) == "vehicle_registration"
    assert named(PERMIT, ROAD_TAX) == "driver_permit" and named(MYKAD, DRIVING) == "mykad"
    assert named(cells(["a cover page with nothing on it"]), DRIVING) == "driving_licence"
    assert named(cells(["one"]), cells(["two"]), cells(["three"]), DRIVING) is None  # a document says what it is on its first pages


def test_auto_extraction_does_not_take_a_look_alike_for_a_form(fake_model):
    answers, prompts = fake_model
    answers.append({})
    ssm = page(cells(["SURUHANJAYA SYARIKAT MALAYSIA", "BORANG D", "PERAKUAN PENDAFTARAN", "AKTA PENDAFTARAN PERNIAGAAN 1956",
                      "NORTHWIND RIDES ENTERPRISE", "No. Pendaftaran : 202203123456 (003123456-X)", "PENDAFTAR PERNIAGAAN"]))
    out = extract.run([ssm], [engine.layout_columns(ssm["lines"])], "auto")
    assert out["document_type"] != "vehicle_registration" and out["read_by"] == "model" and len(prompts) == 1
    # sent as the form by the caller, the reader takes it on its looser test, as before
    cert = page(cells(CERTIFICATE))
    forced = extract.run([cert], [engine.layout_columns(cert["lines"])], "vehicle_registration")
    assert forced["read_by"] == "labels" and forced["fields"]["plate_number"] == "WXY1234"


def test_text_without_places_is_read_by_its_words_unless_lines_that_were_looked_at_say_otherwise(fake_model):
    answers, prompts = fake_model

    def bare(text):  # a page as a vision model gives it: text, no lines
        return {"page": 1, "width": 1000, "height": 700, "lines": [], "text": text}

    card = "\n".join(ln["text"] for ln in DRIVING)
    assert all(read([bare(card)]) is None for read in extract.FORMS.values())  # no reader by position can take it
    answers.append({})
    out = extract.run([bare(card)], [card], "auto")  # nothing to tell the form by: the words stand, the model takes the fields
    assert (out["document_type"], out["read_by"]) == ("driving_licence", "model")
    answers.append({})
    out = extract.run([bare(card)], [card], "auto", look=[page(DRIVING)])  # the standard reader's lines show the card
    assert (out["document_type"], out["read_by"]) == ("driving_licence", "model")
    asks = ["BORANG PERMOHONAN PEMANDU", "Nama: Aina binti Razak", "No. Lesen Memandu: 900101145677", "Kelas Lesen Memandu: D",
            "Tarikh Tamat Lesen Memandu: 01/01/2029", "Sila lampirkan salinan Lesen Memandu / Driving Licence."]
    text = "\n".join(asks)
    assert extract.guess_type(text) == "driving_licence"  # by its words alone, a form that asks about a licence is one
    answers.append({**{k: None for k in extract.SALE_SCHEMA["properties"]}, "document_type": "other", "line_items": []})
    out = extract.run([bare(text)], [text], "auto", look=[page(cells(asks))])  # the lines show it is not the card
    assert out["document_type"] == "other" and len(prompts) == 3


# --- the name in the answers ---------------------------------------------------------------------
def test_a_page_a_vision_model_read_is_named_from_the_standard_readers_lines(client, api_key, need_engine, fake_vision,  # noqa: F811
                                                                             fake_model):  # noqa: F811
    png = synth.image_bytes(CERTIFICATE)  # the stand-in vision model writes two lines of a letter, whatever it is shown
    body = client.post("/v1/ocr", files={"file": ("c.png", png, "image/png")}, data={"engine": "vision"}, headers=api_key).json()
    assert body["document_type"] == "vehicle_registration" and "naming_pages" not in body
    assert body["pages"][0]["source"] == "vision" and body["pages"][0]["lines"] == []  # its reading still has no places
    # its fields: the form is told by the same lines, and the model takes the values from the vision reader's text
    answers, prompts = fake_model
    answers.append({})
    body = client.post("/v1/extract", files={"file": ("c.png", png, "image/png")}, data={"engine": "vision"}, headers=api_key).json()
    assert (body["document_type"], body["read_by"], body["model_calls"]) == ("vehicle_registration", "model", 1)
    assert "identified_type" not in body and "naming_pages" not in body and "registration certificate" in prompts[0][1]


def test_a_pdf_with_stored_text_is_named_the_same_whatever_detail_is_asked_for(client, api_key, need_engine):
    scale, ops = 595 / 1800, []
    for ln in PERMIT:  # the permit as a PDF that carries its own text
        (x, y), (_, bottom) = ln["box"][0], ln["box"][2]
        size = max((bottom - y) * scale * 0.9, 5)
        safe = ln["text"].replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append("BT /F1 %.1f Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET" % (size, x * scale, 842 - y * scale - size, safe))
    pdf = synth._pdf(["\n".join(ops).encode("latin-1")])
    full, brief = (client.post("/v1/ocr", files={"file": ("permit.pdf", pdf, "application/pdf")}, data={"detail": d},
                               headers=api_key).json() for d in ("lines", "text"))
    assert full["pages"][0]["source"] == brief["pages"][0]["source"] == "text_layer"
    assert "lines" in full["pages"][0] and "lines" not in brief["pages"][0]  # detail=text still leaves the lines out
    assert full["document_type"] == "driver_permit"
    assert (brief["document_type"], brief["document_label"]) == (full["document_type"], full["document_label"])


def test_a_task_the_standard_reader_ignores_does_not_stop_the_naming(client, api_key, need_engine):
    r = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(CERTIFICATE), "image/png")}, data={"task": "table"},
                    headers=api_key)
    assert r.status_code == 200 and r.json()["document_type"] == "vehicle_registration"


def test_what_the_model_calls_other_is_no_kind_of_document(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers.append({**{k: None for k in extract.SALE_SCHEMA["properties"]}, "document_type": "other", "line_items": []})
    body = extract_image(client, api_key, ["Minutes of the weekly team meeting", "Present: Aina, Farid, Mei Ling",
                                           "Next meeting: Monday at ten"], external_ref="minutes-1").json()
    assert (body["document_type"], body["identified_type"], body["document_label"]) == ("other", None, None)
    rows = client.get("/v1/documents?external_ref=minutes-1", headers=api_key).json()["documents"]
    assert [(d["document_type"], d["document_label"]) for d in rows] == [(None, "Not recognised")]


def test_a_form_read_as_something_else_is_still_named_as_the_form(client, api_key, need_engine, fake_model):
    answers, _ = fake_model
    answers.append({})  # the payslip the caller asked for is not there to read
    body = extract_image(client, api_key, CERTIFICATE, type="payslip", external_ref="forced-1").json()
    assert body["document_type"] == "payslip"  # what the fields were read as, as the caller asked
    assert (body["identified_type"], body["document_label"]) == ("vehicle_registration", "Vehicle registration card (VOC, geran)")
    rows = client.get("/v1/documents?external_ref=forced-1", headers=api_key).json()["documents"]
    assert [d["document_type"] for d in rows] == ["vehicle_registration"]


# --- the announcement of a job ---------------------------------------------------------------------
def test_a_job_read_plainly_is_announced_at_the_usual_webhook_whatever_it_is_named(client, need_engine, monkeypatch):
    from app import webhooks
    sent = []
    monkeypatch.setattr(webhooks, "_deliver", lambda url, secret, payload: sent.append((payload["external_ref"], url)))
    made = client.post("/admin/api/clients", json={"name": "routed", "webhook_url": "https://app.example.com/hook"},
                       headers=ADMIN).json()
    key = {"X-API-Key": made["api_key"]}
    assert client.put(f"/admin/api/clients/{made['id']}/routes", headers=ADMIN,
                      json={"routes": {"vehicle_registration": "https://fleet.example.com/hook"}}).status_code == 200
    r = client.post("/v1/jobs", files={"file": ("a.png", synth.image_bytes(CERTIFICATE), "image/png")},
                    data={"external_ref": "plain-1"}, headers=key)
    job = wait_for(client, key, r.json()["id"])
    assert job["status"] == "completed" and job["document_type"] == "vehicle_registration"  # named all the same
    end = time.time() + 5
    while not sent and time.time() < end:
        time.sleep(0.05)
    # routes go by the kind the fields were extracted as: a plain reading has none, and its app is the one to tell
    assert sent == [("plain-1", "https://app.example.com/hook")]


# --- the record ---------------------------------------------------------------------------------
def test_a_line_has_a_handle_of_its_own_and_can_be_removed(client, api_key, need_engine):
    png = synth.image_bytes(CERTIFICATE)
    client.post("/v1/ocr", files={"file": ("a.png", png, "image/png")}, data={"external_ref": "remove-me"}, headers=api_key)
    row = client.get("/v1/documents?external_ref=remove-me", headers=api_key).json()["documents"][0]
    assert row["id"].startswith("doc_") and len(row["id"]) == 28  # not a count of every client's documents
    other = {"X-API-Key": client.post("/admin/api/clients", json={"name": "someone else"}, headers=ADMIN).json()["api_key"]}
    assert client.delete("/v1/documents/" + row["id"], headers=other).status_code == 404  # not another client's to remove
    assert client.delete("/v1/documents/" + row["id"], headers=api_key).status_code == 204
    assert client.get("/v1/documents?external_ref=remove-me", headers=api_key).json() == {"documents": []}
    assert client.delete("/v1/documents/" + row["id"], headers=api_key).status_code == 404


def test_the_lines_of_a_job_go_with_the_job(client, api_key, need_engine):
    r = client.post("/v1/jobs", files={"file": ("a.png", synth.image_bytes(CERTIFICATE), "image/png")},
                    data={"external_ref": "job-gone"}, headers=api_key)
    wait_for(client, api_key, r.json()["id"])
    assert len(client.get("/v1/documents?external_ref=job-gone", headers=api_key).json()["documents"]) == 1
    assert client.delete("/v1/jobs/" + r.json()["id"], headers=api_key).status_code == 204
    assert client.get("/v1/documents?external_ref=job-gone", headers=api_key).json() == {"documents": []}


def test_a_reference_that_cannot_be_stored_does_not_fail_the_reading(client, api_key, need_engine):
    import base64
    body = {"file": base64.b64encode(synth.image_bytes(CERTIFICATE)).decode(), "external_ref": "driver-\ud83d"}
    r = client.post("/v1/ocr", content=__import__("json").dumps(body), headers={**api_key, "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["document_type"] == "vehicle_registration"
    assert any(d["external_ref"] == "driver-?" for d in client.get("/v1/documents", headers=api_key).json()["documents"])


def test_old_lines_go_when_a_time_is_set_for_them(client, api_key, need_engine, monkeypatch):
    client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(CERTIFICATE), "image/png")}, data={"external_ref": "aged"},
                headers=api_key)
    with session_scope() as db:
        for row in db.query(DocumentRecord).filter(DocumentRecord.external_ref == "aged"):
            row.at = utcnow() - timedelta(days=40)
    service.housekeeping()  # no time set: the record is kept
    assert len(client.get("/v1/documents?external_ref=aged", headers=api_key).json()["documents"]) == 1
    monkeypatch.setattr(get_settings(), "record_days", 30)
    service.housekeeping()
    assert client.get("/v1/documents?external_ref=aged", headers=api_key).json() == {"documents": []}
