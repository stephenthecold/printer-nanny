"""Centralized configuration (pydantic-settings, reads environment / .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database. Default to a local SQLite file so the project runs with no setup.
    database_url: str = "sqlite:///./printer_nanny.sqlite3"

    # Dashboard session signing.
    secret_key: str = "dev-insecure-change-me"

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

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        """A non-SQLite database means a real deployment (Docker/Postgres)."""
        return not self.is_sqlite

    @property
    def secure_cookies(self) -> bool:
        """Mark the session cookie Secure in production (TLS via the reverse proxy)."""
        return self.is_production

    def assert_secure(self) -> None:
        """Fail fast if a real deployment is still using a known-insecure SECRET_KEY."""
        insecure = {"dev-insecure-change-me", "change-me-in-prod", ""}
        if self.is_production and self.secret_key in insecure:
            raise RuntimeError(
                "SECRET_KEY is unset or a known default but DATABASE_URL is not SQLite. "
                "Set a strong SECRET_KEY (e.g. `python -c \"import secrets;print(secrets.token_urlsafe(48))\"`) "
                "before running in production."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
