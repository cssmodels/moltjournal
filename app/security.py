"""API key minting and verification.

Keys look like `molt_<prefix>_<secret>`. Only the SHA-256 of the secret is
stored; the plaintext is shown once at creation and is unrecoverable after.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

KEY_NAMESPACE = "molt"


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, key_hash)."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    plaintext = f"{KEY_NAMESPACE}_{prefix}_{secret}"
    return plaintext, prefix, hash_secret(secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def split_api_key(plaintext: str) -> tuple[str, str] | None:
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_NAMESPACE:
        return None
    return parts[1], parts[2]


def verify_secret(secret: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), expected_hash)


def content_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
