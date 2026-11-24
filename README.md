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

Every dependency ships an Apple Silicon build, so nothing is compiled. This is the same way
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

**Public address.** If get.ekyc's Cloudflare tunnel already runs on that Mac, add a second public
hostname to the same tunnel in Cloudflare Zero Trust (for example `ocr.getride.my`) with service
**HTTP** `localhost:8090`. No second tunnel is needed. Block `/admin*` at Cloudflare with an Access
policy, so the console is reachable only on the Mac or through Cloudflare Access. With Docker
instead, `docker compose --profile tunnel up -d` starts a tunnel of its own (`TUNNEL_TOKEN` in
`.env`, service **HTTP** `ocr:8080`).

`scripts/install_service.sh` is adapted from get.ekyc's script. **It has not been run on a Mac
yet**: this repository was built and tested on Linux.

## Integrating (get-ride or any app)

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
| 503 | `engine_unavailable` | The OCR engine did not load on the server |

`GET /health` needs no key. Send `X-API-Key` with it to check that a key works (401 if not).

Keep the app working when this service is off, as get-ride's `docs/NAS.md` asks of anything on
the NAS: treat a failed call as "no text yet", not as a blocker.

## Security and privacy

- `POST /v1/ocr` stores nothing: the file and the text exist only in memory during the request.
- A queued file is encrypted on disk (AES-256-GCM) and deleted as soon as it has been read. Job
  results are encrypted in the database and deleted after `OCR_RETENTION_DAYS` (default 7), or
  at once with `DELETE /v1/jobs/{id}`.
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
- **Plain text only.** No tables, form fields or key-value extraction. It gives you lines and
  where they are; picking "total" or "IC number" out of them is the calling app's work.
  For identity documents use get.ekyc, which validates what it reads.
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
app/engine.py            PP-OCR through RapidOCR
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
