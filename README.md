# GET OCR

Self-hosted text recognition for **get-ride** and any other app, over a REST API.
Send an image or a PDF, get the text back. Runs on a Mac mini (Apple Silicon or Intel) or an
Intel/AMD Synology NAS, in Docker or as a macOS service. Files never leave your hardware,
and there are no per-page fees.

It follows the same pattern as [get.ekyc](https://github.com/getgroupmy/get.ekyc): an admin
console where you create one API key per app, an `X-API-Key` header on every call, signed
webhooks, and data encrypted at rest.

## What it does

| | |
|---|---|
| **Input** | JPEG, PNG, WEBP, TIFF (multi-page), BMP, GIF and PDF, up to `OCR_MAX_UPLOAD_MB` |
| **Read now** | `POST /v1/ocr` answers in the same request. Images and PDFs up to `OCR_SYNC_MAX_PAGES` pages. Nothing is stored |
| **Queue** | `POST /v1/jobs` for long PDFs. Answers at once with a job id; the result comes by signed webhook or by polling |
| **Output** | Plain text in reading order, and every line with its position and the model's confidence |
| **PDF text layer** | A PDF page that already carries text is read from that text (exact and fast). Scanned pages are read as pictures. `force_ocr` reads everything as pictures |
| **Fields** | `POST /v1/extract` turns a document into fields and lines (invoices and other sales documents, bank statements, payslips, utility bills, order lists, or your own list of fields), checks the arithmetic, and flags values that are not in the text. Needs a language model installed on the same machine (see below) |
| **Engine** | PP-OCR (PaddleOCR models through RapidOCR), on the CPU. Latin script (English, Malay) and Chinese |

## Quick start (Docker)

```bash
cp .env.example .env
```

Put two secrets in `.env`:

```bash
openssl rand -hex 32      # OCR_ADMIN_TOKEN
```

```bash
openssl rand -base64 32   # OCR_STORAGE_KEY
```

```bash
docker compose up -d --build
```

Open `http://localhost:8090/admin`, sign in with the admin token, create a client under
**API clients** and copy its key. **Try it** reads a file from the browser. The API reference is
at `/docs`. The port is published on localhost only, and is 8090 so the service can sit beside
get.ekyc (8080) on the same machine.

On a Synology, make the data folder writable for the container's user first (uid 10001):

```bash
mkdir -p data && sudo chown 10001 data && chmod 700 data
```

## Mac mini (M-series) without Docker

Every dependency ships an Apple Silicon build, so nothing is compiled, and the Python that comes with
macOS (3.9) is enough. This is the same way
get.ekyc runs on the Mac mini.

```bash
cd ~/get.ocr && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in the two secrets as above, and add `OCR_DATA_DIR=./data` to `.env`. Then install it as a
startup service (asks for your Mac password):

```bash
./scripts/install_service.sh
```

It starts at boot before anyone logs in, runs as you, and restarts by itself if it stops. It
listens on `127.0.0.1:8090`.

- Restart by hand: `sudo launchctl kickstart -k system/my.getride.ocr`
- Log: `~/get.ocr/server.log`
- Remove: `./scripts/install_service.sh --uninstall`
- To try it without installing: `.venv/bin/uvicorn app.main:app --port 8090`

### The model for field extraction

`/v1/extract` needs a language model running on the same Mac. Nothing is sent off the machine: the
server refuses to start if `OCR_LLM_URL` points anywhere but this machine or the local network.
Install [Ollama](https://ollama.com/download) (it uses the Mac's GPU and starts at login), then fetch
the model once (a download of several GB):

```bash
ollama pull qwen2.5:7b
```

Then `curl -s localhost:8090/health` should show `"extraction": true`. To use another model, set
`OCR_LLM_MODEL`. Ollama starts when you log in, not at boot: after a restart, extraction is off until
someone logs in to the Mac, unless you set the Mac to log in automatically.

With Docker the container reaches Ollama on the host at `host.docker.internal:11434`; that works with
Docker Desktop on a Mac. On Linux, Ollama must listen on an address the container can reach.

**A second reader for handwriting and hard pictures.** The standard reader is fast and gives the
position of every line, but it cannot read handwriting and struggles with complex tables. A small
vision model, [GLM-OCR](https://huggingface.co/zai-org/GLM-OCR) (MIT licence, about 2.7 GB), can be
installed beside it on the same model server:

```bash
ollama pull glm-ocr
```

`/health` then shows `"vision_reader": true`. Ask for it per request with `engine=glm` on
`/v1/ocr`, `/v1/extract` or `/v1/jobs`; `task` chooses what the model is asked for (`text`, the
default, `table` or `formula`). Each page is sent to it as a picture, on this machine only, and
comes back with `"source": "glm"`.

What you give up: it answers with the text of the whole page, so there are no `lines`, no positions
and no confidence, and `sources` cannot point at a box. Bank statements read this way go through
the extraction model instead of the exact table reader, so use the standard reader for anything
with a text layer or clean print. It is also much slower than the standard reader.

**This was built from the model's published instructions and has not been run with the real
model.** Its prompts and the request format are as documented; how well it reads, in which form it
gives tables, how long a page takes on the Mac mini and the largest picture it handles well are
unknown until it is tried there. Two models loaded together need the memory for both.

**Public address.** If get.ekyc's Cloudflare tunnel already runs on that Mac, add a second public
hostname to the same tunnel in Cloudflare Zero Trust (for example `ocr.getride.my`) with service
**HTTP** `localhost:8090`. No second tunnel is needed. Block `/admin*` at Cloudflare with an Access
policy, so the console is reachable only on the Mac or through Cloudflare Access. With Docker
instead, `docker compose --profile tunnel up -d` starts a tunnel of its own (`TUNNEL_TOKEN` in
`.env`, service **HTTP** `ocr:8080`).

`scripts/install_service.sh` is adapted from get.ekyc's script. **It has not been run on a Mac
yet**: this repository was built and tested on Linux.

## Running it

On the Mac mini, from `~/get.ocr`:

| Task | Command |
|---|---|
| Update to the latest `main` and restart | `./scripts/update.sh` |
| Back up the database (to `./backups`, or a folder you name) | `./scripts/backup.sh /Volumes/NAS/ocr` |
| Replace the admin token | `./scripts/rotate_admin_token.sh` |
| Measure accuracy on your own documents | `.venv/bin/python scripts/evaluate.py eval` (see `eval/README.md`) |

- **Backups** hold the encrypted database and queued files. They are useless without
  `OCR_STORAGE_KEY`: keep that key in a password manager, never beside the backups. To run the
  backup nightly: `crontab -e`, then `15 2 * * * cd ~/get.ocr && ./scripts/backup.sh /path >> backup.log 2>&1`.
- **Accuracy.** `scripts/evaluate.py` runs every document in a folder against its expected answer and
  prints, per kind, how many fields were right, how often the checks passed and the time per
  document. `--models a,b` compares installed models; `--engine glm` reads with the vision model.
  Real documents in `eval/` are kept out of git.
- **Limits per app.** One API key may make `OCR_RATE_PER_MINUTE` requests a minute (429
  `rate_limited` beyond that).
- **Activity log.** Creating, revoking and changing clients is recorded with the time and the
  address it came from, shown under "Activity in this console" and at `GET /admin/api/audit`.
- **Watching it.** `GET /health` needs no key and reports `status`, `ocr`, `extraction`,
  `vision_reader`, `queue` and `disk_free_gb`; point any uptime checker at it. Nothing in the
  service sends alerts by itself.
- **Tests on GitHub.** `.github/workflows/tests.yml` runs the tests on Python 3.9 and 3.12 for every
  pull request and push to `main`.

## Integrating (get-ride or any app)

`examples/client.py` is a small Python client (read, extract, queue, wait, list the kinds, check a
webhook's signature) to copy into a backend.

Call the API **from your backend**, never from a mobile or web app: the key must stay server-side.

```bash
BASE=https://ocr.example.com
KEY=ok_...
```

### Read a file now

```bash
curl -s -X POST $BASE/v1/ocr -H "X-API-Key: $KEY" -F file=@receipt.jpg
```

```json
{
  "engine": "pp-ocr", "kind": "image", "page_count": 1,
  "text": "Invoice No: INV-2026-00417\nJumlah perlu dibayar RM 1,250.80",
  "pages": [{
    "page": 1, "width": 1100, "height": 280, "source": "ocr", "confidence": 0.9978,
    "text": "Invoice No: INV-2026-00417\nJumlah perlu dibayar RM 1,250.80",
    "lines": [{"text": "Invoice No: INV-2026-00417", "confidence": 0.9817,
               "box": [[48, 51], [556, 51], [556, 90], [48, 90]]}]
  }],
  "duration_ms": 1413
}
```

`box` is the line's four corners in pixels of the page as read (`width` × `height`), clockwise
from the top left. `source` is `ocr`, or `text_layer` for a PDF page read from its own text
(no `lines` or `confidence` then).

Form fields besides `file`:

| Field | Meaning |
|---|---|
| `pages` | PDF pages to read, like `1-3,7`. Default: all |
| `detail` | `lines` (default) or `text` to leave the lines out |
| `force_ocr` | `true` reads PDF pages as pictures even when they carry text |

The same call with JSON instead of a form, for callers that hold the file as base64:

```bash
curl -s -X POST $BASE/v1/ocr -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"file\": \"$(base64 < receipt.jpg | tr -d '\n')\", \"detail\": \"text\"}"
```

From Node / Deno (for example a Supabase edge function):

```js
const form = new FormData();
form.append("file", new Blob([bytes]), "receipt.jpg");
const r = await fetch(`${BASE}/v1/ocr`, { method: "POST", headers: { "X-API-Key": KEY }, body: form });
const { text } = await r.json();
```

From Python:

```python
import httpx
r = httpx.post(f"{BASE}/v1/ocr", headers={"X-API-Key": KEY}, files={"file": open("receipt.jpg", "rb")}, timeout=60)
text = r.json()["text"]
```

### Fields from an invoice, receipt or bank statement

```bash
curl -s -X POST $BASE/v1/extract -H "X-API-Key: $KEY" -F file=@invoice.pdf
```

The shape of the answer (shortened):

```json
{
  "document_type": "invoice",
  "fields": {
    "supplier_name": "ACME SUPPLIES SDN BHD", "supplier_tax_id": null, "customer_name": null,
    "document_number": "INV-2026-00417", "date": "2026-04-03", "due_date": null, "currency": "MYR",
    "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0, "amount": 100.0},
                   {"description": "Gasket", "quantity": 1, "unit_price": 25.5, "amount": 25.5}],
    "subtotal": 125.5, "discount": null, "service_charge": null, "tax": 10.04, "rounding": null,
    "total": 135.54, "payment_method": null
  },
  "checks": [
    {"name": "line_amounts", "passed": true, "detail": "quantity × unit price equals the amount on 2 line(s)"},
    {"name": "line_items_sum", "passed": true, "detail": "lines add up to 125.50, the subtotal"},
    {"name": "total", "passed": true, "detail": "subtotal, charges and tax come to 135.54; total is 135.54"}
  ],
  "checks_passed": true,
  "not_found_in_text": [],
  "sources": {"total": {"page": 1, "box": [[50, 610], [330, 610], [330, 650], [50, 650]]}},
  "model": "qwen2.5:7b", "model_calls": 1, "page_count": 1
}
```

Send `type` (default `auto`; the kinds are listed below) to say what the document
is, and `include_text=true` to also get the full text. `pages` and `force_ocr` work as for `/v1/ocr`.
For long documents queue it instead: `POST /v1/jobs` with a `type` field gives the same answer as the
job's `result`.

A bank statement gives `bank_name`, `account_holder`, `account_number`, `currency`, `period_start`,
`period_end`, `opening_balance`, `closing_balance` and `transactions` (each with `date`,
`description`, `reference`, `debit`, `credit`, `balance`).

| `type` | Document | Checks |
|---|---|---|
| `invoice`, `receipt`, `credit_note`, `debit_note`, `quotation`, `purchase_order`, `delivery_order` | Sales documents; all give the invoice fields | `line_amounts`, `line_items_sum`, `total` |
| `bank_statement`, `card_statement`, `ewallet_statement` | Bank, credit card and e-wallet statements, read as a table. On a card statement the balance is what is owed, so spending raises it | `running_balances`, `balance_reconciles`, `printed_totals` |
| `payslip` | Salary slip | `earnings_sum`, `deductions_sum`, `net_pay` |
| `utility_bill` | Electricity, water, internet, phone bill | `total_due` |
| `order` | Kitchen or counter order list | `item_quantities` |
| `table` | Any table with a row of column headings, read by position without the model | `column_totals` |
| `custom` | Anything else: you name the fields | none |

`auto` recognises bank statements, order lists, payslips, utility bills and most of the catalogue
below by their wording, and treats everything else as a sales document. It never picks `custom`,
`card_statement` or `ewallet_statement`: name those yourself.

`GET /v1/types` lists every kind with its fields, the name of its list of lines and its checks.

**The catalogue.** These kinds are each read by the model in one pass. The fields are named in
`GET /v1/types`; the checks compare what the document itself prints (lines against a total, parts
against an amount due, one date against another, an amount in words against the figure, the shape
of a number).

| `type` | Document | Checks |
|---|---|---|
| `vehicle_registration` | Vehicle registration card (VOC, geran) | `chassis_number` |
| `road_tax` | Road tax (motor vehicle licence) | `period` |
| `motor_insurance` | Motor insurance cover note or schedule | `period`, `total_payable` |
| `vehicle_inspection` | Vehicle inspection report | none |
| `driver_permit` | E-hailing permit or PSV licence | `validity` |
| `traffic_summons` | Traffic summons or compound | `dates` |
| `vehicle_agreement` | Vehicle rental or hire-purchase agreement | `term` |
| `vehicle_handover` | Vehicle handover or condition checklist | none |
| `supplier_statement` | Supplier statement of account | `total_due` |
| `payment_voucher` | Payment voucher | `amount_in_words` |
| `official_receipt` | Official receipt | `amount_in_words` |
| `remittance_advice` | Bank transfer slip or remittance advice | none |
| `cheque` | Cheque | `amount_in_words` |
| `loan_statement` | Loan or financing statement | `closing_balance` |
| `expense_claim` | Expense or petty cash claim | `lines_sum` |
| `ea_form` | EA form (annual pay statement) | none |
| `contribution_statement` | EPF, SOCSO or EIS contribution statement | `employer_sum`, `employee_sum`, `total_amount` |
| `pcb_statement` | PCB / CP39 statement | `pcb_sum`, `total_amount` |
| `sst_return` | SST return or payment receipt | `period`, `total_payable` |
| `tax_notice` | Tax assessment notice | `dates` |
| `company_profile` | Company profile (SSM) | `registration_number` |
| `business_licence` | Business licence | `validity` |
| `contract` | Contract or agreement | `term` |
| `tenancy_agreement` | Tenancy agreement | `term` |
| `employment_letter` | Letter of offer or employment letter | none |
| `goods_received_note` | Goods received note | none |
| `consignment_note` | Consignment note or airway bill | none |
| `bill_of_lading` | Bill of lading | none |
| `packing_list` | Packing list | `cartons_sum`, `gross_weight_sum` |
| `customs_declaration` | Customs declaration (K1, K2) | `value_sum`, `duty_sum`, `total_payable` |
| `medical_bill` | Medical bill | `lines_sum` |
| `medical_certificate` | Medical certificate (MC) | `leave`, `days` |
| `insurance_claim` | Insurance claim form | `dates` |
| `business_card` | Business card | `email` |
| `property_tax_bill` | Assessment or quit rent bill | `total_due` |
| `timesheet` | Timesheet or attendance sheet | `hours_sum`, `overtime_sum` |
| `driving_licence` | Driving licence | `validity` |
| `accident_report` | Accident or police report | `dates` |
| `repair_estimate` | Repair estimate or workshop quotation | `lines_sum`, `total` |
| `adjuster_report` | Insurance adjuster's report | `net_payable` |
| `ownership_transfer` | Vehicle ownership transfer form | `chassis_number` |
| `loan_release_letter` | Loan settlement or release letter | none |
| `insurance_endorsement` | Insurance endorsement | `total_payable` |
| `insurance_policy` | Insurance policy schedule (non-motor) | `period`, `total_payable` |
| `vehicle_sales_order` | Vehicle sales order or booking form | `on_the_road_price` |
| `instalment_schedule` | Instalment or repayment schedule | `term`, `instalments_sum` |
| `fleet_card_statement` | Fuel or toll card statement | `amount_sum`, `litres_sum` |
| `bank_letter` | Bank confirmation letter | none |
| `bank_guarantee` | Bank guarantee | `validity`, `amount_in_words` |
| `fixed_deposit_advice` | Fixed deposit advice | `tenure`, `maturity_amount` |
| `bank_advice` | Bank debit or credit advice | none |
| `deposit_slip` | Cash or cheque deposit slip | `total_amount` |
| `aging_report` | Aging report | `totals_sum`, `grand_total` |
| `trial_balance` | Trial balance | `debit_sum`, `credit_sum`, `balanced` |
| `ledger` | Ledger or account printout | `closing_balance` |
| `settlement_report` | Payment gateway settlement report | `gross_sum`, `net_sum`, `net_amount` |
| `earnings_statement` | Driver or commission earnings statement | `net_payout` |
| `hr_letter` | Staff letter (resignation, termination, warning, confirmation) | `dates` |
| `leave_form` | Leave application form | `leave` |
| `borang_e` | Borang E (employer's return) | none |
| `employee_tax_form` | CP22 / CP22A (joiner or leaver tax notice) | none |
| `payment_receipt_statutory` | Statutory payment receipt | none |
| `work_permit` | Foreign worker permit or levy receipt | `validity` |
| `ssm_certificate` | SSM certificate or form | `registration_number` |
| `resolution` | Board or members' resolution | none |
| `power_of_attorney` | Power of attorney | none |
| `statutory_declaration` | Statutory declaration | none |
| `stamp_certificate` | Stamp duty certificate | `total_paid` |
| `court_document` | Court document | none |
| `letter_of_demand` | Letter of demand | `dates` |
| `reference_letter` | Reference or clearance letter | none |
| `booking_confirmation` | Booking or reservation confirmation | `period`, `balance_due` |
| `refund_form` | Deposit refund or refund form | `refund_amount` |
| `civil_certificate` | Birth, marriage or death certificate | none |
| `id_card_other` | Student, staff or OKU card | none |
| `sale_purchase_agreement` | Sale and purchase agreement | `balance`, `dates` |
| `land_title` | Land title | none |
| `premises_certificate` | Premises certificate (fire, occupancy, signboard) | `validity` |
| `maintenance_bill` | Maintenance fee or sinking fund bill | `total_due` |
| `commercial_invoice` | Commercial invoice (export or import) | `items_sum`, `total` |
| `certificate_of_origin` | Certificate of origin | none |
| `letter_of_credit` | Letter of credit | `validity`, `shipment` |
| `proof_of_delivery` | Proof of delivery | none |
| `weighbridge_ticket` | Weighbridge ticket | `net_weight` |
| `gate_pass` | Gate pass | none |
| `stock_count_sheet` | Stock count sheet | `quantity_sum` |

A new kind is one entry in `app/doctypes.py`: its fields, its table of lines, its checks and the
words `auto` knows it by.

A payslip gives `employer_name`, `employee_name`, `employee_id`, `id_number`, `position`, `pay_period`,
`pay_date`, `currency`, `gross_pay`, `total_deductions`, `net_pay` and `lines` (each with `kind`:
`earning`, `deduction` or `employer`, a `description` and an `amount`). Employer contributions are
listed but not taken off the pay in the checks.

A utility bill gives `provider_name`, `customer_name`, `account_number`, `bill_number`, `bill_date`,
`due_date`, `period_start`, `period_end`, `currency`, `usage`, `usage_unit`, `previous_balance`,
`payments_received`, `current_charges`, `tax`, `rounding` and `total_due`.

**Any table.** `type=table` reads a table from where the words sit: the row of column headings gives
the columns, each value goes to the heading it is under, a second line of text joins the row above,
and a totals row is checked against the sum of each column. It needs no model, so it works with
extraction turned off and takes no longer than the reading. The answer has `columns`, `rows` (one
object per row, keyed by column) and `totals`. Only the first table in the document is read.

**Several documents in one file.** Send `split=true` when a PDF holds a stack of documents scanned
together. The answer then has `documents`, one entry per document with its `pages` and the usual
fields and checks. A page starts a new document when it says it is page 1, when it is a different
kind of document from the page before, or when the page before printed a total. These are rules of
thumb: check the page ranges.

**QR codes and barcodes.** Extraction looks for codes on every picture, and on the first two and the
last page of a PDF with a text layer; `codes` lists what it found with the page and position. When
one is an LHDN MyInvois link, `einvoice` gives its `uuid` and `url`. The link is only decoded, not
checked with LHDN. For plain reading, send `codes=true` to `POST /v1/ocr`.

**The same document twice.** Every answer says whether this client has sent the same file before
(`duplicate_file_first_seen`), and a sales document says whether the same issuer, number and total
came in before in another file (`duplicate.same_document_first_seen`). Only hashes are kept, for
`OCR_DUPLICATE_DAYS`. This catches a resubmitted invoice; it does not detect an edited picture.

**How sure, and where.** `sources` gives for each top-level value the page, the box of the line it
was read from and the reader's `confidence` in that line (`null` for a PDF's own text, which is
exact). Under the name of the list (`line_items`, `transactions`, …) it gives the same for each row.
This is the reader's confidence in the printed line, not the model's confidence in the field: use it
together with the checks and `not_found_in_text`.

**Rows for a spreadsheet.** Send `Accept: text/csv` to `POST /v1/extract` to get the list of lines
as CSV.

**Pages on their side.** A page photographed or scanned sideways is turned upright before reading;
the page then carries `rotated` (90 or 270) and its positions are on the upright page. An
upside-down page is read, but its lines come out in reverse order.

**Picture quality.** Every page that was read from a picture carries `quality`: `sharpness`,
`brightness` and `issues` (`blurry`, `dark`, `low_resolution`). Use it to ask for a better photo
before trusting the answer. The thresholds are first guesses.

**Your own fields.** For any other document, send `type=custom` and `fields`: the values to find.

```bash
curl -s -X POST $BASE/v1/extract -H "X-API-Key: $KEY" -F file=@cover-note.pdf -F type=custom \
  -F 'fields={"policy_number": "string", "expiry": "date", "sum_insured": {"type": "number", "description": "amount covered"}}'
```

`fields` is JSON as above (types `string`, `number`, `date`; a `description` helps the model), or
just names separated by commas (`policy_number,expiry_date`; a name containing `date` is a date).
Names are lower case letters, digits and `_`, at most 40 fields. There is nothing to add up, so
`checks` is empty and `checks_passed` is `null`; `not_found_in_text` still lists values the model
gave that are not printed.

An order list (the slip a restaurant prints for the kitchen: items and quantities, no prices) gives
`title`, `order_number`, `table`, `guests`, `date`, `time`, `staff`, `service_type` and `items` (each
with `quantity`, `name`, `short_name`, `notes`). `auto` takes a document for an order list when it
names a table or how it is served and prints no prices. Its one check, `item_quantities`, says
whether every item has a quantity; there are no amounts to add up.

**Statements are read as a table, not by the model.** The transactions are taken from where the words
sit on the page: a row starts with a date, its amounts are placed in the deposit, withdrawal or
balance column by their position, and following lines without a date are added to the description.
Which column is money out is taken from the headings (English or Malay) and proved against the
printed balances. This is exact and takes well under a second for a 20-page statement with a text
layer; a scanned statement takes as long as its OCR. The model is called once, for the account
details at the top of the first page. `read_by` is `"table"` for this, and `"model"` when the pages
do not look like a table of dated amounts and the model read everything (slow, and much less
reliable: look at the checks). `reference` is `null` in a table reading; references stay in the
description.

**How to use the answer.** A local model misreads more often than a cloud one, so the answer tells
you how far to trust it:

| Field | Meaning |
|---|---|
| `checks` | Arithmetic on what was extracted. Invoices and receipts: quantity × unit price against each amount, the lines against the subtotal, and subtotal + charges + tax against the total. Statements: every running balance, opening + credits − debits against closing, and the sum and number of debits and credits against the totals the statement prints. `passed` is `null` when the document does not print what the check needs |
| `checks_passed` | `true` only when every check that could run passed. Treat `false` or `null` as "a person should look" |
| `not_found_in_text` | Values the model gave that are not printed in the text, so it invented or altered them. Dates are not looked up, because they are rewritten to `YYYY-MM-DD` |
| `sources` | For each top-level value, the page and the box of the line it was found on |
| `model_calls` | 1 per invoice, plus 1 re-reading when a check fails (kept only if fewer checks fail). 1 for a statement read as a table; one per page plus re-readings when the model read it |
| `read_by` | `table` or `model`, see above |
| `balance_columns_swapped` | Statement lines where debit and credit were exchanged because only that fits the printed balance |
| `truncated` | `true` when the text was longer than the model can take and the end was left out |

`GET /health` shows `"extraction": true` when the model server answers and the model is installed.
Without it `/v1/extract` answers 503 `extraction_unavailable`; plain reading keeps working.

### Queue a long PDF

```bash
curl -s -X POST $BASE/v1/jobs -H "X-API-Key: $KEY" -F file=@contract.pdf -F external_ref=doc-1042
```

The answer (HTTP 202) has the job `id` and `status: "queued"`. `external_ref` is your own id.
Then wait for the webhook, or poll:

```bash
curl -s $BASE/v1/jobs/$ID -H "X-API-Key: $KEY"
```

`status` goes `queued`, `processing`, then `completed` (with `result`, shaped like the answer of
`/v1/ocr`) or `failed` (with `error` and `error_message`). Other calls:
`GET /v1/jobs?status=…&external_ref=…` and `DELETE /v1/jobs/{id}`.

### Webhook

If the client has a webhook URL, this is POSTed to it when a job ends (3 attempts):

```json
{"event":"ocr.completed","id":"job_…","external_ref":"doc-1042","status":"completed",
 "pages":12,"error":null,"completed_at":"2026-11-24T09:18:30Z"}
```

It never carries the text: fetch that with `GET /v1/jobs/{id}`. Verify the call before trusting it:
`X-OCR-Signature` must equal `"sha256=" + HMAC_SHA256(webhook_secret, X-OCR-Timestamp + "." + raw_body)`,
and the timestamp should be recent.

```python
import hashlib, hmac, time

def is_valid(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature) and abs(time.time() - int(timestamp)) < 300
```

### Errors

Every error is `{"error": "<code>", "message": "<text>"}`.

| HTTP | `error` | When |
|---|---|---|
| 401 | `unauthorized` | Missing, wrong or revoked key |
| 413 | `too_large` | File over `OCR_MAX_UPLOAD_MB`, or an image with too many pixels |
| 415 | `unsupported_format` | Not an image or PDF this service reads |
| 422 | `bad_file`, `pdf_protected` | Damaged or password-protected PDF |
| 422 | `too_many_pages` | More pages than the limit; use `pages` or `/v1/jobs` |
| 422 | `bad_pages`, `bad_detail`, `bad_request` | A field is wrong |
| 429 | `queue_full` | Too many jobs waiting |
| 422 | `bad_type`, `no_text` | Unknown `type`, or a document with no readable text to extract from |
| 502, 504 | `extraction_failed`, `extraction_timeout` | The model gave no usable answer, or took too long |
| 503 | `engine_unavailable` | The OCR engine did not load on the server |
| 503 | `extraction_unavailable` | The model server is not running, or the model is not installed |

`GET /health` needs no key. Send `X-API-Key` with it to check that a key works (401 if not).

Keep the app working when this service is off, as get-ride's `docs/NAS.md` asks of anything on
the NAS: treat a failed call as "no text yet", not as a blocker.

## Security and privacy

- `POST /v1/ocr` and `POST /v1/extract` store nothing: the file and the text exist only in memory during the request.
- A queued file is encrypted on disk (AES-256-GCM) and deleted as soon as it has been read. Job
  results are encrypted in the database and deleted after `OCR_RETENTION_DAYS` (default 7), or
  at once with `DELETE /v1/jobs/{id}`.
- Field extraction uses a model on the same machine. `OCR_LLM_ALLOW_REMOTE=false` (the default) stops the
  server from starting if the model address is not local.
- **Back up `OCR_STORAGE_KEY` separately from the data folder.** Without it stored results are unreadable.
- API keys are stored hashed and shown once. A client sees only its own jobs.
- The console and `/admin/api` show usage counts and job status, never the text of a client's job.
- Usage records hold counts only: pages, bytes, time. No file names, no text.
- The Docker container runs as a non-root user with a read-only filesystem and no capabilities.

## Limits you should know

- **Printed text.** Handwriting is read poorly. Accuracy depends on the scan or photo: keep it sharp
  and evenly lit.
- **Scripts.** The bundled model reads Latin script and Chinese. Jawi/Arabic, Tamil and Thai are
  not read; RapidOCR has models for other scripts, but none is wired in here.
- **Rotated pages.** On a page that is upside down or on its side the lines are still read, but
  they come out in the wrong order (bottom line first, or all on one row). Rotate it upright before sending.
- **How good the model is.** On the Mac mini `qwen2.5:7b` answers in 30 to 40 seconds per call. It
  could not copy a dense statement table (wrong balances, invented values), which is why statements
  are now read as a table by code. Invoices and receipts are still read by the model and have not
  been tried on real documents: compare with the originals before relying on it. The checks catch
  wrong amounts; they cannot catch a wrong supplier name or date that is printed somewhere on the page.
- **Photos.** A photo with a shadow or uneven light is evened out before reading (`OCR_EVEN_LIGHTING`,
  on by default; evenly lit pictures and scans are left as they are). This and the detection
  setting for small lone characters were tuned on a single photo of an order list, where they
  recovered a quantity that was missed in the shadow. Other photos are untested.
- **Order lists** are read by the model and have only been run against a stand-in, not the real model.
- **The review view is a viewer.** In Try it, a picture is shown with every found value outlined. It
  does not save corrections, does not work for PDFs or turned pages, and nothing learns from it.
- **Not built.** Matching one document against another (an invoice against its purchase order, a
  payment against a statement line); detecting an edited picture; export to an accounting system;
  searchable PDF; users and roles for the console (there is one admin token); alerts.
- **Also not built.** Handwriting; Jawi, Tamil and Thai; whether a tick box is ticked; whether a signature or
  a stamp is present; identity documents (get.ekyc does those). 1D barcodes go through OpenCV's
  reader but were not tested; only QR codes were.
- **Statement layouts.** The table reader was checked against one real 20-page Hong Leong Bank
  statement (192 transactions, equal to the totals the bank prints) and made-up ones. It expects a
  date at the start of each transaction and right-aligned amount columns. Other banks' layouts are
  untested; the three checks say when a reading does not add up.
- **Which kinds were tried for real.** Only bank statements have run on real documents with the real
  model. Every other kind, including the whole catalogue, was built from general knowledge of what
  such a document prints, without a sample, and run only against a stand-in model. Expect field
  names that do not fit a real layout and values the 7B model gets wrong; try each kind you need on
  your own documents first. Long free-text documents (contracts, agreements) have nothing to add up,
  so a wrong value there cannot be caught. For identity documents use get.ekyc, which validates what it reads.
- **Extraction reads the OCR text, not the picture.** What the OCR cannot read (handwriting, Jawi,
  Tamil, Thai) cannot be extracted either. A statement transaction whose description continues on
  the next page loses those lines.
- **One page at a time.** Each reading already uses every core; requests queue behind each other.
- **One admin token.** There are no console users or roles, and no audit trail, unlike get.ekyc.
- **No per-client rate limit.** A client can fill the queue up to `OCR_MAX_QUEUE`.
- **Tested with made-up documents only**, on a 4-core Linux x86 machine: about 1.4 s for a
  two-line image and 1.3 s a page for a scanned PDF. The Mac mini and the Synology have not been
  measured. On a CPU without AVX (Celeron NAS) set `OCR_ENGINE=openvino` and add `openvino` to
  `requirements.txt`; that path has not been tried here.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest -q
```

```
app/main.py              FastAPI app, health, console page
app/routers/ocr.py       /v1/ocr and /v1/jobs
app/routers/admin.py     /admin/api: clients, usage, try it
app/service.py           reading a document, the job queue, retention
app/documents.py         images and PDFs into pages
app/doctypes.py          the catalogue of other documents: fields, checks, words to recognise them
app/tables.py            any table, read by position
app/codes.py             QR codes and barcodes
app/statement.py         the transaction table of a bank statement, read by position
app/engine.py            PP-OCR through RapidOCR
app/extract.py           fields, arithmetic checks, lookup of values in the text
app/llm.py               the local model server (Ollama or OpenAI-compatible)
app/webhooks.py          signed webhook delivery
app/static/admin.html    console
```

## Components and licences

| Component | Use | Licence |
|---|---|---|
| [RapidOCR](https://github.com/RapidAI/RapidOCR) with PP-OCR models | Text detection and recognition | Apache-2.0 |
| ONNX Runtime | Runs the models on the CPU | MIT |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (PDFium) | PDF rendering and text layer | Apache-2.0 / BSD-3-Clause |
| Pillow | Image decoding | MIT-CMU |

PyMuPDF was not used because it is AGPL.
