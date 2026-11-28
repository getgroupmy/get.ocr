"""Made-up documents for the tests."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
         "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf")


def _font(size: int):
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def page_image(lines: list[str]) -> Image.Image:
    im = Image.new("RGB", (1100, 120 + 80 * len(lines)), "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((50, 50 + 80 * i), line, fill="black", font=_font(38))
    return im


def identity_card(name=("LIM AH KOW",), address=("NO 29", "JALAN WATAN 11", "TAMAN SRI WATAN", "68000 AMPANG", "SELANGOR"),
                  number="850412-10-5759", right=("WARGANEGARA", "LELAKI")) -> Image.Image:
    """A made-up identity card of the older design: a light-blue card, its heading, the number at the far left, the
    name under where the chip is, the address at the foot, the citizenship and the gender at the lower right."""
    im = Image.new("RGB", (1280, 806), (176, 214, 238))
    d = ImageDraw.Draw(im)
    for xy, text, size in (((300, 28), "KAD PENGENALAN", 34), ((300, 70), "MALAYSIA", 50), ((300, 128), "IDENTITY CARD", 30), ((1010, 60), "MyKad", 50)):
        d.text(xy, text, fill=(30, 40, 110), font=_font(size))
    d.text((40, 170), number, fill=(20, 20, 24), font=_font(56))
    d.rounded_rectangle((60, 280, 220, 400), 14, fill=(214, 186, 96))  # the chip
    d.rectangle((880, 190, 1220, 620), fill=(150, 160, 170))  # the photo
    for n, line in enumerate(name):
        d.text((42, 500 + 46 * n), line, fill=(20, 20, 24), font=_font(40))
    for n, line in enumerate(address):
        d.text((43, 766 - 36 * (len(address) - n)), line, fill=(20, 20, 24), font=_font(30))
    for n, word in enumerate(right):
        d.text((1230, 766 - 36 * (len(right) - n)), word, fill=(20, 20, 24), font=_font(30), anchor="ra")
    return im


def on_a_table(card: Image.Image, degrees: float = 0.0) -> Image.Image:
    """The card as a phone photographs it lying on a table, turned by `degrees` (anticlockwise)."""
    table = Image.new("RGB", (1500, 1000), (118, 96, 74))
    turned = card.convert("RGBA").rotate(degrees, expand=True, resample=Image.BICUBIC)
    table.paste(turned, ((1500 - turned.width) // 2, (1000 - turned.height) // 2), turned)
    return table


def stamped(picture: Image.Image, words=("GET RIDE", "Registration"), at=(260, 520), degrees: float = 18.0,
            ink=(230, 50, 50), solid: int = 255, size: int = 64) -> Image.Image:
    """The picture with words laid across it at an angle, as an app stamps its name over a photo it keeps: the
    middle of the words at `at`, in an ink `solid` parts in 255 strong."""
    layer = Image.new("RGBA", (900, 90 * len(words) + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for n, word in enumerate(words):
        d.text((10, 10 + 84 * n), word, fill=ink + (solid,), font=_font(size))
    box = layer.getbbox()
    turned = layer.crop(box).rotate(degrees, expand=True, resample=Image.BICUBIC)
    out = picture.convert("RGBA")
    out.alpha_composite(turned, (max(at[0] - turned.width // 2, 0), max(at[1] - turned.height // 2, 0)))
    return out.convert("RGB")


def png(picture: Image.Image) -> bytes:
    buf = io.BytesIO()
    picture.save(buf, "PNG")
    return buf.getvalue()


def image_bytes(lines: list[str], fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    page_image(lines).save(buf, fmt)
    return buf.getvalue()


def scanned_pdf(pages: list[list[str]]) -> bytes:
    """A PDF of pictures, as a scanner makes: no text layer."""
    ims = [page_image(p) for p in pages]
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:])
    return buf.getvalue()


def text_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer (Helvetica)."""
    stream = f"BT /F1 18 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def letterhead_pdf(picture_lines: list[str], text: str) -> bytes:
    """A one-page PDF with stored text and, above it, a picture that holds words of its own."""
    im = page_image(picture_lines).convert("RGB")
    im = im.crop((0, 0, im.width, min(im.height, 160)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    jpeg = buf.getvalue()
    w = 500
    h = round(w * im.height / im.width)
    stream = f"q {w} 0 0 {h} 50 {740 - h} cm /Im1 Do Q BT /F1 14 Tf 50 500 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
        b"/Filter /DCTDecode /Length %d >>\nstream\n%s\nendstream" % (im.width, im.height, len(jpeg), jpeg),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _pdf(streams: list[bytes]) -> bytes:
    """A PDF with one page per content stream (Helvetica as F1, A4)."""
    n = len(streams)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(n)) + b"] /Count %d >>" % n,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, stream in enumerate(streams):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * i))
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _number_width(text: str, size: float) -> float:
    return sum(278 if c in ",. " else 333 if c == "-" else 556 for c in text) * size / 1000


def _money(v: float) -> str:
    return f"{v:,.2f}"


def bank_statement(pages: int = 3, per_page: int = 14, seed: int = 7) -> tuple[bytes, dict]:
    """A made-up current-account statement with a text layer, laid out like a
    Malaysian bank prints one: deposit before withdrawal, the balance only on
    the last line of each day, second lines of description, English and Malay
    headings. Returns the PDF and what a correct reading gives."""
    import random
    rnd = random.Random(seed)
    size, x_date, x_desc, r_dep, r_wd, r_bal = 8.0, 40, 100, 400, 480, 560
    payees = ["JomPAY Bill Payment at DIO", "IBG Transfer to ZETA MOTORS", "DuitNow from NUR AINA BINTI OMAR",
              "Cheque Deposit", "Instant Transfer to KEDAI ALAT GANTI", "Service Charge", "FPX Payment TNB",
              "Inward IBG LIM AUTO TRADING", "Cash Deposit CDM", "Standing Instruction HP"]
    balance = opening = 188167.56
    txns, streams, day = [], [], 2
    for page in range(1, pages + 1):
        ops, y = [], 800.0

        def put(x, text, right=False):  # noqa: ANN001
            xx = x - _number_width(text, size) if right else x
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F1 {size} Tf 1 0 0 1 {xx:.2f} {y:.2f} Tm ({safe}) Tj ET")

        put(x_date, "387ff303acfb57c73e9ab3b409cff151"); y -= 12
        put(x_date, "HLB PRIMEBIZ CURRENT ACCOUNT"); put(330, "Hong Leong Bank Berhad (97141-X)"); y -= 12
        put(x_date, "Protected by PIDM up to RM250,000 for each depositor."); y -= 12
        put(330, f"Page No / No Mukasurat : {page} of {pages}"); y -= 12
        put(x_date, "DELTA KARGO SDN. BHD."); put(330, "Date / Tarikh : 31-01-2026"); y -= 12
        put(x_date, "NO.9 JALAN SERI 3"); put(330, "A/C No / No Akaun : 02800999123 MYR"); y -= 12
        put(x_date, "43000 KAJANG"); put(330, "Statement Period / Tempoh Penyataan : 01/01/26 - 31/01/26"); y -= 24
        for heads in (("Date", "Transaction Description", "Deposit", "Withdrawal", "Balance"),
                      ("Tarikh", "Deskripsi Transaksi", "Simpanan", "Pengeluaran", "Baki")):
            put(x_date, heads[0]); put(x_desc, heads[1])
            for xr, word in zip((r_dep, r_wd, r_bal), heads[2:]):
                put(xr - len(word) * size * 0.5, word)
            y -= 12
        if page == 1:
            put(x_desc, "Balance from previous statement"); put(r_bal, _money(opening), right=True); y -= 12
        for i in range(per_page):
            credit = rnd.random() < 0.35
            amount = round(rnd.uniform(20, 9000), 2)
            balance = round(balance + amount if credit else balance - amount, 2)
            desc = rnd.choice(payees)
            last_of_day = rnd.random() < 0.45 or i == per_page - 1
            iso = f"2026-01-{day:02d}"
            put(x_date, f"{day:02d}-01-2026"); put(x_desc, desc)
            put(r_dep if credit else r_wd, _money(amount), right=True)
            if last_of_day:
                put(r_bal, _money(balance), right=True)
            y -= 11
            more = ""
            if rnd.random() < 0.6:
                more = rnd.choice(["REF 2026010" + str(rnd.randint(1000, 9999)), "INV 4471 PARTS", "Biller Code 5454"])
                put(x_desc, more); y -= 11
            txns.append({"date": iso, "description": (desc + " " + more).strip(), "reference": None,
                         "debit": None if credit else amount, "credit": amount if credit else None,
                         "balance": balance if last_of_day else None})
            if last_of_day and day < 30:
                day += 1
        if page == pages:
            y -= 4
            put(x_desc, "Closing Balance / Baki Akhir"); put(r_bal, _money(balance), right=True); y -= 12
            put(x_desc, "Total Withdrawals / Jumlah Pengeluaran")
            put(r_wd, _money(sum(t["debit"] or 0 for t in txns)), right=True); y -= 12
            put(x_desc, "Total Deposits / Jumlah Simpanan")
            put(r_dep, _money(sum(t["credit"] or 0 for t in txns)), right=True); y -= 12
        y = 60
        put(x_date, "Please examine this statement and notify the bank of any discrepancy within 14 days.")
        y = 48
        put(x_date, "Hong Leong Bank Berhad. Customer service 03-7626 8899. This is a made-up statement for testing.")
        streams.append("\n".join(ops).encode("latin-1"))
    return _pdf(streams), {"opening_balance": opening, "closing_balance": balance, "transactions": txns}


def qr_page(text: str, lines: list[str]) -> bytes:
    """A page picture with some lines of text and a QR code that says `text`."""
    import cv2
    import numpy as np
    im = page_image(lines + ["", "", "", ""])
    qr = cv2.resize(cv2.QRCodeEncoder.create().encode(text), None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    arr = np.array(im)
    y = arr.shape[0] - qr.shape[0] - 20
    arr[y:y + qr.shape[0], 700:700 + qr.shape[1]] = qr[:, :, None]
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def order_slip(struck=(0, 2), ticked=(3,), pen=(170, 70, 30)):
    """A kitchen order slip as a photo would show it: each quantity stands alone in front of its
    item, a blue pen stroke crosses some, and a [ ] ends every row. Returns the PNG bytes."""
    import cv2
    import numpy as np
    img = np.full((760, 900, 3), 245, np.uint8)
    font, ink = cv2.FONT_HERSHEY_DUPLEX, (40, 45, 60)
    for y, text in ((70, "** Order List **"), (120, "Table No: 2A"), (170, "Date : 25/11/2026 8:19 PM")):
        cv2.putText(img, text, (60, y), font, 1.0, ink, 2, cv2.LINE_AA)
    items = [("1", "Stir-Fried Vege Sawi"), ("1", "Vegetarian Fried Rice"), ("1", "Quarter Roasted Chicken"),
             ("1", "Siu Mai King"), ("2", "Filtered Water")]
    for n, (qty, name) in enumerate(items):
        y = 260 + 60 * n
        cv2.putText(img, qty, (62, y), font, 1.0, ink, 2, cv2.LINE_AA)
        cv2.putText(img, name, (130, y), font, 1.0, ink, 2, cv2.LINE_AA)
        cv2.putText(img, "[", (760, y), font, 1.0, ink, 2, cv2.LINE_AA)
        cv2.putText(img, "]", (830, y), font, 1.0, ink, 2, cv2.LINE_AA)
        if n in struck:
            cv2.line(img, (30, y + 22), (105, y - 40), pen, 4, cv2.LINE_AA)  # a blue pen, unless told otherwise
        if n in ticked:
            cv2.line(img, (785, y - 12), (797, y), pen, 4, cv2.LINE_AA)
            cv2.line(img, (797, y), (820, y - 28), pen, 4, cv2.LINE_AA)
    cv2.putText(img, "END", (380, 600), font, 1.0, ink, 2, cv2.LINE_AA)
    return cv2.imencode(".png", img)[1].tobytes()
