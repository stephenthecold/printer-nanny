"""Encryption-at-rest for operator credentials (design doc §846).

Secrets stored in the database -- SMTP passwords, OAuth tokens, FreeScout
API keys, Slack/Teams webhook URLs, SNMPv3 USM passwords -- are encrypted
with a Fernet key derived from SECRET_KEY. A database dump alone no longer
exposes credentials; an attacker needs the environment's SECRET_KEY too.

Encrypted values are stored as ``enc:v1:<fernet-token>`` so they're
self-identifying: ``decrypt_value`` passes anything without the prefix
through unchanged, which is what makes the migration lazy -- legacy
plaintext rows keep working and get re-encrypted on the next settings save
(or by ``encrypt_existing_settings`` at API startup).

Caveat the operator must know (documented in the Settings UI): rotating
SECRET_KEY makes existing secrets undecryptable. They aren't lost loudly --
``decrypt_value`` returns "" for tokens it can't open -- but every secret
must be re-entered after a key rotation.

The one rotation that must NOT cost the operator their credentials is the
one this code performs on their behalf: an install that was running on a
SECRET_KEY published in this repository gets a generated one on upgrade
(central/config.py), and its stored secrets are still sealed under the old,
public key. So ``decrypt_value`` falls back to those published keys, and
``rewrap_if_legacy`` re-seals under the live key on the next write. Being able
to open a value encrypted with a public key concedes nothing -- anyone could
already -- while not being able to open it would silently blank the operator's
SMTP password, OAuth tokens and SNMPv3 credentials at the moment they upgrade.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken

from central.config import PUBLISHED_SECRET_KEYS
from central.config import settings as _env

log = logging.getLogger("central.secrets")

ENC_PREFIX = "enc:v1:"

# Domain-separation tag so the Fernet key differs from any other key
# material someone might derive from the same SECRET_KEY later.
_KDF_TAG = b"printer-nanny:settings-encryption:v1:"

_warned_legacy = False


def _fernet_for(key: str) -> Fernet:
    digest = hashlib.sha256(_KDF_TAG + key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    # Recomputed per call rather than cached so a test (or a future rotation
    # path) that swaps settings.secret_key takes effect immediately. sha256 +
    # a Fernet constructor is microseconds.
    return _fernet_for(_env.secret_key)


def _legacy_fernets() -> List[Fernet]:
    return [_fernet_for(k) for k in sorted(PUBLISHED_SECRET_KEYS)
            if k and k != _env.secret_key]


def _open(token: bytes) -> "Optional[str]":
    """Plaintext under the live key, or None."""
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def _open_legacy(token: bytes) -> "Optional[str]":
    """Plaintext under a published key this install used to run on, or None."""
    global _warned_legacy
    for fernet in _legacy_fernets():
        try:
            plaintext = fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError):
            continue
        if not _warned_legacy:
            _warned_legacy = True
            log.warning(
                "a stored secret was still encrypted under a SECRET_KEY published "
                "in this repository; it has been read and will be re-encrypted "
                "under the current key on its next save. Re-enter any credential "
                "that mattered -- the old ciphertext was never private."
            )
        return plaintext
    return None


def encrypt_value(value: str) -> str:
    """Encrypt a secret for storage. Empty strings stay empty (no point
    storing a token that decrypts to nothing -- truthiness checks all over
    the settings UI rely on empty meaning 'not set')."""
    if not value:
        return value
    return ENC_PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def decrypt_value(value: object) -> object:
    """Decrypt a stored secret; pass non-encrypted values through unchanged.

    A token that fails to open (SECRET_KEY rotated, row corrupted) returns ""
    so the system behaves as 'secret not set' instead of crashing every page
    that loads settings. The warning in the log is the operator's cue.
    """
    if not is_encrypted(value):
        return value
    token = value[len(ENC_PREFIX):].encode("ascii")  # type: ignore[index]
    plaintext = _open(token)
    if plaintext is not None:
        return plaintext
    plaintext = _open_legacy(token)
    if plaintext is not None:
        return plaintext
    log.warning(
        "could not decrypt a stored secret (SECRET_KEY rotated?) -- "
        "treating it as unset; re-enter the credential in Settings"
    )
    return ""


def rewrap_if_legacy(value: object) -> "Optional[str]":
    """Re-seal a value that only opens under a published key. None if untouched.

    Returning None for "already correct" and for "cannot be opened at all" keeps
    the caller from rewriting a row it does not understand: a token encrypted
    under an operator's own previous key must be left exactly as it is, because
    the operator may still restore that key.
    """
    if not is_encrypted(value):
        return None
    token = value[len(ENC_PREFIX):].encode("ascii")  # type: ignore[index]
    if _open(token) is not None:
        return None
    plaintext = _open_legacy(token)
    return None if plaintext is None else encrypt_value(plaintext)
