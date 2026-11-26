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
