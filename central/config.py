"""Centralized configuration (pydantic-settings, reads environment / .env)."""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import stat
from functools import lru_cache
from typing import Optional

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("central.config")

# Signing keys that have appeared in this repository: this module's own former
# default, .env.example, and docker-compose.yml. Every one of them is public
# knowledge, and SECRET_KEY is not just a session signer -- central/secrets.py
# derives the Fernet key for encryption-at-rest from it. So a deployment running
# on one of these has forgeable admin sessions AND readable SMTP / OAuth /
# FreeScout / SNMPv3 credentials, from a value anyone can read on GitHub.
PUBLISHED_SECRET_KEYS = frozenset({"", "dev-insecure-change-me", "change-me-in-prod"})

# Filename of the generated key, kept beside the SQLite database (see
# ``Settings.secret_key_path``). A dotfile so ``ls`` in a dev checkout is not
# cluttered; 0600 so it is not readable by other local accounts.
SECRET_KEY_FILENAME = ".printer-nanny-secret-key"


def sqlite_file_path(database_url: str) -> str:
    """The on-disk path a SQLite URL points at, or "" for in-memory / non-SQLite.

    ``sqlite:///rel.db`` -> ``rel.db``; ``sqlite:////abs.db`` -> ``/abs.db``;
    ``sqlite://`` and ``sqlite:///:memory:`` -> "" (nothing durable to sit
    beside). Parsed by hand rather than via ``sqlalchemy.make_url`` to keep this
    module importable before the ORM is.
    """
    scheme, sep, rest = database_url.partition("://")
    if not sep or scheme.split("+", 1)[0].lower() != "sqlite":
        return ""
    rest = rest.split("?", 1)[0]
    path = rest[1:] if rest.startswith("/") else rest
    return "" if path in ("", ":memory:") else path


def is_ephemeral_database(database_url: str) -> bool:
    """True for a database that provably cannot outlive the process.

    Only in-memory SQLite qualifies. This is the one deployment shape where
    generating a throwaway signing key is *correct* rather than a guess: there
    is no persisted session, no stored credential and no next boot to keep a key
    for. It is what the test suite and ad-hoc `sqlite://` tooling run on.
    """
    scheme = database_url.partition("://")[0].split("+", 1)[0].lower()
    return scheme == "sqlite" and not sqlite_file_path(database_url)


def _mint() -> str:
    return _secrets.token_urlsafe(48)


def _warn_if_readable_by_others(path: str) -> None:
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning(
            "%s is readable beyond its owner (mode %o) -- it signs every dashboard "
            "session and decrypts every stored credential. chmod 600 it.",
            path, stat.S_IMODE(mode),
        )


def _read_key_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError:
        return ""
    if key:
        _warn_if_readable_by_others(path)
    return key


def _read_or_create_key_file(path: str) -> str:
    """Read the persisted key, generating one on first run. "" if impossible.

    ``O_CREAT | O_EXCL`` is what makes this safe when the api and worker
    containers start together: exactly one creator wins and the loser reads what
    the winner wrote, rather than the two silently ending up with different keys
    (which would mean sessions minted by one being rejected by the other, and
    settings encrypted by one being undecryptable by the other).
    """
    existing = _read_key_file(path)
    if existing:
        return existing
    key = _mint()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (key + "\n").encode("ascii"))
        finally:
            os.close(fd)
    except FileExistsError:
        return _read_key_file(path)
    except OSError as exc:
        log.error("cannot persist a generated SECRET_KEY to %s: %s", path, exc)
        return ""
    log.warning(
        "no SECRET_KEY was set, so one was generated and stored at %s (mode 0600). "
        "BACK IT UP WITH THE DATABASE: without this file, stored credentials "
        "cannot be decrypted and every session is invalidated.",
        path,
    )
    return key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database. Default to a local SQLite file so the project runs with no setup.
    database_url: str = "sqlite:///./printer_nanny.sqlite3"

    # Dashboard session signing AND the root of encryption-at-rest
    # (central/secrets.py). Blank by default rather than a placeholder: an unset
    # key is *resolved* by ``resolve_secret_key`` -- generated once and persisted
    # 0600 beside the SQLite database -- so no install ever runs on a value that
    # ships in this repository. See PUBLISHED_SECRET_KEYS above.
    secret_key: str = ""

    # Where a generated key is persisted. Defaults to a dotfile beside the
    # SQLite database file; set it explicitly to put the key anywhere else, or
    # to give a non-SQLite deployment somewhere to generate one.
    secret_key_file: str = ""

    # Override for the session cookie's Secure flag. None = follow the database
    # backend (see ``secure_cookies``).
    session_cookie_secure: Optional[bool] = None

    # Verbosity of this project's own loggers (central.* / printer_nanny.*), see
    # central/logging_config.py. Env rather than the DB-backed settings in
    # central/runtime.py because it is a bootstrap concern: the first thing worth
    # logging is a database that will not answer, which is exactly when a
    # DB-backed value cannot be read. It moves nothing outside those namespaces,
    # so turning it down to DEBUG cannot switch on SQLAlchemy's bound-parameter
    # logging or httpx's request URLs (a webhook URL is a credential).
    log_level: str = "INFO"

    # An agent is considered offline if no heartbeat within this many seconds.
    agent_offline_grace_seconds: int = 300

    # Email (SMTP) channel.
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "printer-nanny@example.com"
    smtp_use_tls: bool = False

    # FreeScout channel.
    freescout_base_url: str = ""
    freescout_api_key: str = ""
    freescout_mailbox_id: int = 1

    # Microsoft Teams channel (stub).
    teams_webhook_url: str = ""

    # How ``secret_key`` was arrived at: "environment" | "file" | "ephemeral" |
    # "unresolved". Private so it cannot be set from the environment -- it is a
    # statement about what happened, not a knob.
    _secret_key_source: str = PrivateAttr(default="environment")

    @property
    def secret_key_source(self) -> str:
        return self._secret_key_source

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        """Postgres-backed, i.e. the Docker Compose stack.

        This is a statement about the DATABASE BACKEND and nothing more. It used
        to double as the security signal for ``assert_secure``, which was wrong:
        SQLite is a supported small-deployment backend here, so "SQLite means
        dev" let a real install run on a published signing key. Key safety is now
        decided by ``resolve_secret_key`` / ``assert_secure``, which do not
        consult the backend at all.
        """
        return not self.is_sqlite

    @property
    def secure_cookies(self) -> bool:
        """Mark the session cookie Secure (TLS terminated at the reverse proxy).

        Defaults to following the backend because that is what the deployment
        shapes look like: Postgres means the Compose stack behind Caddy, SQLite
        usually means plain HTTP on a LAN or localhost. Getting this wrong in the
        strict direction is a silent lockout -- a Secure cookie is stored by the
        browser and then never sent back over http, so every login appears to
        succeed and bounces straight back to /login. ``SESSION_COOKIE_SECURE=1``
        turns it on for a SQLite install that *is* behind TLS; =0 turns it off
        for a Postgres install that is not.
        """
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    def secret_key_path(self) -> str:
        """Where a generated key would live, or "" if there is nowhere to put it."""
        explicit = self.secret_key_file.strip()
        if explicit:
            return explicit
        db_path = sqlite_file_path(self.database_url)
        if not db_path:
            return ""
        return os.path.join(
            os.path.dirname(os.path.abspath(db_path)), SECRET_KEY_FILENAME
        )

    def resolve_secret_key(self) -> None:
        """Turn an unset/published SECRET_KEY into a real one, or leave it to fail.

        Called once, from ``get_settings``, because the value is read at import
        time by ``SessionMiddleware`` and by ``central.secrets``.

        Generating rather than merely warning is the point: the failure this
        guards against is an operator who did *nothing* (copied .env.example, ran
        it), so a fix that requires them to act does not reach them. Where no key
        can be persisted -- Postgres, a read-only filesystem -- nothing is
        invented and ``assert_secure`` refuses to boot instead. A per-process
        random key there would be worse than useless: the api and worker would
        disagree, and every restart would invalidate every session and orphan
        every encrypted credential.
        """
        if self.secret_key not in PUBLISHED_SECRET_KEYS:
            self._secret_key_source = "environment"
            return
        if is_ephemeral_database(self.database_url):
            # Nothing persists, so nothing needs the same key twice.
            self.secret_key = _mint()
            self._secret_key_source = "ephemeral"
            return
        path = self.secret_key_path()
        key = _read_or_create_key_file(path) if path else ""
        if key:
            self.secret_key = key
            self._secret_key_source = "file"
        else:
            self._secret_key_source = "unresolved"

    def assert_secure(self) -> None:
        """Refuse to run on a signing key an attacker already has.

        Deliberately NOT conditioned on the database backend any more. It signs
        dashboard sessions and derives the encryption-at-rest key, so the
        question "is this key public" has the same answer on SQLite as on
        Postgres.
        """
        if self.secret_key and self.secret_key not in PUBLISHED_SECRET_KEYS:
            return
        where = self.secret_key_path()
        hint = (
            f"A key can be generated automatically, but {where} could not be "
            "written -- fix its permissions and restart, or set SECRET_KEY."
            if where
            else "This backend has nowhere to generate one (SECRET_KEY_FILE names "
                 "a writable path if you want that)."
        )
        raise RuntimeError(
            "SECRET_KEY is unset or set to a value published in this repository. "
            "It signs dashboard sessions and derives the key that encrypts every "
            "stored credential, so no deployment may run on it.\n"
            "  SECRET_KEY=$(python -c \"import secrets;print(secrets.token_urlsafe(48))\")\n"
            + hint
        )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.resolve_secret_key()
    return s


settings = get_settings()
