"""The catalogue of documents the model reads in one pass: for each kind its
fields, its table of lines if it has one, what can be checked, and the words
`auto` knows it by.

Fields are written as "name, name:n, name:d" (text, number, date). A check is
a tuple; `run_checks` works them out:

  ("sum", name, "rows.column", "total" or ("subtotal", "total"))  the column adds up to one of the fields
  ("add", name, ["+a", "-b", ...], "total")       the fields come to the total (missing ones count as 0)
  ("ledger", name, "opening", "rows.debit", "rows.credit", "closing")
  ("order", name, "earlier_date", "later_date")   the first date is not after the second
  ("days", name, "start", "end", "days")          the number of days from start to end, both counted
  ("format", name, "field", regex, "what it should look like")
  ("words", name, "amount_in_words", "amount")    the amount in words equals the figure
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

TOLERANCE = 0.05
_KINDS = {"": "string", "n": "number", "d": "date"}


def _fields(spec: str) -> dict:
    out = {}
    for part in spec.replace("\n", " ").split(","):
        name, _, kind = part.strip().partition(":")
        if name:
            out[name] = _KINDS[kind]
    return out


def _doc(label: str, what: str, fields: str, rows: Optional[tuple] = None, checks: tuple = (), words: str = "",
         hints: str = "") -> dict:
    return {"label": label, "what": what, "fields": _fields(fields),
            "rows": (rows[0], _fields(rows[1])) if rows else None, "checks": checks,
            "words": re.compile(words, re.I) if words else None, "hints": hints}


DOCS: dict[str, dict] = {
    # --- vehicles and drivers ---------------------------------------------------
    "vehicle_registration": _doc(
        "Vehicle registration card (VOC, geran)", "a vehicle ownership or registration certificate",
        "plate_number, chassis_number, engine_number, make, model, year_made:n, engine_capacity_cc:n, fuel_type, colour, "
        "owner_name, owner_id_number, owner_address, registration_date:d, ownership_claim",
        checks=(("format", "chassis_number", "chassis_number", r"[A-Z0-9]{8,17}", "8 to 17 letters and digits"),),
        words=r"sijil pemilikan kenderaan|vehicle ownership certificate|no\.? casis|no\.? enjin|chassis no|engine no|"
              r"keupayaan enjin|hak milik",
        hints="ownership_claim is the bank or financier with a claim on the vehicle, when one is printed."),
    "road_tax": _doc(
        "Road tax (motor vehicle licence)", "a road tax disc or motor vehicle licence receipt",
        "plate_number, owner_name, receipt_number, period_start:d, expiry_date:d, amount:n, currency",
        checks=(("order", "period", "period_start", "expiry_date"),),
        words=r"lesen kenderaan motor|road ?tax|cukai jalan|\blkm\b|tarikh luput|motor vehicle licence"),
    "motor_insurance": _doc(
        "Motor insurance cover note or schedule", "a motor insurance or takaful cover note, certificate or policy schedule",
        "insurer_name, policy_number, cover_note_number, insured_name, id_number, plate_number, chassis_number, "
        "engine_number, make_model, cover_type, period_start:d, period_end:d, sum_insured:n, basic_premium:n, "
        "ncd_percent:n, ncd_amount:n, gross_premium:n, service_tax:n, stamp_duty:n, total_payable:n, currency",
        checks=(("order", "period", "period_start", "period_end"),
                ("add", "total_payable", ["+gross_premium", "+service_tax", "+stamp_duty"], "total_payable")),
        words=r"cover note|nota perlindungan|sum insured|jumlah diinsuranskan|\bncd\b|no claim discount|policy no|"
              r"certificate of insurance|sijil insurans|takaful|the insured|premium",
        hints="gross_premium is the premium after the no-claim discount and before tax and stamp duty."),
    "vehicle_inspection": _doc(
        "Vehicle inspection report", "a vehicle inspection report (for example from Puspakom)",
        "centre_name, report_number, plate_number, chassis_number, inspection_type, inspection_date:d, result, "
        "expiry_date:d, mileage_km:n",
        words=r"puspakom|laporan pemeriksaan|inspection report|pemeriksaan kenderaan|vehicle inspection|lulus|gagal"),
    "driver_permit": _doc(
        "E-hailing permit or PSV licence", "an e-hailing vehicle permit (EVP) or a vocational driving licence (PSV)",
        "permit_type, permit_number, holder_name, id_number, plate_number, operator_name, issue_date:d, expiry_date:d",
        checks=(("order", "validity", "issue_date", "expiry_date"),),
        words=r"e-?hailing|\bevp\b|\bpsv\b|permit kenderaan|lesen vokasional|vocational licence|\bapad\b",
        hints="operator_name is the e-hailing company the permit is tied to."),
    "traffic_summons": _doc(
        "Traffic summons or compound", "a traffic summons, compound notice or parking fine",
        "issuer_name, notice_number, plate_number, offender_name, offence, offence_date:d, offence_time, location, "
        "amount:n, pay_by_date:d, currency",
        checks=(("order", "dates", "offence_date", "pay_by_date"),),
        words=r"\bsaman\b|kompaun|notis kesalahan|summons|compound|kesalahan trafik|traffic offence|pol\.? ?170"),
    "vehicle_agreement": _doc(
        "Vehicle rental or hire-purchase agreement", "a vehicle rental, lease or hire-purchase agreement",
        "agreement_number, agreement_type, owner_name, hirer_name, hirer_id_number, plate_number, vehicle, "
        "agreement_date:d, start_date:d, end_date:d, term_months:n, periodic_amount:n, payment_frequency, deposit:n, "
        "total_amount:n, currency",
        checks=(("order", "term", "start_date", "end_date"),),
        words=r"hire[ -]purchase|sewa beli|rental agreement|perjanjian sewa kenderaan|car rental|the hirer|penyewa|"
              r"the owner|lessor",
        hints="owner_name is the owner, lessor or financier; hirer_name the person renting or buying. "
              "periodic_amount is the rent or instalment per period and payment_frequency that period (monthly, weekly)."),
    "vehicle_handover": _doc(
        "Vehicle handover or condition checklist", "a vehicle handover, return or condition checklist",
        "form_number, plate_number, vehicle, driver_name, handover_type, date:d, time, mileage_km:n, fuel_level, remarks",
        rows=("items", "item, condition, remarks"),
        words=r"handover|serah terima|vehicle checklist|condition report|senarai semak kenderaan|fuel level|mileage"),
    # --- bookkeeping and finance ------------------------------------------------
    "supplier_statement": _doc(
        "Supplier statement of account", "a statement of account from a supplier listing invoices and payments",
        "supplier_name, customer_name, account_number, statement_date:d, currency, opening_balance:n, total_due:n",
        rows=("lines", "date:d, reference, description, debit:n, credit:n, balance:n"),
        checks=(("ledger", "total_due", "opening_balance", "lines.debit", "lines.credit", "total_due"),),
        hints="debit is an amount charged (an invoice), credit an amount paid or credited."),
    "payment_voucher": _doc(
        "Payment voucher", "a payment voucher",
        "voucher_number, date:d, payer_name, payee_name, amount:n, amount_in_words, currency, payment_method, "
        "reference, purpose, prepared_by, approved_by",
        checks=(("words", "amount_in_words", "amount_in_words", "amount"),),
        words=r"payment voucher|baucar bayaran|\bpv no|voucher no|pay to|bayar kepada|approved by|diluluskan"),
    "official_receipt": _doc(
        "Official receipt", "an official receipt acknowledging money received",
        "issuer_name, receipt_number, date:d, received_from, amount:n, amount_in_words, currency, payment_method, "
        "reference, payment_for",
        checks=(("words", "amount_in_words", "amount_in_words", "amount"),),
        words=r"official receipt|resit rasmi|received from|diterima daripada|the sum of|sejumlah|being payment"),
    "remittance_advice": _doc(
        "Bank transfer slip or remittance advice", "a bank transfer confirmation, payment slip or remittance advice",
        "bank_name, reference_number, date:d, time, payer_name, payer_account, payee_name, payee_account, payee_bank, "
        "amount:n, currency, status, purpose",
        words=r"duitnow|instant transfer|\bibg\b|interbank giro|reference no|transaction (?:successful|status)|"
              r"remittance advice|beneficiary|penerima|recipient reference|fund transfer"),
    "cheque": _doc(
        "Cheque", "a cheque",
        "bank_name, cheque_number, date:d, payee_name, amount:n, amount_in_words, currency, account_number, drawer_name",
        checks=(("words", "amount_in_words", "amount_in_words", "amount"),),
        words=r"\bcheque\b|\bcek\b|or bearer|atau pembawa|a/c payee|pay\b.*\bor bearer"),
    "loan_statement": _doc(
        "Loan or financing statement", "a loan, financing or hire-purchase account statement",
        "lender_name, borrower_name, account_number, statement_date:d, period_start:d, period_end:d, "
        "opening_balance:n, payments:n, interest_charged:n, other_charges:n, closing_balance:n, instalment_amount:n, "
        "arrears:n, next_due_date:d, currency",
        checks=(("add", "closing_balance", ["+opening_balance", "+interest_charged", "+other_charges", "-payments"],
                 "closing_balance"),),
        words=r"loan statement|penyata pinjaman|penyata pembiayaan|financing statement|outstanding balance|"
              r"baki tertunggak|instalment|ansuran|interest charged|keuntungan",
        hints="opening_balance and closing_balance are the amount owed at the start and end of the period; payments "
              "is the total paid in the period, as a positive number."),
    "expense_claim": _doc(
        "Expense or petty cash claim", "an expense claim or petty cash claim form",
        "claimant_name, department, claim_number, claim_date:d, period, currency, total:n, approved_by",
        rows=("lines", "date:d, description, category, amount:n"),
        checks=(("sum", "lines_sum", "lines.amount", "total"),),
        words=r"expense claim|tuntutan perbelanjaan|petty cash|wang runcit|claim form|borang tuntutan|claimant|penuntut"),
    # --- tax, statutory and payroll ---------------------------------------------
    "ea_form": _doc(
        "EA form (annual pay statement)", "an EA form: the yearly statement of remuneration an employer gives an employee",
        "employer_name, employer_number, employee_name, id_number, tax_number, year:n, gross_remuneration:n, "
        "benefits_in_kind:n, total_income:n, epf_employee:n, socso_employee:n, eis_employee:n, pcb:n, zakat:n, currency",
        words=r"borang ea|\bc\.?p\.? ?8a\b|penyata saraan|statement of remuneration|jumlah saraan|lembaga hasil"),
    "contribution_statement": _doc(
        "EPF, SOCSO or EIS contribution statement", "a monthly EPF (KWSP), SOCSO (PERKESO) or EIS contribution statement",
        "scheme, employer_name, employer_number, contribution_month, payment_date:d, reference, total_employer:n, "
        "total_employee:n, total_amount:n, currency",
        rows=("lines", "employee_name, id_number, member_number, wages:n, employer_amount:n, employee_amount:n"),
        checks=(("sum", "employer_sum", "lines.employer_amount", "total_employer"),
                ("sum", "employee_sum", "lines.employee_amount", "total_employee"),
                ("add", "total_amount", ["+total_employer", "+total_employee"], "total_amount")),
        words=r"borang a\b|caruman|contribution|\bkwsp\b|\bepf\b|perkeso|socso|\bsip\b|\beis\b|syer majikan|syer pekerja",
        hints="scheme is EPF, SOCSO or EIS. One line per employee."),
    "pcb_statement": _doc(
        "PCB / CP39 statement", "a monthly tax deduction (PCB, CP39) statement",
        "employer_name, employer_number, month, payment_date:d, reference, total_pcb:n, total_cp38:n, total_amount:n, "
        "currency",
        rows=("lines", "employee_name, tax_number, id_number, pcb_amount:n, cp38_amount:n"),
        checks=(("sum", "pcb_sum", "lines.pcb_amount", "total_pcb"),
                ("add", "total_amount", ["+total_pcb", "+total_cp38"], "total_amount")),
        words=r"\bc\.?p\.? ?39\b|potongan cukai bulanan|\bpcb\b|\bmtd\b|monthly tax deduction|\bcp ?38\b"),
    "sst_return": _doc(
        "SST return or payment receipt", "a sales and service tax (SST-02) return or its payment receipt",
        "registrant_name, sst_number, period_start:d, period_end:d, due_date:d, taxable_value:n, tax_payable:n, "
        "penalty:n, total_payable:n, payment_reference, currency",
        checks=(("order", "period", "period_start", "period_end"),
                ("add", "total_payable", ["+tax_payable", "+penalty"], "total_payable")),
        words=r"sst-?02|cukai jualan|cukai perkhidmatan|sales tax|service tax|tempoh bercukai|taxable period|"
              r"jabatan kastam"),
    "tax_notice": _doc(
        "Tax assessment notice", "a notice of assessment or a demand from the tax authority",
        "authority_name, taxpayer_name, tax_number, notice_type, reference, year_of_assessment:n, notice_date:d, "
        "amount_payable:n, due_date:d, currency",
        checks=(("order", "dates", "notice_date", "due_date"),),
        words=r"notis taksiran|notice of assessment|tahun taksiran|year of assessment|lembaga hasil dalam negeri|"
              r"cukai kena dibayar|tax payable"),
    # --- company and legal --------------------------------------------------------
    "company_profile": _doc(
        "Company profile (SSM)", "a company or business profile from the companies registry (SSM)",
        "company_name, registration_number, old_registration_number, company_type, status, incorporation_date:d, "
        "registered_address, business_address, nature_of_business, paid_up_capital:n, currency",
        rows=("people", "name, id_number, role, appointed_date:d, shares:n"),
        checks=(("format", "registration_number", "registration_number", r"\d{12}|\d{5,7}-?[A-Z]",
                 "12 digits, or the old form 123456-X"),),
        words=r"suruhanjaya syarikat|companies commission|\bssm\b|company profile|profil syarikat|no\.? pendaftaran|"
              r"registration no|nature of business|jenis perniagaan|pengarah|directors?",
        hints="people lists directors, shareholders and the secretary, one entry each; role says which."),
    "business_licence": _doc(
        "Business licence", "a business or premises licence from a local council or authority",
        "authority_name, licence_number, holder_name, business_name, premises_address, activity, issue_date:d, "
        "expiry_date:d, fee:n, currency",
        checks=(("order", "validity", "issue_date", "expiry_date"),),
        words=r"lesen perniagaan|business licence|lesen premis|majlis (?:perbandaran|bandaraya|daerah)|dewan bandaraya|"
              r"no\.? lesen|licen[cs]e no"),
    "contract": _doc(
        "Contract or agreement", "a contract or agreement between two parties",
        "title, party_a, party_b, agreement_date:d, start_date:d, end_date:d, contract_value:n, currency, payment_terms, "
        "termination_notice, governing_law",
        checks=(("order", "term", "start_date", "end_date"),),
        hints="party_a is the first party named and party_b the second. Give terms as short phrases, not whole clauses."),
    "tenancy_agreement": _doc(
        "Tenancy agreement", "a tenancy or lease agreement for a property",
        "landlord_name, tenant_name, premises_address, agreement_date:d, start_date:d, end_date:d, term_months:n, "
        "monthly_rent:n, security_deposit:n, utility_deposit:n, currency",
        checks=(("order", "term", "start_date", "end_date"),),
        words=r"tenancy agreement|perjanjian penyewaan|perjanjian sewa\b|landlord|tuan rumah|the tenant|penyewa|"
              r"monthly rent(?:al)?|sewa bulanan|security deposit"),
    "employment_letter": _doc(
        "Letter of offer or employment letter", "a letter of offer or employment contract",
        "employer_name, employee_name, id_number, position, letter_date:d, start_date:d, basic_salary:n, allowances:n, "
        "currency, probation_months:n, notice_period, working_hours",
        words=r"letter of offer|offer of employment|surat tawaran|letter of appointment|pelantikan|probation|percubaan|"
              r"basic salary|gaji pokok|commencement date"),
    # --- logistics and trade ------------------------------------------------------
    "goods_received_note": _doc(
        "Goods received note", "a goods received note",
        "supplier_name, receiver_name, grn_number, po_number, do_number, date:d, received_by",
        rows=("items", "code, description, quantity_ordered:n, quantity_received:n, unit"),
        words=r"goods received|\bgrn\b|nota terimaan barang|received by|quantity received|qty received"),
    "consignment_note": _doc(
        "Consignment note or airway bill", "a courier consignment note or airway bill",
        "carrier_name, tracking_number, date:d, sender_name, sender_address, sender_phone, receiver_name, "
        "receiver_address, receiver_phone, pieces:n, weight_kg:n, service_type, cod_amount:n, shipping_fee:n, currency",
        words=r"consignment note|airway bill|\bawb\b|tracking no|nota konsainan|\bcod\b|shipper|consignee|sender|"
              r"penghantar|penerima"),
    "bill_of_lading": _doc(
        "Bill of lading", "a bill of lading",
        "carrier_name, bl_number, date:d, shipper, consignee, notify_party, vessel, voyage, port_of_loading, "
        "port_of_discharge, container_numbers, packages:n, gross_weight_kg:n, description_of_goods",
        words=r"bill of lading|\bb/l\b|port of loading|port of discharge|notify party|vessel|voyage"),
    "packing_list": _doc(
        "Packing list", "a packing list",
        "seller_name, buyer_name, invoice_number, date:d, total_cartons:n, total_net_weight_kg:n, total_gross_weight_kg:n",
        rows=("items", "description, quantity:n, cartons:n, net_weight_kg:n, gross_weight_kg:n"),
        checks=(("sum", "cartons_sum", "items.cartons", "total_cartons"),
                ("sum", "gross_weight_sum", "items.gross_weight_kg", "total_gross_weight_kg")),
        words=r"packing list|senarai pembungkusan|gross weight|net weight|cartons?|\bctns?\b"),
    "customs_declaration": _doc(
        "Customs declaration (K1, K2)", "a customs declaration form (import K1, export K2)",
        "form_type, declaration_number, declaration_date:d, importer_exporter, agent_name, port, country, currency, "
        "total_value:n, total_duty:n, total_sales_tax:n, total_payable:n",
        rows=("items", "description, hs_code, quantity:n, value:n, duty:n, sales_tax:n"),
        checks=(("sum", "value_sum", "items.value", "total_value"),
                ("sum", "duty_sum", "items.duty", "total_duty"),
                ("add", "total_payable", ["+total_duty", "+total_sales_tax"], "total_payable")),
        words=r"kastam no\.? ?[12]|borang k ?[12]\b|customs no|perakuan barang|declaration of goods|hs code|"
              r"kod tarif|duti import|import duty"),
    # --- personal and other ---------------------------------------------------------
    "medical_bill": _doc(
        "Medical bill", "a bill from a clinic, hospital, dentist or pharmacy",
        "provider_name, patient_name, id_number, bill_number, date:d, doctor_name, subtotal:n, tax:n, total:n, "
        "amount_paid:n, payment_method, currency",
        rows=("lines", "description, quantity:n, amount:n"),
        checks=(("sum", "lines_sum", "lines.amount", ("subtotal", "total")),),
        words=r"klinik|clinic|hospital|patient|pesakit|consultation|rawatan|ubat|medication|pharmacy|farmasi|dental"),
    "medical_certificate": _doc(
        "Medical certificate (MC)", "a medical certificate of sick leave",
        "clinic_name, patient_name, id_number, doctor_name, serial_number, issue_date:d, start_date:d, end_date:d, days:n",
        checks=(("order", "leave", "start_date", "end_date"), ("days", "days", "start_date", "end_date", "days")),
        words=r"medical certificate|sijil cuti sakit|sijil sakit|unfit for (?:duty|work)|cuti sakit|tidak sihat untuk"),
    "insurance_claim": _doc(
        "Insurance claim form", "an insurance or takaful claim form",
        "insurer_name, policy_number, claim_number, claimant_name, id_number, plate_number, incident_date:d, "
        "incident_description, claim_amount:n, submitted_date:d, currency",
        checks=(("order", "dates", "incident_date", "submitted_date"),),
        words=r"claim form|borang tuntutan|claim no|no\.? tuntutan|date of (?:loss|accident)|tarikh kemalangan|claimant"),
    "business_card": _doc(
        "Business card", "a business card",
        "name, title, company, phone, mobile, email, website, address",
        checks=(("format", "email", "email", r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", "an email address"),)),
    "property_tax_bill": _doc(
        "Assessment or quit rent bill", "a property assessment (cukai taksiran) or quit rent (cukai tanah) bill",
        "authority_name, bill_type, account_number, owner_name, property_address, bill_number, period, bill_date:d, "
        "due_date:d, arrears:n, current_amount:n, penalty:n, total_due:n, currency",
        checks=(("add", "total_due", ["+arrears", "+current_amount", "+penalty"], "total_due"),),
        words=r"cukai taksiran|cukai tanah|cukai pintu|assessment (?:tax|rate)|quit rent|no\.? akaun harta|pegangan|"
              r"pejabat tanah"),
    "timesheet": _doc(
        "Timesheet or attendance sheet", "a timesheet or attendance record",
        "employee_name, employee_id, employer_name, period_start:d, period_end:d, total_hours:n, total_overtime_hours:n, "
        "approved_by",
        rows=("lines", "date:d, time_in, time_out, hours:n, overtime_hours:n"),
        checks=(("sum", "hours_sum", "lines.hours", "total_hours"),
                ("sum", "overtime_sum", "lines.overtime_hours", "total_overtime_hours")),
        words=r"timesheet|time sheet|attendance|kehadiran|time in|time out|masa masuk|masa keluar|clock in|overtime"),
}


# --- amounts written in words ---------------------------------------------------------
_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_UNITS.update({w: i for i, w in enumerate("kosong satu dua tiga empat lima enam tujuh lapan sembilan sepuluh "
                                          "sebelas".split())})
_UNITS.update({"se": 1})
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_TENS["fourty"] = 40
_SCALES = {"hundred": 100, "ratus": 100, "thousand": 1000, "ribu": 1000, "million": 10 ** 6, "juta": 10 ** 6}
_MINOR = re.compile(r"\b(?:sen|cents?)\b")


def _count(words: list[str]) -> Optional[int]:
    """'one thousand two hundred and fifty' / 'seribu dua ratus lima puluh' -> 1250."""
    total = current = pending = 0  # pending: a unit waiting for "hundred", "puluh" or "belas"
    seen = False
    for w in words:
        if w in ("and", "dan", "only", "sahaja", ""):
            continue
        seen = True
        if w.isdigit():
            pending += int(w)
        elif w in _UNITS:
            pending += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "belas":  # dua belas = 12
            current, pending = current + 10 + pending, 0
        elif w == "puluh":  # lima puluh = 50
            current, pending = current + max(pending, 1) * 10, 0
        elif w in _SCALES and _SCALES[w] == 100:
            current, pending = current + max(pending, 1) * 100, 0
        elif w in _SCALES:
            total, current, pending = total + max(current + pending, 1) * _SCALES[w], 0, 0
        else:
            return None
    return total + current + pending if seen else None


def words_to_amount(text: str) -> Optional[float]:
    """The amount a phrase such as 'Ringgit Malaysia One Thousand Two Hundred
    Fifty and Sen Thirty Only' states, or None when it cannot be worked out."""
    s = re.sub(r"[^a-z0-9/ ]", " ", text.lower().replace("-", " "))
    s = re.sub(r"\b(ringgit|malaysia|rm|myr|dollars?|only|sahaja|tunai)\b", " ", s)
    s = re.sub(r"\bse(ratus|ribu|juta|puluh|belas)\b", r"se \1", s)  # seratus = satu ratus
    fraction = re.search(r"\b(\d{1,2})\s*/\s*100\b", s)
    minor = 0
    if fraction:
        minor, s = int(fraction.group(1)), s[:fraction.start()] + s[fraction.end():]
    elif _MINOR.search(s):
        m = _MINOR.search(s)
        left, right = s[:m.start()].split(), s[m.end():].split()
        if _count(right) is not None:
            minor, major = _count(right), left
        else:  # "... and fifty sen"
            cut = max((i for i, w in enumerate(left) if w in ("and", "dan")), default=-1)
            minor, major = _count(left[cut + 1:]), left[:max(cut, 0)]
        if minor is None:
            return None
        s = " ".join(major)
    whole = _count(s.split())
    return None if whole is None or minor > 99 else round(whole + minor / 100, 2)


# --- checks -----------------------------------------------------------------------------
def _near(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE + 1e-9


def _column(f: dict, path: str) -> list:
    rows, _, col = path.partition(".")
    return [r.get(col) for r in f.get(rows) or []]


def _result(name: str, passed: Optional[bool], detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def run_checks(kind: str, f: dict) -> list[dict]:
    """passed is None when the document does not print what the check needs."""
    out = []
    for check in DOCS[kind]["checks"]:
        op, name = check[0], check[1]
        if op == "sum":
            values = [v for v in _column(f, check[2]) if v is not None]
            targets = {t: f.get(t) for t in ((check[3],) if isinstance(check[3], str) else check[3])}
            targets = {t: v for t, v in targets.items() if v is not None}
            if not values or not targets:
                out.append(_result(name, None, "the amounts or the total are not printed"))
                continue
            got = round(sum(values), 2)
            hit = next((t for t, v in targets.items() if _near(got, v)), None)
            out.append(_result(name, hit is not None, f"the {len(values)} line(s) add up to {got:.2f}, " + (
                f"the {hit.replace('_', ' ')}" if hit else
                "but " + ", ".join(f"{t.replace('_', ' ')} is {v:.2f}" for t, v in targets.items()))))
        elif op == "add":
            total, terms = f.get(check[3]), [(t[0], f.get(t[1:])) for t in check[2]]
            if total is None or all(v is None for _, v in terms):
                out.append(_result(name, None, "the total or its parts are not printed"))
                continue
            got = round(sum((abs(v) if sign == "+" else -abs(v)) for sign, v in terms if v is not None), 2)
            out.append(_result(name, _near(got, total),
                               f"the parts come to {got:.2f}; {check[3].replace('_', ' ')} is {total:.2f}"))
        elif op == "ledger":
            closing = f.get(check[5])
            debit = [v for v in _column(f, check[3]) if v is not None]
            credit = [v for v in _column(f, check[4]) if v is not None]
            if closing is None or not (debit or credit):
                out.append(_result(name, None, "the lines or the closing amount are not printed"))
                continue
            got = round((f.get(check[2]) or 0) + sum(debit) - sum(credit), 2)
            out.append(_result(name, _near(got, closing),
                               f"opening plus charges less payments is {got:.2f}; {check[5].replace('_', ' ')} "
                               f"is {closing:.2f}"))
        elif op in ("order", "days"):
            try:
                a, b = date.fromisoformat(f.get(check[2]) or ""), date.fromisoformat(f.get(check[3]) or "")
            except ValueError:
                out.append(_result(name, None, "one of the dates is not printed or could not be read"))
                continue
            if op == "order":
                out.append(_result(name, a <= b, f"{check[2].replace('_', ' ')} {a} and "
                                                 f"{check[3].replace('_', ' ')} {b}"))
            elif f.get(check[4]) is None:
                out.append(_result(name, None, "the number of days is not printed"))
            else:
                span = (b - a).days + 1
                out.append(_result(name, span == f[check[4]], f"{a} to {b} is {span} day(s); the document says "
                                                              f"{f[check[4]]:g}"))
        elif op == "format":
            value = f.get(check[2])
            if value is None:
                out.append(_result(name, None, f"{check[2].replace('_', ' ')} is not printed"))
            else:
                raw = str(value).strip()
                ok = any(re.fullmatch(check[3], v) for v in (raw, re.sub(r"[\s-]", "", raw).upper(), raw.upper()))
                out.append(_result(name, ok, f"'{value}' " + ("looks like " if ok else "does not look like ") + check[4]))
        elif op == "words":
            words, amount = f.get(check[2]), f.get(check[3])
            stated = words_to_amount(words) if words else None
            if stated is None or amount is None:
                out.append(_result(name, None, "the amount in words is not printed or could not be worked out"))
            else:
                out.append(_result(name, _near(stated, amount), f"the words say {stated:.2f}; the figure is {amount:.2f}"))
    return out


def prompt(kind: str) -> str:
    d = DOCS[kind]
    text = f"This is {d['what']}."
    if d["rows"]:
        text += f"\n- {d['rows'][0]}: one entry per printed row, in order."
    if d["hints"]:
        text += "\n- " + d["hints"]
    return text + "\n- The field names say what each value is. Amounts are as printed, without the currency."


def guess(text: str) -> Optional[str]:
    """The catalogue kind whose words the text uses most, when it uses at least three different ones."""
    best, best_hits = None, 2
    for kind, d in DOCS.items():
        if d["words"] is None:
            continue
        hits = len({m.lower() for m in d["words"].findall(text)})
        if hits > best_hits:
            best, best_hits = kind, hits
    return best
