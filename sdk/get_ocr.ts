/**
 * GET OCR client for a server (Deno, Supabase Edge Functions, Node 18+, Bun).
 * One file, no dependencies: it uses fetch and Web Crypto.
 *
 *     import { GetOcr } from "./get_ocr.ts";
 *     const ocr = new GetOcr("https://ocr.getcdn.link", OCR_KEY);
 *     const { text } = await ocr.read(bytes, { filename: "receipt.jpg" });
 *     const bill = await ocr.extract(bytes, { type: "utility_bill" });
 *     if (bill.checks_passed !== true || bill.not_found_in_text.length) { ... } // have a person look
 *     const job = await ocr.queue(pdfBytes, { type: "bank_statement", externalRef: "acc-42" });
 *     const done = await ocr.wait(job.id);
 *
 * The reading happens on the GET OCR server, not in the app. Call it from your
 * server only: the API key must never be in a mobile or web app. A phone sends
 * its photo to your server (an edge function), and that function calls this.
 */

export type OcrFile = Uint8Array | ArrayBuffer | Blob;

export type OcrEngine = "standard" | "detect" | "jawi" | "arabic" | "tamil" | "thai" | "glm" | "vision";

export interface OcrOptions {
  /** The file's name, with its ending (.jpg, .heic, .pdf, .docx, .xlsx). Only used as a label. */
  filename?: string;
  /** PDF pages to read, like "1-3,7". Default: all. */
  pages?: string;
  /** "detect" finds Jawi, Arabic, Tamil or Thai by itself; "glm" and "vision" (GET.ocr Vision) are vision models for handwriting and hard photos. */
  engine?: OcrEngine;
  /** With a vision engine: "text" (default), "table", "formula", and with "vision" also "seal" and "chart". */
  task?: "text" | "table" | "formula" | "seal" | "chart";
  /** The password of a locked PDF. Used once to open it; the server keeps it nowhere. */
  pdfPassword?: string;
  /** Read a PDF's pages as pictures even when it carries text. */
  forceOcr?: boolean;
  /** Also report tick boxes, stamp ink and signatures on every page. */
  marks?: boolean;
  /** Your own reference, returned with a queued job and its webhook. */
  externalRef?: string;
  /** Give up after this many milliseconds. Default 90 000: Cloudflare ends a request at 100 s. */
  timeoutMs?: number;
}

export interface ExtractOptions extends OcrOptions {
  /** What the document is: "invoice", "receipt", "bank_statement", "order", ... Default "auto". See types(). */
  type?: string;
  /** With type "custom": the values to find, as names or { name: "string|number|date" }. */
  fields?: string[] | Record<string, unknown>;
  /** Several documents in one file: each is returned on its own. */
  split?: boolean;
  /** Also return the plain text that was read. */
  includeText?: boolean;
}

export interface OcrLine {
  text: string;
  confidence: number;
  /** Four corners in page pixels, clockwise from the top left. */
  box: [number, number][];
  /** "jawi", "arabic", "tamil" or "thai" when that reader read the line. */
  script?: string;
  /** A pen stroke crosses this line's quantity (an order slip). */
  struck?: boolean;
}

export interface OcrPage {
  page: number;
  width: number;
  height: number;
  /** "ocr" (a picture was read), "text_layer" (a PDF's own text), "document" (Word, Excel), "glm". */
  source: string;
  text: string;
  confidence: number | null;
  /** How far the page was turned clockwise to stand upright; a decimal for a tilted page. */
  rotated?: number;
  lines?: OcrLine[];
  quality?: { sharpness: number; brightness: number; issues: string[] };
  [more: string]: unknown;
}

export interface OcrResult {
  engine: string;
  /** "image", "pdf", "docx" or "xlsx". */
  kind: string;
  page_count: number;
  pages: OcrPage[];
  text: string;
  duration_ms: number;
  [more: string]: unknown;
}

export interface OcrCheck {
  name: string;
  /** true: it adds up. false: it does not. null: the document prints nothing to check it by. */
  passed: boolean | null;
  detail: string;
}

export interface ExtractResult {
  document_type: string;
  fields: Record<string, unknown>;
  checks: OcrCheck[];
  /** true only when every check that could be made passed. */
  checks_passed: boolean | null;
  /** Values the model gave that are not printed in the text: treat as made up. */
  not_found_in_text: string[];
  evidence?: Record<string, string>;
  sources?: Record<string, unknown>;
  text?: string;
  [more: string]: unknown;
}

export interface OcrJob {
  id: string;
  status: "queued" | "processing" | "completed" | "failed";
  external_ref: string | null;
  pages: number | null;
  error: string | null;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
  result_available: boolean;
  result?: OcrResult | ExtractResult | null;
}

/** A call the server refused or could not serve. `code` is machine readable ("pdf_protected", "too_large", ...). */
export class OcrError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(`${code}: ${message}`);
    this.name = "OcrError";
    this.status = status;
    this.code = code;
  }
  /** Worth trying again later: the server was busy, starting, or the request ran out of time. */
  get retryable(): boolean {
    return this.status === 429 || this.status >= 500 || this.code === "timeout" || this.code === "network";
  }
}

export class GetOcr {
  readonly base: string;
  private readonly key: string;
  private readonly fetcher: typeof fetch;

  constructor(base: string, key: string, fetcher: typeof fetch = fetch) {
    this.key = key;
    this.fetcher = fetcher;
    this.base = base.replace(/\/+$/, "");
    if (!/^https?:\/\//.test(this.base)) throw new Error("GetOcr: the address must start with https://");
    if (!key) throw new Error("GetOcr: an API key is needed");
  }

  /** The text of an image, PDF, Word or Excel file, with every line's position. Answers in the same call. */
  read(file: OcrFile, options: OcrOptions = {}): Promise<OcrResult> {
    return this.post("/v1/ocr", file, options) as Promise<OcrResult>;
  }

  /** A document as fields, with the checks that say whether it adds up. Needs the server's language model. */
  extract(file: OcrFile, options: ExtractOptions = {}): Promise<ExtractResult> {
    return this.post("/v1/extract", file, options) as Promise<ExtractResult>;
  }

  /** For a long document: answers at once with a job. The result comes by webhook, or by wait() or job(). */
  queue(file: OcrFile, options: ExtractOptions = {}): Promise<OcrJob> {
    return this.post("/v1/jobs", file, options) as Promise<OcrJob>;
  }

  job(id: string): Promise<OcrJob> {
    return this.call("GET", `/v1/jobs/${encodeURIComponent(id)}`) as Promise<OcrJob>;
  }

  /** Ask every few seconds until the job is completed or failed. A failed job throws. */
  async wait(id: string, timeoutMs = 600_000, everyMs = 3_000): Promise<OcrJob> {
    const until = Date.now() + timeoutMs;
    for (;;) {
      const job = await this.job(id);
      if (job.status === "completed") return job;
      if (job.status === "failed") throw new OcrError(422, job.error ?? "failed", job.error_message ?? "the job failed");
      if (Date.now() + everyMs > until) throw new OcrError(504, "timeout", "the job was not finished in time");
      await new Promise((r) => setTimeout(r, everyMs));
    }
  }

  /** The kinds of document extract() knows, each with its fields and checks. */
  types(): Promise<Record<string, unknown>> {
    return this.call("GET", "/v1/types") as Promise<Record<string, unknown>>;
  }

  /** Is the server up, and which readers are ready? */
  health(): Promise<Record<string, unknown>> {
    return this.call("GET", "/health") as Promise<Record<string, unknown>>;
  }

  private post(path: string, file: OcrFile, o: ExtractOptions): Promise<unknown> {
    const form = new FormData();
    const blob = file instanceof Blob ? file : new Blob([file as BlobPart]);
    form.append("file", blob, o.filename ?? "document");
    const set = (name: string, value: unknown) => {
      if (value !== undefined && value !== null && value !== false && value !== "") form.append(name, String(value));
    };
    set("pages", o.pages);
    set("engine", o.engine === "standard" ? undefined : o.engine);
    set("task", o.task === "text" ? undefined : o.task);
    set("pdf_password", o.pdfPassword);
    set("force_ocr", o.forceOcr);
    set("marks", o.marks);
    set("external_ref", o.externalRef);
    set("type", o.type);
    set("split", o.split);
    set("include_text", o.includeText);
    if (o.fields !== undefined) set("fields", Array.isArray(o.fields) ? o.fields.join(",") : JSON.stringify(o.fields));
    return this.call("POST", path, form, o.timeoutMs);
  }

  private async call(method: string, path: string, body?: FormData, timeoutMs = 90_000): Promise<unknown> {
    const stop = new AbortController();
    const timer = setTimeout(() => stop.abort(), timeoutMs);
    let response: Response;
    try {
      response = await this.fetcher(this.base + path, { method, body, headers: { "X-API-Key": this.key }, signal: stop.signal });
    } catch (e) {
      const timedOut = stop.signal.aborted;
      throw new OcrError(timedOut ? 504 : 503, timedOut ? "timeout" : "network",
        timedOut ? "GET OCR did not answer in time; for a long document use queue()" : `GET OCR could not be reached: ${(e as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    let data: unknown = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* not JSON: an error page from a proxy */ }
    if (!response.ok) {
      const d = (data ?? {}) as { error?: string; message?: string };
      throw new OcrError(response.status, d.error ?? `http_${response.status}`, d.message ?? (text.slice(0, 200) || response.statusText));
    }
    return data;
  }
}

/**
 * Is this webhook really from GET OCR? It signs "timestamp.body" with the secret the console showed
 * once for your API client: X-OCR-Signature: sha256=<hex>, X-OCR-Timestamp: <unix seconds>.
 * Pass the body exactly as received (text, not re-serialised JSON). A webhook older than
 * `toleranceSeconds` is refused, so a recorded one cannot be sent again later.
 */
export async function webhookIsGenuine(
  secret: string, rawBody: string, timestamp: string | null, signature: string | null,
  toleranceSeconds = 300, now: number = Date.now() / 1000,
): Promise<boolean> {
  if (!secret || !timestamp || !signature || !/^\d+$/.test(timestamp)) return false;
  if (Math.abs(now - Number(timestamp)) > toleranceSeconds) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(`${timestamp}.${rawBody}`)));
  const want = "sha256=" + Array.from(mac, (b) => b.toString(16).padStart(2, "0")).join("");
  if (want.length !== signature.length) return false;
  let diff = 0; // compared in full, so the time taken says nothing about where they differ
  for (let i = 0; i < want.length; i++) diff |= want.charCodeAt(i) ^ signature.charCodeAt(i);
  return diff === 0;
}
