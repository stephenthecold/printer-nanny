"""`alembic upgrade head` must survive a `%` anywhere in DATABASE_URL.

A percent-encoded password (`p%40ss`) is the ordinary way to put an `@`, `/` or
`#` in a Postgres URL, and `docker-compose.yml` builds `DATABASE_URL` straight
out of `POSTGRES_PASSWORD`. Routing that string through alembic's ConfigParser
(`config.set_main_option("sqlalchemy.url", ...)`) makes BasicInterpolation read
`%t` as a directive and raise `ValueError: invalid interpolation syntax` while
`migrations/env.py` is still *importing* -- so the api container dies before a
single migration runs, on nothing but a strong password.

These drive the real `alembic` CLI in a subprocess rather than reaching into
ConfigParser, because the failure lived in the wiring between the two: only an
actual `python -m alembic <cmd>` exercises env.py the way a deployment does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# A percent-encoded password, exactly as an operator would write `p@ss%word`.
PCT_PASSWORD_URL = "postgresql+psycopg://pn:p%40ss%25word@db.example:5432/printer_nanny"


def run_alembic(*args: str, database_url: str) -> subprocess.CompletedProcess:
    """Invoke the alembic CLI against this checkout, from the repo root.

    cwd is the repo root because `alembic.ini` sets `script_location = migrations`
    and `prepend_sys_path = .`; PYTHONPATH pins `central` to this same tree, since
    an editable install otherwise resolves it to whichever checkout was installed.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["SECRET_KEY"] = "test-secret-not-used-by-migrations"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _report(label: str, proc: subprocess.CompletedProcess) -> str:
    return f"{label} exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"


@pytest.fixture()
def percent_sqlite_url(tmp_path: Path) -> str:
    """An absolute SQLite URL whose path contains a literal `%`."""
    return "sqlite:///" + str(tmp_path / "pct%test.sqlite3")


def test_upgrade_head_survives_percent_in_database_url(percent_sqlite_url: str) -> None:
    proc = run_alembic("upgrade", "head", database_url=percent_sqlite_url)
    assert "invalid interpolation syntax" not in proc.stderr, _report("upgrade head", proc)
    assert proc.returncode == 0, _report("upgrade head", proc)
    # Not just "it exited 0": the schema has to actually be there.
    assert "0001_baseline" in proc.stderr, _report("upgrade head", proc)

    current = run_alembic("current", database_url=percent_sqlite_url)
    assert current.returncode == 0, _report("current", current)
    assert "(head)" in current.stdout, _report("current", current)


def test_current_and_history_survive_percent_in_database_url(percent_sqlite_url: str) -> None:
    """`current` and `history` load env.py too, so they died on the same line."""
    for args in (("current",), ("history",), ("history", "--indicate-current")):
        proc = run_alembic(*args, database_url=percent_sqlite_url)
        assert "invalid interpolation syntax" not in proc.stderr, _report(str(args), proc)
        assert proc.returncode == 0, _report(str(args), proc)
    # `history` reads the script directory, so it lists revisions with no DB at all.
    history = run_alembic("history", database_url=percent_sqlite_url)
    assert "0001_baseline" in history.stdout, _report("history", history)


def test_percent_encoded_password_reaches_sqlalchemy_intact() -> None:
    """The real-world shape: `%40` in a Postgres password, offline so no DB is needed.

    Offline (`--sql`) mode never connects, so this proves the URL survived env.py
    and was parsed by SQLAlchemy as Postgres -- the exact path the api container
    takes on boot, minus the database. It stops at 0002 because migration 0003
    calls `sa.inspect(bind)`, which has never worked against `--sql`'s
    MockConnection; that is unrelated to the URL and true of any password.
    """
    proc = run_alembic("upgrade", "0002_readings_brin", "--sql", database_url=PCT_PASSWORD_URL)
    assert "invalid interpolation syntax" not in proc.stderr, _report("upgrade --sql", proc)
    assert proc.returncode == 0, _report("upgrade --sql", proc)
    assert "CREATE TABLE clients" in proc.stdout, _report("upgrade --sql", proc)
    # SERIAL rather than INTEGER proves the Postgres dialect was selected, i.e.
    # the whole URL -- not just its scheme -- came through.
    assert "SERIAL" in proc.stdout, _report("upgrade --sql", proc)


def test_plain_url_still_upgrades_and_logs(tmp_path: Path) -> None:
    """Guard the ordinary path: no `%`, and alembic's INI logging config still applies.

    The INFO lines come from `[logger_alembic] level = INFO` in alembic.ini via
    `fileConfig`. Asserting on them is what proves removing the `set_main_option`
    call did not disturb logging setup -- `fileConfig` re-reads the ini from disk
    and never sees an in-memory option, but that is the kind of claim worth
    checking rather than reasoning about.
    """
    plain = "sqlite:///" + str(tmp_path / "plain.sqlite3")
    proc = run_alembic("upgrade", "head", database_url=plain)
    assert proc.returncode == 0, _report("upgrade head", proc)
    assert "INFO  [alembic.runtime.migration]" in proc.stderr, _report("upgrade head", proc)
