"""Encryption at rest (AES-256-GCM) and hashing of API keys."""
from __future__ import annotations
from typing import Union

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_NONCE = 12


def encrypt(data: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + AESGCM(get_settings().storage_key_bytes).encrypt(nonce, data, aad or None)


def decrypt(blob: bytes, aad: bytes = b"") -> bytes:
    return AESGCM(get_settings().storage_key_bytes).decrypt(blob[:_NONCE], blob[_NONCE:], aad or None)


def sha256_hex(value: Union[str, bytes]) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()
