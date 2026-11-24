from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import engine, llm, service
from .auth import find_client
from .config import get_settings
from .db import get_db, init_db
from .errors import ApiError
from .routers import admin, ocr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ocr")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings()  # fail fast on bad configuration
    init_db()
    if not engine.available():
        log.error("the OCR engine did not load: readings will answer 503")
    service.start()
    yield
    service.stop()


app = FastAPI(title="GET OCR", version="1.0.0", lifespan=lifespan,
              description="Self-hosted text recognition for images and PDFs. "
                          "Send `X-API-Key` with every `/v1` call; create keys in the console at `/admin`.")

_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])

app.include_router(ocr.router)
app.include_router(admin.router)


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError):
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", ())[1:])
    return JSONResponse({"error": "bad_request", "message": f"{where}: {first.get('msg', 'invalid')}".strip(": ")},
                        status_code=422)


@app.get("/health", tags=["system"])
def health(request: Request, db=Depends(get_db)):  # noqa: ANN001
    """No key needed. Send X-API-Key to also check that the key works (401 if not)."""
    key = request.headers.get("x-api-key")
    if key is not None and find_client(db, key) is None:
        raise ApiError(401, "unauthorized", "invalid X-API-Key")
    ready = engine.available()
    # "extraction" is separate from status: plain reading works without the model server.
    body = {"status": "ok" if ready else "degraded", "engine": engine.NAME, "ocr": ready,
            "extraction": llm.ready(), "queue": service.queue_length()}
    if key is not None:
        body["key"] = "ok"
    return body


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")


@app.get("/admin", include_in_schema=False)
def admin_ui():
    return FileResponse(STATIC / "admin.html", headers={
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
                                   "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
    })
