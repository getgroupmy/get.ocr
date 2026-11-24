import pytest

from app import engine
from app.documents import parse_pages
from app.errors import ApiError


def test_parse_pages():
    assert parse_pages(None, 3) == [1, 2, 3]
    assert parse_pages(" ", 2) == [1, 2]
    assert parse_pages("2", 3) == [2]
    assert parse_pages("3,1-2,2", 5) == [1, 2, 3]
    assert parse_pages("2-99", 4) == [2, 3, 4]  # an open-ended range stops at the last page
    for bad in ("0", "3-1", "a", "1,,2", "-2", "9"):
        with pytest.raises(ApiError):
            parse_pages(bad, 4)


def _line(text, x, y, h=20, w=100):
    return {"text": text, "confidence": 0.9, "box": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]}


def test_layout_joins_a_row_left_to_right():
    lines = [_line("Total", 300, 104), _line("RM 12.00", 600, 100), _line("Receipt", 300, 20), _line("Thanks", 300, 200)]
    assert engine.layout_text(lines) == "Receipt\nTotal RM 12.00\nThanks"
    assert engine.layout_text([]) == ""


def test_webhook_signature_matches_the_documented_recipe():
    import hashlib
    import hmac

    from app import webhooks
    want = "sha256=" + hmac.new(b"s3cret", b"1700000000." + b'{"a":1}', hashlib.sha256).hexdigest()
    assert webhooks.sign(b"s3cret", "1700000000", b'{"a":1}') == want


def test_settings_reject_weak_secrets():
    from pydantic import ValidationError

    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(admin_token="short", storage_key="a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(admin_token="x" * 32, storage_key="not-base64", _env_file=None)
