"""Runtime configuration. Every setting can be overridden with an OCR_* env var."""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_local_url(url: str) -> bool:
    """True for this machine, a Docker host alias, a .local name or a private network address."""
    import ipaddress
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal", "ollama") or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OCR_", env_file=".env", extra="ignore")

    # --- required secrets -------------------------------------------------
    admin_token: str  # protects /admin/api; at least 24 chars
    storage_key: str  # base64 of 32 random bytes; encrypts queued files and stored results

    # --- paths ------------------------------------------------------------
    data_dir: Path = Path("/data")
    database_url: str = ""  # default: sqlite file inside data_dir

    # --- engine -----------------------------------------------------------
    engine: str = "onnxruntime"  # onnxruntime | openvino (CPU without AVX) | off
    even_lighting: bool = True  # photos with a shadow or uneven light are evened out before reading
    min_confidence: float = 0.5  # lines the model is less sure of than this are dropped
    pdf_dpi: int = 200  # resolution PDF pages are rendered at before reading
    max_image_side: int = 3000  # larger images and pages are scaled down to this

    # --- field extraction (a language model on this machine) -------------
    llm_url: str = "http://127.0.0.1:11434"  # Ollama on the same machine; "" turns extraction off
    llm_api: str = "ollama"  # ollama | openai (any OpenAI-compatible local server; URL then ends in /v1)
    llm_model: str = "qwen2.5:7b"
    llm_context: int = 8192  # tokens the model is given room for (Ollama's own default is too small)
    llm_timeout_seconds: float = 180.0
    # A second reader: a vision model on the same model server that reads the picture of a page itself
    # (handwriting, hard photos, complex tables). "" turns it off. Asked for per request with engine=glm.
    vlm_model: str = "glm-ocr"
    vlm_timeout_seconds: float = 180.0
    vlm_max_side: int = 2000  # pictures are scaled down to this before they are sent to the model
    vlm_max_tokens: int = 4096  # the longest answer per page; stops a model that starts repeating itself
    llm_allow_remote: bool = False  # False: refuse to start if llm_url is not this machine or the local network
    date_order: str = "dmy"  # how to read 03/04/2026 on a document: dmy | mdy

    # --- limits / housekeeping -------------------------------------------
    max_upload_mb: int = 20
    sync_max_pages: int = 5  # POST /v1/ocr answers in the request; longer PDFs go to /v1/jobs
    max_pages: int = 100  # per job
    max_queue: int = 200  # jobs waiting; beyond this new jobs get 429
    retention_days: int = 7  # job results are deleted this long after completion (0 = keep)
    webhook_timeout_seconds: float = 10.0
    cors_origins: str = ""  # comma separated; empty = no CORS

    @field_validator("admin_token")
    @classmethod
    def _admin_token_strong(cls, v: str) -> str:
        if len(v) < 24:
            raise ValueError("OCR_ADMIN_TOKEN must be at least 24 characters")
        return v

    @field_validator("storage_key")
    @classmethod
    def _storage_key_valid(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("OCR_STORAGE_KEY must be base64") from exc
        if len(raw) != 32:
            raise ValueError("OCR_STORAGE_KEY must decode to exactly 32 bytes")
        return v

    @model_validator(mode="after")
    def _llm_stays_local(self) -> "Settings":
        if self.llm_url and not self.llm_allow_remote and not is_local_url(self.llm_url):
            raise ValueError("OCR_LLM_URL points outside this machine and the local network, so documents would "
                             "leave your hardware. Use a local address, or set OCR_LLM_ALLOW_REMOTE=true to allow it")
        if self.llm_api not in ("ollama", "openai"):
            raise ValueError("OCR_LLM_API must be 'ollama' or 'openai'")
        if self.date_order not in ("dmy", "mdy"):
            raise ValueError("OCR_DATE_ORDER must be 'dmy' or 'mdy'")
        return self

    @property
    def storage_key_bytes(self) -> bytes:
        return base64.b64decode(self.storage_key)

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'ocr.db'}"

    @property
    def inputs_dir(self) -> Path:
        return self.data_dir / "inputs"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
