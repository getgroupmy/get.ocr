"""Runtime configuration. Every setting can be overridden with an OCR_* env var."""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    min_confidence: float = 0.5  # lines the model is less sure of than this are dropped
    pdf_dpi: int = 200  # resolution PDF pages are rendered at before reading
    max_image_side: int = 3000  # larger images and pages are scaled down to this

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
