"""Evidence, file hints, corrections, matching, routes, console users, several tables, turned pages, alerts."""
import io

from app import engine, extract, match, service, tables
from tests import synth
from tests.conftest import *  # noqa: F401,F403
from tests.test_api import ADMIN, LINES
from tests.test_doctypes import settlement_table
from tests.test_extract import INVOICE, INVOICE_LINES, fake_model, post_invoice, table_page  # noqa: F401


def test_evidence_says_how_well_each_value_is_backed(client, api_key, need_engine, fake_model):  # noqa: F811
    answers, _ = fake_model
    answers += [{**INVOICE, "customer_name": "Nobody Sdn Bhd", "total": 999.0}] * 2
    body = post_invoice(client, api_key).json()
    ev = body["evidence"]
    assert ev["customer_name"] == "low" and ev["total"] == "low"  # neither is printed
    assert ev["supplier_name"] == "high" and ev["date"] == "unverified"
    assert ev["subtotal"] == "medium"  # printed, but the sums did not work out
    assert body["file"]["edited_with"] is None and set(body["file"]) >= {"producer", "created", "modified"}


def test_file_hints_name_an_editing_program():
    from PIL import Image
    from app.documents import Document
    im = Image.new("RGB", (60, 60), "white")
    exif = im.getexif()
    exif[305] = "Adobe Photoshop 25.0 (Macintosh)"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    with Document(buf.getvalue()) as doc:
        assert doc.info()["edited_with"] == "Photoshop" and "Photoshop" in doc.info()["producer"]
    with Document(synth.text_pdf("hello there, this is a text")) as doc:
        assert doc.info()["edited_with"] is None


def test_a_reported_misreading_is_put_right_next_time(client, api_key, need_engine, fake_model):  # noqa: F811
    answers, _ = fake_model
    misread = {**INVOICE, "supplier_name": "ACME SUPPLlES SDN BHD", "document_number": "INV-2026-01500"}
    lines = [ln.replace("INV-2026-00417", "INV-2026-01500") for ln in INVOICE_LINES]
    answers += [misread, misread]
    send = lambda: client.post("/v1/extract", files={"file": ("m.png", synth.image_bytes(lines), "image/png")},  # noqa: E731
                               headers=api_key).json()
    assert send()["corrected_from_memory"] == {}
    made = client.post("/v1/corrections", headers=api_key, json={
        "document_type": "invoice", "field": "supplier_name", "original": "ACME SUPPLlES SDN BHD",
        "corrected": "ACME SUPPLIES SDN BHD"})
    assert made.status_code == 201
    again = send()
    assert again["fields"]["supplier_name"] == "ACME SUPPLIES SDN BHD"
    assert again["corrected_from_memory"]["supplier_name"]["from"] == "ACME SUPPLlES SDN BHD"
    assert client.get("/v1/corrections", headers=api_key).json()["corrections"][0]["field"] == "supplier_name"
    assert client.delete(f"/v1/corrections/{made.json()['id']}", headers=api_key).status_code == 204
    bad = client.post("/v1/corrections", headers=api_key, json={"document_type": "invoice", "field": "x",
                                                                "original": "same", "corrected": "same"})
    assert bad.status_code == 422


def test_an_invoice_is_set_against_its_order():
    order = {"supplier_name": "ACME SUPPLIES SDN BHD", "total": 125.5, "line_items": [
        {"description": "Widget blue", "quantity": 2, "unit_price": 50.0, "amount": 100.0},
        {"description": "Gasket 12mm", "quantity": 1, "unit_price": 25.5, "amount": 25.5}]}
    invoice = {"supplier_name": "Acme Supplies Sdn. Bhd.", "total": 175.5, "line_items": [
        {"description": "Gasket 12mm", "quantity": 1, "unit_price": 25.5, "amount": 25.5},
        {"description": "Widget blue", "quantity": 3, "unit_price": 50.0, "amount": 150.0},
        {"description": "Delivery charge", "quantity": 1, "unit_price": 0.0, "amount": 0.0}]}
    r = match.compare(order, invoice)
    assert r["agree"] is False and r["parties"]["supplier_name"]["same"] is True and r["totals"]["total"]["same"] is False
    assert [(ln["status"], ln["differences"]) for ln in r["lines"]] == [
        ("differs", ["quantity", "amount"]), ("same", []), ("only_in_b", [])]
    assert match.compare(order, order)["agree"] is True


def test_payments_are_found_on_a_statement(client, api_key):
    statement = {"transactions": [
        {"date": "2026-01-05", "description": "IBG ACME SUPPLIES INV-2026-00417", "debit": 135.54, "credit": None},
        {"date": "2026-01-06", "description": "TRANSFER", "debit": 135.54, "credit": None},
        {"date": "2026-01-09", "description": "TNB", "debit": 198.3, "credit": None}]}
    docs = [{"ref": "a", "fields": {"document_number": "INV-2026-00417", "total": 135.54, "date": "2026-01-03"}},
            {"ref": "b", "fields": {"total_due": 198.3, "provider_name": "TNB"}},
            {"ref": "c", "fields": {"total": 77.0}}]
    r = client.post("/v1/match", headers=api_key, json={"reconcile": {"statement": statement, "documents": docs}}).json()
    assert [(d["document"], d["transaction_index"], d["by"]) for d in r["documents"]] == [
        ("a", 0, "reference"), ("b", 2, "amount"), ("c", None, None)]
    assert r["unmatched_transactions"] == [1] and r["all_matched"] is False
    assert client.post("/v1/match", headers=api_key, json={"nothing": 1}).status_code == 422


def test_routes_send_a_kind_of_document_to_its_own_address(client, monkeypatch):
    from app import webhooks
    made = client.post("/admin/api/clients", json={"name": "routed", "webhook_url": "https://app.example.com/hook"},
                       headers=ADMIN).json()
    no_hook = client.post("/admin/api/clients", json={"name": "no hook"}, headers=ADMIN).json()
    r = client.put(f"/admin/api/clients/{made['id']}/routes", headers=ADMIN,
                   json={"routes": {"invoice": "https://accounts.example.com/hook"}})
    assert r.status_code == 200 and client.get(f"/admin/api/clients/{made['id']}/routes", headers=ADMIN).json()["routes"] == \
        {"invoice": "https://accounts.example.com/hook"}
    assert client.put(f"/admin/api/clients/{no_hook['id']}/routes", headers=ADMIN,
                      json={"routes": {"invoice": "https://x.example.com"}}).json()["error"] == "no_webhook"
    assert client.put(f"/admin/api/clients/{made['id']}/routes", headers=ADMIN,
                      json={"routes": {"passport": "https://x.example.com"}}).json()["error"] == "bad_type"
    sent = []
    monkeypatch.setattr(webhooks.threading, "Thread", lambda target, daemon, args: type("T", (), {"start": lambda s: sent.append(args[0])})())

    class J:
        id, status, external_ref, pages, error, error_message, client_id = "job_1", "completed", None, 1, None, None, "c"
        result = {"document_type": "invoice"}

    class C:
        webhook_url, webhook_secret = "https://app.example.com/hook", b"s"

    monkeypatch.setattr(webhooks, "payload_for", lambda job: {"id": job.id})
    webhooks.notify(J, C, {"invoice": "https://accounts.example.com/hook"})
    J.result = {"document_type": "receipt"}
    webhooks.notify(J, C, {"invoice": "https://accounts.example.com/hook"})
    assert sent == ["https://accounts.example.com/hook", "https://app.example.com/hook"]


def test_console_users_and_what_a_viewer_may_do(client):
    viewer = client.post("/admin/api/users", json={"name": "Aina", "role": "viewer"}, headers=ADMIN).json()
    admin = client.post("/admin/api/users", json={"name": "Faiz", "role": "admin"}, headers=ADMIN).json()
    v, a = {"X-Admin-Token": viewer["token"]}, {"X-Admin-Token": admin["token"]}
    assert client.get("/admin/api/clients", headers=v).status_code == 200
    assert client.post("/admin/api/clients", json={"name": "x"}, headers=v).status_code == 403
    assert client.post("/admin/api/try", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")},
                       headers=v).status_code == 200
    made = client.post("/admin/api/clients", json={"name": "made by Faiz"}, headers=a)
    assert made.status_code == 201 and client.get("/admin/api/users", headers=a).status_code == 403
    events = client.get("/admin/api/audit", headers=ADMIN).json()["events"]
    assert "(by Faiz)" in events[0]["target"] and viewer["token"] not in str(events)
    assert client.delete(f"/admin/api/users/{viewer['id']}", headers=ADMIN).status_code == 204
    assert client.get("/admin/api/clients", headers=v).status_code == 401
    assert client.post("/admin/api/users", json={"name": "x", "role": "root"}, headers=ADMIN).status_code == 422


def test_several_tables_in_one_document():
    first = settlement_table()
    more = table_page([
        [("Item", 30), ("Qty", 300, True), ("Amount", 560, True)],
        [("Refund A", 30), ("1", 300, True), ("20.00", 560, True)],
        [("Refund B", 30), ("2", 300, True), ("30.00", 560, True)],
        [("Total", 30), ("3", 300, True), ("50.00", 560, True)],
    ])
    offset = max(p[1] for ln in first["lines"] for p in ln["box"]) + 120
    for ln in more["lines"]:
        ln["box"] = [[x, y + offset] for x, y in ln["box"]]
    page = {**first, "lines": first["lines"] + more["lines"]}
    got = tables.parse([page])
    assert len(got["rows"]) == 2 and len(got["more"]) == 1
    assert got["more"][0]["columns"] == ["item", "qty", "amount"] and got["more"][0]["totals"] == {"qty": 3.0, "amount": 50.0}
    assert tables.checks(got)[0]["passed"] is True
    got["more"][0]["rows"][0]["amount"] = 25.0
    assert "table 2: amount adds up to 55.00" in tables.checks(got)[0]["detail"]


def test_an_upside_down_page_is_turned(need_engine):
    import numpy as np
    lines = ["Invoice No: INV-2026-00417", "Jumlah perlu dibayar RM 1,250.80", "Third line of text here", "Total 135.54"]
    upright = np.ascontiguousarray(np.asarray(synth.page_image(lines))[:, :, ::-1])
    got, _, turned = engine.read_upright(np.ascontiguousarray(np.rot90(upright, 2)))
    text = engine.layout_text(got)
    assert turned == 180 and text.index("Invoice") < text.index("Jumlah") < text.index("Total")
    assert engine.read_upright(upright)[2] == 0


def test_of_the_two_quarter_turns_the_one_whose_lines_stand_upright_is_taken(monkeypatch):
    """Each line is read the right way round whichever quarter a picture on its side is turned by, so both
    turns read as well as each other; one of them leaves the page on its head. A real card photographed on
    its side came out that way, and its name and address could not be placed."""
    import numpy as np

    def box(text, x, y, w, h):
        return {"text": text, "confidence": 0.99, "box": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]}

    down_the_picture = [box("KAD PENGENALAN", 40, 30, 24, 200), box("850412-10-5759", 80, 30, 24, 200)]
    level = [box("KAD PENGENALAN", 30, 40, 200, 24), box("850412-10-5759", 30, 80, 200, 24)]
    picture = np.zeros((400, 300, 3), np.uint8)
    picture[0, 0] = 255  # a mark in one corner, to tell the two turns apart: turned anticlockwise it is at the bottom left
    monkeypatch.setattr(engine, "read", lambda img, script=None: level)
    for head_first, wanted in ((270, 90), (90, 270)):
        def share(img, lines, head_first=head_first):
            anticlockwise = img[-1, 0, 0] == 255  # the turn the engine calls 270
            return 0.9 if anticlockwise == (head_first == 270) else 0.1
        monkeypatch.setattr(engine, "on_their_head", share)
        lines, upright, turned = engine._read_quarters(picture, None, down_the_picture)
        assert turned == wanted and lines == level and upright.shape[:2] == (300, 400)
    # where the classifier does not tell the two apart, the better read turn is taken, as before
    monkeypatch.setattr(engine, "on_their_head", lambda img, lines: 0.5)
    assert engine._read_quarters(picture, None, down_the_picture)[2] == 270
    monkeypatch.setattr(engine, "on_their_head", lambda img, lines: None)
    assert engine._read_quarters(picture, None, down_the_picture)[2] == 270


def test_a_second_reading_in_the_grounds_own_colour_is_taken_where_it_is_surer(monkeypatch):
    """A card's hologram lies over its print in colours. In the colour of the card itself it is as light as the
    card, and the print under it reads clear: a real card's name read "... AR" in full colour and "... AL" in blue."""
    import numpy as np

    def line(text, confidence, x=40, y=40, w=None):
        w = w or 14 * len(text)
        return {"text": text, "confidence": confidence, "box": [[x, y], [x + w, y], [x + w, y + 30], [x, y + 30]]}

    # which reading of a line is kept: the second may add to a line, never take from it
    kept = lambda a, b: engine.surer([a], [b])[0]["text"]  # noqa: E731
    assert kept(line("RAVIKUNAR AR", 0.92), line("RAVIKUMAR AL", 1.0)) == "RAVIKUMAR AL"      # alike, as long, clearly surer
    assert kept(line("RAVIKUNAR", 0.99, w=170), line("RAVIKUMAR AL", 1.0, w=200)) == "RAVIKUMAR AL"  # longer by two and as sure
    assert kept(line("JOHARI BINAGAP", 0.97), line("JOHARI BIN AGAP", 0.95)) == "JOHARI BIN AGAP"    # the same letters, a space regained
    assert kept(line("B2 D E", 0.94), line("B2 DE", 1.0)) == "B2 D E"                                # a space lost: not taken, however sure
    assert kept(line("D:TEKSI/TEKSI MEWAH/", 0.92), line("D:TEKSI/TEKSI MEWAH", 0.98)) == "D:TEKSI/TEKSI MEWAH/"  # a character lost
    assert kept(line("WXY1234", 1.0), line(": WXY1234", 0.98, x=20, w=130)) == "WXY1234"             # longer, but neither surer nor by two
    assert kept(line("TOTAL 135.54", 0.9), line("SOMETHING ELSE HERE", 1.0)) == "TOTAL 135.54"       # not the same line's words
    assert kept(line("TAN MEI LING", 0.9), line("TAN MEI LING", 1.0, y=200)) == "TAN MEI LING"       # another place: no second reading of it
    # in one colour alone a small comma loses its tail and reads as a stop, and reads so more surely: not taken; ink regained is
    assert kept(line("338,214.28", 0.958), line("338.214.28", 0.994)) == "338,214.28"
    assert kept(line("Jumlah 9,319.39", 0.94), line("Jumlah 9.319.39", 0.99)) == "Jumlah 9,319.39"
    assert kept(line("338.214.28", 0.9), line("338,214.28", 0.99)) == "338,214.28" and kept(line("84.38", 0.9), line("84.33", 1.0)) == "84.33"
    assert kept(line("338,2I4.28", 0.9), line("338.214.28", 0.99)) == "338,214.28"  # the comma stays whatever else is put right
    assert kept(line("1,250 dan 1.250", 0.9), line("1,250 dan 1.250 sahaja", 0.99, w=320)) == "1,250 dan 1.250 sahaja"
    # a space is regained between words, between a number and a word, between the groups of a long number and between
    # two dates; not inside an amount or a long code
    assert kept(line("775,442.03", 1.0), line("775, 442.03", 0.96)) == "775,442.03" and kept(line("X 775,442.03", 1.0), line("X775, 442.03", 0.96)) == "X 775,442.03"
    assert kept(line("425613007208", 1.0), line("4256 1300 7208", 0.97)) == "4256 1300 7208"
    assert kept(line("15/03/2022-12/03/2025", 1.0), line("15/03/2022 - 12/03/2025", 0.98)) == "15/03/2022 - 12/03/2025"
    assert kept(line("PMLLAJVF7RV1K50S1", 1.0), line("PMLLAJVF 7RV1K50S1", 0.97)) == "PMLLAJVF7RV1K50S1"
    assert kept(line("43000KAJANG", 1.0), line("43000 KAJANG", 0.99)) == "43000 KAJANG" and kept(line("NO 12JALAN 3", 1.0), line("NO 12 JALAN 3", 0.96)) == "NO 12 JALAN 3"
    assert kept(line("B2D", 1.0), line("B2 D", 0.99)) == "B2 D" and kept(line("Cawangan:0701", 0.99), line("Cawangan: 0701", 0.97)) == "Cawangan: 0701"
    both = engine.surer([line("ONE", 0.9), line("TWO", 0.9, y=100)], [line("TWO!", 1.0, y=100), line("THREE", 1.0, y=200)])
    assert [ln["text"] for ln in both] == ["ONE", "TWO!"]  # no line is added or removed
    # the ground's colour is taken where the text stands: a blue card on a red table is blue; a white page has none
    picture = np.zeros((300, 400, 3), np.uint8)
    picture[:] = (40, 60, 200)                 # the table (blue, green, red)
    picture[20:120, 20:380] = (230, 180, 150)  # the card
    assert engine.ground_colour(picture, [line("RAVIKUMAR A/L", 0.9, 40, 40), line("MUTHUSAMY", 1.0, 40, 80)]) == 0
    picture[:] = 245
    assert engine.ground_colour(picture, [line("RAVIKUMAR A/L", 0.9)]) is None
    # read_upright: an unsure line on a coloured ground brings a second reading, in that colour alone
    picture[:] = (230, 180, 150)
    shown = []

    def read(img, script=None):
        shown.append(bool((img[:, :, 0] == img[:, :, 1]).all() and (img[:, :, 1] == img[:, :, 2]).all()))
        return [line("RAVIKUMAR AL", 1.0), line("MUTHUSAMY", 1.0, y=80)] if shown[-1] else [line("RAVIKUNAR AR", 0.92), line("MUTHUSAMY", 1.0, y=80)]

    monkeypatch.setattr(engine, "read", read)
    lines, _, turned = engine.read_upright(picture)
    assert [ln["text"] for ln in lines] == ["RAVIKUMAR AL", "MUTHUSAMY"] and turned == 0 and shown == [False, True]
    assert (picture[0, 0] == (230, 180, 150)).all()  # the picture itself is left as it was
    # every line sure, or a white ground: the picture is read once
    shown.clear()
    monkeypatch.setattr(engine, "read", lambda img, script=None: shown.append(1) or [line("RAVIKUMAR A/L", 0.99), line("MUTHUSAMY", 1.0, y=80)])
    engine.read_upright(picture)
    assert shown == [1]


def test_a_stamp_is_told_by_its_slope_and_its_ink_and_the_print_under_it_is_read_with_the_ink_taken_out(need_engine):
    """An app stamps its name in red across the photo of a card it keeps. Told from the card's print by its slope
    alone, it was the card's name again as soon as the card lay a few degrees the stamp's way."""
    import numpy as np
    from PIL import Image, ImageDraw
    address = "NO 29, JALAN WATAN 11, TAMAN SRI WATAN, 68000 AMPANG, SELANGOR"
    card = synth.identity_card()

    def read(picture):
        data = synth.png(picture)
        out = service.extract_fields(data, {"extract": "mykad"}, 3)
        lines = service.recognise(data, {"detail": "lines"}, 3)["pages"][0]["lines"]
        return out["fields"], {c["name"]: c["passed"] for c in out["checks"]}, out["checks_passed"], lines

    # the card leans eight degrees the way the stamp rises: ten degrees lie between them, and less as the reader measures it
    f, checks, passed, lines = read(synth.stamped(synth.on_a_table(card, 8), at=(420, 560)))
    assert (f["name"], f["address"]) == ("LIM AH KOW", address)
    assert sorted(ln["text"] for ln in lines if ln.get("across")) == ["GET RIDE", "Registration"]  # marked, and kept in the text
    assert checks["clear_of_stamp"] is True and passed is True  # it stands clear of the number, the name and the address
    # a faint stamp across the name: print and stamp come as one line, and with the stamp's ink taken out the name is read
    f, checks, passed, lines = read(synth.stamped(card, at=(260, 540), solid=115))
    assert (f["name"], f["address"]) == ("LIM AH KOW", address)
    assert any(ln.get("under_stamp") and ln["text"] == "LIM AH KOW" for ln in lines)
    assert checks["clear_of_stamp"] is False and passed is False  # and the caller is told a stamp lay across it
    # a card with no stamp has no such check, and a line of red print that runs level with the page is the page's own
    f, checks, passed, lines = read(card)
    assert (f["name"], "clear_of_stamp" in checks, passed) == ("LIM AH KOW", False, True)
    page = Image.new("RGB", (1100, 700), "white")
    d = ImageDraw.Draw(page)
    for n, text in enumerate(["SIJIL PEMILIKAN KENDERAAN", "No. Pendaftaran : WXY1234", "Nama Pemilik : AHMAD BIN ALI", "No. Enjin : 4G15AB12345",
                              "No. Casis : PL1BT3SNRGB123456", "Tarikh : 01/07/2023"]):
        d.text((50, 50 + 80 * n), text, fill="black", font=synth._font(38))
    d.text((640, 600), "No. Siri : AB1234567", fill=(215, 30, 30), font=synth._font(38))
    lines, _, _ = engine.read_upright(np.ascontiguousarray(np.asarray(page)[:, :, ::-1]))
    assert any("AB1234567" in ln["text"] for ln in lines) and not any(ln.get("across") or ln.get("under_stamp") for ln in lines)


def test_a_stamps_ink_is_taken_out_of_a_picture_whatever_its_strength():
    import numpy as np
    ground, stamp, print_ = np.array([238.0, 214.0, 176.0]), np.array([50.0, 50.0, 230.0]), np.array([24.0, 20.0, 20.0])  # blue, green, red
    faint, crossed = 0.55 * ground + 0.45 * stamp, 0.55 * print_ + 0.45 * stamp
    picture = np.array([[ground, stamp, faint, print_, crossed]], dtype=np.uint8)
    seen = engine.without_ink(picture, ground, stamp, print_)[0].astype(int)
    assert abs(seen[0] - 235) <= 2 and abs(seen[1] - seen[0]) <= 3 and abs(seen[2] - seen[0]) <= 3  # the ink is as light as the ground
    assert seen[3] <= 22 and seen[3] < seen[4] < seen[0] - 60  # print stays dark, and print under a faint stamp stays darker than the ground
    # a grey stamp on a black and white copy lies between the ground and the print: there is no such mix
    assert engine.without_ink(picture, np.array([240.0] * 3), np.array([150.0] * 3), np.array([30.0] * 3)) is None
    # which lines are a stamp's: off the page's own lines and in another ink, on the page's own ground
    img = np.full((700, 1000, 3), (238, 214, 176), np.uint8)

    def line(text, y, ink, degrees=0.0, x=60, w=420, h=40):
        import cv2
        import math
        a = math.radians(degrees)
        box = [[x, y], [x + w * math.cos(a), y - w * math.sin(a)], [x + w * math.cos(a) + h * math.sin(a), y - w * math.sin(a) + h * math.cos(a)],
               [x + h * math.sin(a), y + h * math.cos(a)]]
        strokes = np.zeros(img.shape[:2], np.uint8)
        cv2.fillPoly(strokes, [np.round(np.array(box)).astype(np.int32)], 1)
        strokes[:, 1::3] = strokes[:, 2::3] = 0  # a third of the box in ink, the rest ground, as a line of letters is
        img[strokes > 0] = ink
        return {"text": text, "confidence": 0.99, "box": [[round(px, 1), round(py, 1)] for px, py in box]}

    own = [line("LINE %d OF THE CARD'S OWN PRINT" % n, 60 + 70 * n, (24, 20, 20)) for n in range(6)]
    red_print = line("No. Siri : AB1234567", 520, (50, 50, 230))
    in_black = line("A SECOND PAPER UNDER THE FIRST", 640, (24, 20, 20), degrees=8, x=480)
    watermark = line("Registration", 470, (50, 50, 230), degrees=8, x=520)
    assert engine.stamp_lines(img, own + [red_print, in_black, watermark]) == [watermark]


def test_a_second_reading_under_a_stamp_is_taken_only_at_the_stamp_and_only_where_it_is_clearly_better(monkeypatch):
    """A "paid" stamp in an empty corner of an invoice made every line of the page count as touched by its ink, and
    a lone 9 was replaced by the 6 the second reading made of it; on a form filled in with a red pen a name read
    right was replaced by two pieces of it. Found by a review of the first version of this step."""
    import cv2
    import math
    import numpy as np
    img = np.full((900, 1200, 3), 250, np.uint8)  # white paper: the soft edge of a black letter is grey here
    black, red = (24, 20, 20), (50, 50, 230)

    def line(text, x, y, ink=black, degrees=0.0, w=360, h=40, also=None, confidence=0.99):
        a = math.radians(degrees)
        box = [[x, y], [x + w * math.cos(a), y - w * math.sin(a)], [x + w * math.cos(a) + h * math.sin(a), y - w * math.sin(a) + h * math.cos(a)],
               [x + h * math.sin(a), y + h * math.cos(a)]]
        strokes = np.zeros(img.shape[:2], np.uint8)
        cv2.fillPoly(strokes, [np.round(np.array(box)).astype(np.int32)], 1)
        edge = strokes.copy()
        strokes[:, 1::4] = strokes[:, 2::4] = strokes[:, 3::4] = 0  # a quarter of the box in ink
        edge[:, 0::4] = edge[:, 2::4] = edge[:, 3::4] = 0           # and beside each stroke its soft edge, half-way to the paper
        img[edge > 0] = tuple((int(c) + 250) // 2 for c in ink)
        img[strokes > 0] = ink
        if also:  # a stroke of another ink run into the line
            img[int(y) + 4:int(y) + h - 4, int(x) + w - 60:int(x) + w - 20][:, ::2] = also
        return {"text": text, "confidence": confidence, "box": [[round(px, 1), round(py, 1)] for px, py in box]}

    rows = [line("ROW %d OF AN INVOICE 9" % n, 60, 60 + 70 * n) for n in range(7)]
    stamp = [line("TELAH DIBAYAR", 700, 700, red, degrees=13), line("12 MEI 2026", 720, 770, red, degrees=13)]
    reads = []
    monkeypatch.setattr(engine, "read", lambda picture, script=None: reads.append(1) or [])
    # the stamp stands clear of all print: its lines are marked, no line of the page is, and nothing is read again
    out = engine.under_a_stamp(img, rows + stamp)
    assert [ln.get("across") for ln in out] == [None] * 7 + [True, True] and not any(ln.get("under_stamp") for ln in out) and reads == []
    # the stamp runs into two lines beside it: those are read again, and no other line of the page is touched
    hit = line("JALAN WATAN 11E", 640, 600, also=red, confidence=0.95)
    nine = line("9", 980, 640, w=60, also=red, confidence=0.9995)
    lines = rows + [hit, nine] + stamp

    def second(*pieces):
        monkeypatch.setattr(engine, "read", lambda picture, script=None: [dict(src, text=text, confidence=conf) for src, text, conf in pieces])
        got = engine.under_a_stamp(img, lines)
        return {ln["text"]: sorted(k for k in ("across", "under_stamp") if ln.get(k)) for ln in got}

    got = second((hit, "JALAN WATAN 11", 0.94), (nine, "6", 0.9999), (rows[0], "ROW 0 OF AN INVOICE 6", 1.0))
    assert got["JALAN WATAN 11"] == ["under_stamp"] and "JALAN WATAN 11E" not in got  # the first reading's letters without what the stamp added
    assert got["9"] == ["under_stamp"] and "6" not in got                              # not surer by a margin: the first reading stands
    assert got["ROW 0 OF AN INVOICE 9"] == [] and "ROW 0 OF AN INVOICE 6" not in got  # away from the stamp nothing is taken
    assert second((hit, "JALAN WATAN 11E", 1.0))["JALAN WATAN 11E"] == ["under_stamp"]                       # the same letters: it stands
    assert second((hit, "JALAN WATAM 11E", 0.99))["JALAN WATAM 11E"] == ["under_stamp"]                      # surer by a margin: taken
    assert "JALAN WATAN 11E" in second((hit, "JALAN", 0.99), (dict(hit, box=[[p[0] + 200, p[1]] for p in hit["box"]]), "ATAN 11", 0.99))  # two pieces of a sure line: it stands


def test_problems_are_named(client, monkeypatch):
    from app import llm
    from app.config import get_settings
    monkeypatch.setattr(llm, "ready", lambda *a, **k: False)
    found = service.problems()
    assert "extraction" in found and "disk" not in found
    monkeypatch.setattr(get_settings(), "alert_disk_gb", 10 ** 9)
    assert "disk" in service.problems()


def test_tick_boxes_and_stamps_are_reported_when_asked(client, api_key, need_engine):
    import cv2
    import numpy as np
    page = np.full((700, 1000, 3), 255, np.uint8)
    for i, (label, tick) in enumerate([("Scratches", True), ("Dents", False), ("Spare tyre present", True)]):
        x, y = 60, 80 + 70 * i
        cv2.rectangle(page, (x, y), (x + 28, y + 28), (0, 0, 0), 2)
        if tick:
            cv2.line(page, (x + 5, y + 14), (x + 12, y + 23), (0, 0, 0), 3)
            cv2.line(page, (x + 12, y + 23), (x + 24, y + 5), (0, 0, 0), 3)
        cv2.putText(page, label, (110, y + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    cv2.circle(page, (780, 520), 90, (40, 40, 200), 5)
    png = cv2.imencode(".png", page)[1].tobytes()
    plain = client.post("/v1/ocr", files={"file": ("f.png", png, "image/png")}, headers=api_key).json()["pages"][0]
    assert "checkboxes" not in plain and "preview" not in plain
    got = client.post("/v1/ocr", files={"file": ("f.png", png, "image/png")}, data={"marks": "true"},
                      headers=api_key).json()["pages"][0]
    assert [(c["label"], c["checked"]) for c in got["checkboxes"]] == [
        ("Scratches", True), ("Dents", False), ("Spare tyre present", True)]
    assert [s["colour"] for s in got["stamps"]] == ["red"]


def test_the_console_gets_a_picture_of_each_page_to_review(client, need_engine):
    pdf = synth.scanned_pdf([INVOICE_LINES, LINES])
    r = client.post("/admin/api/try", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=ADMIN).json()
    assert all(p["preview"].startswith("data:image/jpeg;base64,") for p in r["pages"])


def test_a_scan_becomes_a_searchable_pdf(client, api_key, need_engine):
    from app.documents import Document
    scan = synth.scanned_pdf([INVOICE_LINES, LINES])
    r = client.post("/v1/searchable", files={"file": ("s.pdf", scan, "application/pdf")}, headers=api_key)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:5] == b"%PDF-"
    with Document(r.content) as doc:
        assert doc.page_count == 2
        first, second = doc.page(1, with_lines=True), doc.page(2)
        assert "INV-2026-00417" in first.text_layer and "Total RM 135.54" in first.text_layer
        assert "Jumlah perlu dibayar" in second.text_layer
        tops = {ln["text"]: ln["box"][0][1] for ln in first.text_lines}
        assert tops["ACME SUPPLIES SDN BHD"] < tops["Subtotal 125.50"] < tops["Total RM 135.54"]  # each line where it is printed
    # a PDF that already carries its text comes back as it was
    text_pdf = synth.text_pdf("Perjanjian sewa kenderaan 2026")
    same = client.post("/v1/searchable", files={"file": ("t.pdf", text_pdf, "application/pdf")}, headers=api_key)
    assert same.content == text_pdf
    photo = client.post("/v1/searchable", files={"file": ("p.png", synth.image_bytes(LINES), "image/png")}, headers=api_key)
    with Document(photo.content) as doc:
        assert "INV-2026-00417" in doc.page(1).text_layer


def test_barcodes_of_several_kinds_are_read():
    import numpy as np
    import zxingcpp
    from app import codes
    for kind, text in ((zxingcpp.BarcodeFormat.Code128, "TRACK-99812"), (zxingcpp.BarcodeFormat.QRCode, "https://example.com/a?b=1"),
                       (zxingcpp.BarcodeFormat.EAN13, "9556001234568")):
        try:
            img = np.array(zxingcpp.write_barcode(kind, text, width=400, height=160))
        except AttributeError:  # an older ZXing without write_barcode
            return
        img = img if img.ndim == 2 else img[:, :, 0]
        page = np.full((img.shape[0] + 120, img.shape[1] + 120, 3), 255, np.uint8)
        page[60:60 + img.shape[0], 60:60 + img.shape[1]] = img[:, :, None]
        found = codes.find(page)
        assert [c["text"] for c in found] == [text], kind
        assert len(found[0]["box"]) == 4 and found[0]["kind"]


def test_merged_cells_are_placed_in_the_grid():
    from app import tablegrid
    html = ('<html><body><table><tr><td rowspan="2">Subject</td><td colspan="2">Age (years)</td></tr>'
            '<tr><td>Mean</td><td>Range</td></tr><tr><td>With OA</td><td>64.8</td><td>45 - 83</td></tr></table></body></html>')
    cells = tablegrid.cells_of(html)
    assert cells[0] == {"row": 0, "column": 0, "rowspan": 2, "colspan": 1, "text": "Subject"}
    assert cells[1] == {"row": 0, "column": 1, "rowspan": 1, "colspan": 2, "text": "Age (years)"}
    assert [(c["row"], c["column"]) for c in cells[2:]] == [(1, 1), (1, 2), (2, 0), (2, 1), (2, 2)]  # the cell reaching down pushes the next row along


def test_signature_detection_is_off_until_its_model_is_fetched(client, api_key, need_engine):
    from app import signatures
    assert signatures.available() is False and client.get("/health").json()["signatures"] is False
    page = client.post("/v1/ocr", files={"file": ("a.png", synth.image_bytes(LINES), "image/png")}, data={"marks": "true"},
                       headers=api_key).json()["pages"][0]
    assert "signatures" not in page and "checkboxes" in page
    assert signatures._overlap((0, 0, 10, 10), (5, 0, 15, 10)) == 5 * 10 / 150


def test_the_console_can_download_a_searchable_pdf_and_a_viewer_may_too(client, need_engine):
    scan = synth.scanned_pdf([LINES])
    r = client.post("/admin/api/searchable", files={"file": ("s.pdf", scan, "application/pdf")}, headers=ADMIN)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    viewer = client.post("/admin/api/users", json={"name": "Looks only", "role": "viewer"}, headers=ADMIN).json()
    again = client.post("/admin/api/searchable", files={"file": ("s.pdf", scan, "application/pdf")},
                        headers={"X-Admin-Token": viewer["token"]})
    assert again.status_code == 200
    assert client.post("/admin/api/searchable", files={"file": ("s.pdf", scan, "application/pdf")}).status_code == 401


def test_a_nearly_square_hit_is_not_a_signature(monkeypatch):
    import numpy as np
    from app import signatures

    class Fake:
        def run(self, _, feed):
            logits = np.full((1, 100, 2), [-9.0, 9.0], np.float32)
            logits[0, 0], logits[0, 1] = [9.0, -9.0], [9.0, -9.0]
            boxes = np.zeros((1, 100, 4), np.float32)
            boxes[0, 0] = [0.5, 0.8, 0.30, 0.06]   # wide and short: a signature
            boxes[0, 1] = [0.5, 0.4, 0.20, 0.20]   # square: a watermark or a logo
            return logits, boxes

    monkeypatch.setattr(signatures, "_load", lambda: Fake())
    found = signatures.find(np.full((1000, 800, 3), 255, np.uint8))
    assert len(found) == 1 and found[0]["box"][0] == [280.0, 770.0] and found[0]["confidence"] > 0.99
    assert len(signatures.find(np.full((1000, 800, 3), 255, np.uint8), min_shape=0)) == 2


def test_jawi_characters_are_put_in_reading_order():
    from app import jawi
    def on_paper(text: str) -> str:  # the model gives a line as it sits on the paper, left to right
        return " ".join(w if w[0].isascii() else w[::-1] for w in reversed(text.split(" ")))
    for line in ("جومله بايرن اياله 250 ريڠݢيت", "سلامت داتڠ کڤد سموا", "کيرا٢ 5,000 ݢورو", "ڤد 12/03/2026 دان 14/03/2026"):
        assert jawi.logical(on_paper(line)) == line
    assert jawi.logical("RM 1,250.50") == "RM 1,250.50"  # a Latin line stays as it is
    assert jawi.logical("ناد Kuala Lumpur يد") == "دي Kuala Lumpur دان"  # Latin words keep their order
    assert jawi.logical("(تروس) 3") == "3 (سورت)"  # brackets are swapped along with the order, so they still enclose
    assert jawi.is_jawi("سلامت داتڠ", 0.9) and not jawi.is_jawi("اس", 0.95) and not jawi.is_jawi("سلامت", 0.7)
    assert not jawi.is_jawi("Tarikh 12 Mac سل", 0.99)  # mostly Latin


def test_a_page_with_jawi_is_read_when_asked(client, api_key, need_engine):
    from pathlib import Path
    png = (Path(__file__).parent / "pictures" / "jawi.png").read_bytes()
    r = client.post("/v1/ocr", files={"file": ("surat.png", png, "image/png")}, data={"engine": "jawi"}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    page = body["pages"][0]
    assert body["engine"] == "pp-ocr+jawi-kraken-v1" and "rotated" not in page
    assert client.get("/health").json()["jawi"] is True
    jawi_lines = [ln for ln in page["lines"] if ln.get("script") == "jawi"]
    assert len(jawi_lines) == 3 and all(ln["confidence"] > 0.8 for ln in jawi_lines)
    assert "سلامت داتڠ" in page["text"] and "250" in jawi_lines[-1]["text"] and "بايرن" in jawi_lines[-1]["text"]
    # the lines in Latin letters are the standard reader's, and in their place on the page
    rows = page["text"].split("\n")
    assert "Sekolah Kebangsaan Taman Melati" in rows[0] and "12 Mac 2026" in rows[-1]
    # without the option the Jawi lines are not read, and nothing is marked
    plain = client.post("/v1/ocr", files={"file": ("surat.png", png, "image/png")}, headers=api_key).json()
    assert not any("script" in ln for ln in plain["pages"][0]["lines"]) and "سلامت" not in plain["text"]
    assert "Sekolah Kebangsaan Taman Melati" in plain["text"]


def test_a_jawi_page_on_its_head_is_turned(need_engine):
    from pathlib import Path

    import cv2
    import numpy as np

    from app import engine
    img = cv2.imdecode(np.frombuffer((Path(__file__).parent / "pictures" / "jawi.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    lines, _, turned = engine.read_upright(np.ascontiguousarray(np.rot90(img, 2)), "jawi")
    assert turned == 180 and sum(1 for ln in lines if ln.get("script") == "jawi") == 3
    lines, _, turned = engine.read_upright(img, "jawi")
    assert turned == 0


def test_an_unknown_reader_is_refused(client, api_key):
    r = client.post("/v1/ocr", files={"file": ("a.png", b"x", "image/png")}, data={"engine": "klingon"}, headers=api_key)
    assert r.status_code == 422 and r.json()["error"] == "bad_engine" and "jawi" in r.json()["message"]


def test_latin_names_inside_a_jawi_line_are_read(need_engine):
    from pathlib import Path

    import cv2
    import numpy as np

    from app import engine, jawi
    img = cv2.imdecode(np.frombuffer((Path(__file__).parent / "pictures" / "jawi_mixed.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    lines = engine.read(img, "jawi")
    assert [ln.get("script") for ln in lines] == ["jawi", "jawi"]
    # in reading order: the Jawi words right to left, each Latin name where it stands among them
    assert lines[0]["text"] == "سيلا هوبوڠي Encik Ahmad د Kuala Lumpur"
    assert "Syarikat" in lines[1]["text"] and "250" in lines[1]["text"] and lines[1]["text"].startswith("ب")
    # the Jawi model alone knows no small Latin letters
    alone, _ = jawi.read_line(jawi._crop(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), lines[0]["box"]), latin=False)
    assert "Kuala" not in alone and "سيلا" in alone
    assert jawi.logical("KPDNHEP: Ghost Smoke اسقيرڤ") == "ڤريقسا KPDNHEP: Ghost Smoke"


def test_a_searchable_pdf_carries_the_jawi_text(client, api_key, need_engine):
    from pathlib import Path

    from app.documents import Document
    png = (Path(__file__).parent / "pictures" / "jawi.png").read_bytes()
    r = client.post("/v1/searchable", files={"file": ("surat.png", png, "image/png")}, data={"engine": "jawi"}, headers=api_key)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    with Document(r.content) as doc:
        text = doc.page(1).text_layer
    # found by a search for the word as it is typed, letters in reading order
    assert "سلامت داتڠ" in text and "250" in text and "Sekolah Kebangsaan Taman Melati" in text
    plain = client.post("/v1/searchable", files={"file": ("surat.png", png, "image/png")}, headers=api_key)
    with Document(plain.content) as doc:
        assert "سلامت" not in doc.page(1).text_layer and "Sekolah Kebangsaan" in doc.page(1).text_layer
    assert len(r.content) - len(plain.content) < 80_000  # the font for the hidden text is small


def test_arabic_tamil_and_thai_lines_are_read_when_asked(client, api_key, need_engine):
    from pathlib import Path

    import pytest

    from app import scripts
    wanted = {"arabic": ("250", "دينارا"), "tamil": ("மொத்த தொகை 250 ரூபாய்",), "thai": ("ยอดรวมทั้งหมด 250 บาท",)}
    for script, words in wanted.items():
        if not scripts.available(script):  # its model is fetched from the net on first use
            pytest.skip(f"the {script} model could not be fetched here")
        png = (Path(__file__).parent / "pictures" / f"{script}.png").read_bytes()
        r = client.post("/v1/ocr", files={"file": (f"{script}.png", png, "image/png")}, data={"engine": script}, headers=api_key)
        assert r.status_code == 200, r.text
        page = r.json()["pages"][0]
        marked = [ln for ln in page["lines"] if ln.get("script") == script]
        assert len(marked) == 2 and r.json()["engine"] == "pp-ocr+" + scripts.name(script)
        assert all(word in marked[1]["text"] for word in words)
        # the two lines in Latin letters are the standard reader's, as without the option
        assert "Invoice No. 2026/0417" in page["text"] and "Date: 12 March 2026" in page["text"]


def test_a_scrap_read_inside_an_unread_line_does_not_hide_the_line():
    long_line = [[10, 10], [500, 10], [500, 40], [10, 40]]
    scrap = [[200, 12], [230, 12], [230, 38], [200, 38]]
    assert engine._same_place(long_line, scrap) is False  # the long line is still to be read
    assert engine._same_place(scrap, long_line) is True
    assert engine._same_place(long_line, [[12, 9], [498, 9], [498, 41], [12, 41]]) is True


def test_an_order_slip_keeps_its_quantities_strokes_and_tick_brackets(client, api_key, need_engine, fake_model):  # noqa: F811
    slip = synth.order_slip(struck=(0, 2), ticked=(3,))
    r = client.post("/v1/ocr", files={"file": ("slip.png", slip, "image/png")}, data={"marks": "true"}, headers=api_key)
    assert r.status_code == 200, r.text
    page = r.json()["pages"][0]
    rows = page["text"].split("\n")
    # every item with its quantity, also the two whose "1" has a pen stroke through it; no stray "L" from a bracket
    assert rows[3:8] == ["1 Stir-Fried Vege Sawi", "1 Vegetarian Fried Rice", "1 Quarter Roasted Chicken",
                         "1 Siu Mai King", "2 Filtered Water"]
    assert [ln["text"] for ln in page["lines"] if ln.get("struck")] == ["1 Stir-Fried Vege Sawi", "1 Quarter Roasted Chicken"]
    boxes = [c for c in page["checkboxes"] if c.get("kind") == "brackets"]
    assert len(boxes) == 5 and [c["checked"] for c in boxes] == [False, False, False, True, False]
    assert "Siu Mai King" in boxes[3]["label"] and "Filtered Water" in boxes[4]["label"]
    # as fields: the model reads the text; the stroke and the tick come from the paper
    answers, _ = fake_model
    answers.append({"title": "Order List", "order_number": None, "table": "2A", "guests": None, "date": "2026-11-25",
                    "time": "8:19 PM", "staff": None, "service_type": None,
                    "items": [{"quantity": 1, "name": "Stir-Fried Vege Sawi", "short_name": None, "notes": None},
                              {"quantity": 1, "name": "Vegetarian Fried Rice", "short_name": None, "notes": None},
                              {"quantity": None, "name": "Quarter Roasted Chicken", "short_name": None, "notes": None},
                              {"quantity": 1, "name": "Siu Mai King", "short_name": None, "notes": None},
                              {"quantity": 2, "name": "Filtered Water", "short_name": None, "notes": None}]})
    body = client.post("/v1/extract", files={"file": ("slip.png", slip, "image/png")}, data={"type": "order"}, headers=api_key).json()
    items = body["fields"]["items"]
    assert [(it["quantity"], it["struck"], it["ticked"]) for it in items] == [
        (1, True, False), (1, False, False), (1, True, False), (1, False, True), (2, False, False)]
    assert body["checks"][0]["passed"] is True  # the quantity the model left out was taken from the line


def test_a_plain_page_gets_no_quantities_or_strokes(need_engine):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(synth.image_bytes(LINES), np.uint8), cv2.IMREAD_COLOR)
    lines = engine.read(img)
    assert not any(ln.get("struck") for ln in lines)
    from app import lists
    assert [ln["text"] for ln in lists.leading_quantities(img, lines)] == [ln["text"] for ln in lines]
    assert lists.brackets(img, lines) == []


def test_a_stroke_in_a_pen_as_dark_as_the_print_is_seen_too(need_engine):
    import cv2
    import numpy as np
    for struck in ((0, 2), ()):
        img = cv2.imdecode(np.frombuffer(synth.order_slip(struck=struck, ticked=(), pen=(40, 45, 60)), np.uint8), cv2.IMREAD_COLOR)
        lines = engine.read(img)
        rows = engine.layout_text(lines).split("\n")
        assert rows[3:8] == ["1 Stir-Fried Vege Sawi", "1 Vegetarian Fried Rice", "1 Quarter Roasted Chicken",
                             "1 Siu Mai King", "2 Filtered Water"]
        assert [ln["text"] for ln in lines if ln.get("struck")] == [
            "1 Stir-Fried Vege Sawi", "1 Quarter Roasted Chicken"][:len(struck)]


def test_the_script_of_a_page_is_found_without_being_told(client, api_key, need_engine):
    from pathlib import Path

    import pytest

    from app import scripts
    if not all(scripts.available(s) for s in scripts.SCRIPTS):
        pytest.skip("the script models could not be fetched here")
    for name, want in (("jawi", "jawi"), ("jawi_mixed", "jawi"), ("arabic", "arabic"), ("tamil", "tamil"), ("thai", "thai")):
        png = (Path(__file__).parent / "pictures" / f"{name}.png").read_bytes()
        r = client.post("/v1/ocr", files={"file": (f"{name}.png", png, "image/png")}, data={"engine": "detect"}, headers=api_key)
        assert r.status_code == 200, r.text
        assert {ln["script"] for ln in r.json()["pages"][0]["lines"] if ln.get("script")} == {want}, name
    # a page in Latin letters is read as by the standard reader, and nothing is marked
    plain = client.post("/v1/ocr", files={"file": ("p.png", synth.image_bytes(LINES), "image/png")}, data={"engine": "detect"}, headers=api_key)
    usual = client.post("/v1/ocr", files={"file": ("p.png", synth.image_bytes(LINES), "image/png")}, headers=api_key)
    assert plain.json()["text"] == usual.json()["text"] and not any("script" in ln for ln in plain.json()["pages"][0]["lines"])


def test_tamil_and_thai_text_is_carried_in_the_searchable_pdf(client, api_key, need_engine):
    from pathlib import Path

    import pytest

    from app import scripts
    from app.documents import Document
    for script, words in (("tamil", "மொத்த தொகை 250 ரூபாய்"), ("thai", "ยอดรวมทั้งหมด 250 บาท")):
        if not scripts.available(script):
            pytest.skip(f"the {script} model could not be fetched here")
        png = (Path(__file__).parent / "pictures" / f"{script}.png").read_bytes()
        r = client.post("/v1/searchable", files={"file": (f"{script}.png", png, "image/png")}, data={"engine": script}, headers=api_key)
        with Document(r.content) as doc:
            assert words in doc.page(1).text_layer and "Invoice No. 2026/0417" in doc.page(1).text_layer


def test_a_protected_pdf_opens_with_its_password_and_the_password_is_not_kept(client, api_key):
    from pathlib import Path
    locked = (Path(__file__).parent / "pictures" / "locked.pdf").read_bytes()
    r = client.post("/v1/ocr", files={"file": ("p.pdf", locked, "application/pdf")}, headers=api_key)
    assert r.status_code == 422 and r.json()["error"] == "pdf_protected" and "pdf_password" in r.json()["message"]
    wrong = client.post("/v1/ocr", files={"file": ("p.pdf", locked, "application/pdf")}, data={"pdf_password": "salah"}, headers=api_key)
    assert wrong.status_code == 422 and wrong.json()["error"] == "pdf_protected"
    ok = client.post("/v1/ocr", files={"file": ("p.pdf", locked, "application/pdf")}, data={"pdf_password": "rahsia123"}, headers=api_key)
    assert ok.status_code == 200 and "baki akhir RM 1,250.80" in ok.json()["text"] and "rahsia123" not in ok.text
    # queued: the file is opened before it is stored, so the job carries no password
    job = client.post("/v1/jobs", files={"file": ("p.pdf", locked, "application/pdf")}, data={"pdf_password": "rahsia123"}, headers=api_key)
    assert job.status_code in (200, 202) and "rahsia123" not in job.text
    from app.db import Job, session_scope
    with session_scope() as db:
        assert "rahsia123" not in str(db.get(Job, job.json()["id"]).options)
    # a password sent with a file that needs none changes nothing
    plain = client.post("/v1/ocr", files={"file": ("t.pdf", synth.text_pdf("Tiada kata laluan"), "application/pdf")},
                        data={"pdf_password": "apa-apa"}, headers=api_key)
    assert plain.status_code == 200 and "Tiada kata laluan" in plain.json()["text"]


def test_an_iphone_photo_in_heic_is_read(client, api_key, need_engine):
    import io

    from PIL import Image
    heic = io.BytesIO()
    Image.open(io.BytesIO(synth.image_bytes(LINES))).save(heic, format="HEIF")
    assert heic.getvalue()[4:12] == b"ftypheic"
    r = client.post("/v1/ocr", files={"file": ("IMG_0001.HEIC", heic.getvalue(), "image/heic")}, headers=api_key)
    assert r.status_code == 200 and r.json()["kind"] == "image" and LINES[0] in r.json()["text"]


def test_word_and_excel_files_are_read_from_their_own_text(client, api_key):
    from pathlib import Path
    docx = (Path(__file__).parent / "pictures" / "quote.docx").read_bytes()
    r = client.post("/v1/ocr", files={"file": ("quote.docx", docx, "application/octet-stream")}, headers=api_key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "docx" and body["page_count"] == 1 and body["pages"][0]["source"] == "document"
    rows = body["text"].split("\n")
    assert rows[0] == "Sebut Harga" and "Kertas A4    10    125.50" in rows and rows[-1] == "Jumlah: RM 365.50"  # a table row stays a row
    xlsx = (Path(__file__).parent / "pictures" / "ledger.xlsx").read_bytes()
    body = client.post("/v1/ocr", files={"file": ("ledger.xlsx", xlsx, "application/octet-stream")}, headers=api_key).json()
    assert body["kind"] == "xlsx" and body["page_count"] == 2  # a sheet is a page
    first = body["pages"][0]["text"].split("\n")
    assert first[0] == "Jan" and first[2].split() == ["2026-01-02", "RENT", "JAN", "1200.8", "2799.2"]  # a date, not a day count
    assert "Tiada transaksi" in body["pages"][1]["text"]
    # asked for what needs a picture, it says so
    bad = client.post("/v1/searchable", files={"file": ("quote.docx", docx, "application/octet-stream")}, headers=api_key)
    assert bad.status_code == 422 and "searchable already" in bad.json()["message"]
    assert client.post("/v1/ocr", files={"file": ("q.docx", docx, "x/y")}, data={"marks": "true", "codes": "true"}, headers=api_key).status_code == 200


def test_an_office_file_that_is_not_what_it_seems_is_refused():
    import io
    import zipfile

    import pytest

    from app import office
    from app.errors import ApiError
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
    assert office.kind(bomb.getvalue()) == "docx"
    with pytest.raises(ApiError) as err:
        office.pages(bomb.getvalue(), "docx")
    assert err.value.code == "bad_file"
    assert office.kind(b"PK\x03\x04 not a zip") is None and office.kind(b"%PDF-1.7") is None


def test_a_tilted_page_is_made_level_before_it_is_read(need_engine):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(synth.image_bytes(INVOICE_LINES), np.uint8), cv2.IMREAD_COLOR)
    level_rows = engine.layout_text(engine.read(img)).split("\n")
    for lean in (5.0, -8.0):
        tilted = engine.straighten(img, -lean)  # a page leaning `lean` degrees clockwise
        lines, upright, turned = engine.read_upright(tilted)
        assert abs(((turned + 180) % 360 - 180) + lean) < 1.0  # turned back by about what it leaned
        assert abs(engine.tilt(lines)) < 1.5
        rows = engine.layout_text(lines).split("\n")
        assert len(set(rows) & set(level_rows)) >= 0.8 * len(level_rows)  # each printed row is a row again
    # a level page is left alone, and so is a page that only has a few short lines
    assert engine.read_upright(img)[2] == 0
    assert engine.tilt([{"text": "ab", "box": [[0, 0], [30, 9], [30, 19], [0, 10]]}] * 5) == 0.0
