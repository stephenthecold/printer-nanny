"""Password hashing, agent API-key hashing, and token generation."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from passlib.context import CryptContext

# pbkdf2_sha256 is pure-Python (hashlib-backed): no native bcrypt build, no
# 72-byte limit, and dependable across platforms/Python versions.
_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


# --- Dashboard user passwords ------------------------------------------------
def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd.verify(password, password_hash)
    except ValueError:
        return False


# --- Agent API keys ----------------------------------------------------------
# Agent keys are opaque high-entropy tokens. We only store a SHA-256 digest so a
# DB leak doesn't expose usable credentials; lookup is by digest.
def generate_api_key() -> str:
    return "pn_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def api_key_matches(api_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key), stored_hash)
