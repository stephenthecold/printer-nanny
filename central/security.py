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


# --- Agent claim codes -------------------------------------------------------
# A claim code is what travels to the site: an operator pastes it into an
# installer, the agent redeems it once for a real API key, and it is dead
# thereafter. Same storage discipline as API keys -- only the SHA-256 digest is
# persisted, so a database dump yields nothing redeemable.
#
# Entropy is the primary defence and is deliberately not reduced for typing
# comfort: 32 urlsafe bytes is ~192 bits, so guessing one is not a threat model
# worth rate-limiting against. The properties that actually need enforcing are
# the ones a guess can't help with -- single use and a short TTL -- and both are
# enforced in the database rather than here.
CLAIM_CODE_PREFIX = "pnc_"


def generate_claim_code() -> str:
    return CLAIM_CODE_PREFIX + secrets.token_urlsafe(32)


def hash_claim_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_enroll_key() -> str:
    """A client-scoped workstation enrollment key.

    Prefixed so an operator who finds one in an installer, a log or a ticket can
    tell what it is and revoke the right thing. Longer than a claim code because
    this one is long-lived and multi-use -- it is not protected by a short TTL.
    """
    return "pnw_" + secrets.token_urlsafe(32)


def hash_enroll_key(key: str) -> str:
    """SHA-256, matching hash_api_key: a database dump must not yield a working
    enrollment."""
    return hashlib.sha256((key or "").encode()).hexdigest()
