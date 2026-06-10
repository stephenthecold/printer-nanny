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
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from central.config import settings as _env

log = logging.getLogger("central.secrets")

ENC_PREFIX = "enc:v1:"

# Domain-separation tag so the Fernet key differs from any other key
# material someone might derive from the same SECRET_KEY later.
_KDF_TAG = b"printer-nanny:settings-encryption:v1:"


def _fernet() -> Fernet:
    digest = hashlib.sha256(_KDF_TAG + _env.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


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
    token = value[len(ENC_PREFIX):]  # type: ignore[index]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.warning(
            "could not decrypt a stored secret (SECRET_KEY rotated?) -- "
            "treating it as unset; re-enter the credential in Settings"
        )
        return ""
