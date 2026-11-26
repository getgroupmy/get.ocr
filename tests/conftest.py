import base64
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ocr-test-")
os.environ.update({
    "OCR_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
    "OCR_STORAGE_KEY": base64.b64encode(b"k" * 32).decode(),
    "OCR_DATA_DIR": _tmp,
    "OCR_SYNC_MAX_PAGES": "2",
    "OCR_ADMIN_TRIES": "1000",  # the tests send many wrong tokens from one address
    "OCR_MAX_PAGES": "3",
    "OCR_LLM_URL": "http://127.0.0.1:9",  # nothing listens here: extraction is off unless a test fakes the model
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = {"X-Admin-Token": os.environ["OCR_ADMIN_TOKEN"]}


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_client(client, name="get-ride", webhook_url=None):
    r = client.post("/admin/api/clients", json={"name": name, "webhook_url": webhook_url}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="session")
def api_key(client):
    return {"X-API-Key": make_client(client)["api_key"]}


@pytest.fixture(scope="session")
def need_engine():
    from app import engine
    if not engine.available():
        pytest.skip("OCR engine not installed")
