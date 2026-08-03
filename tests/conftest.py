"""Test fixtures. Forces an isolated temp SQLite DB before importing the app."""

from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import tempfile

# Must be set before importing anything under `central` (engine binds at import).
# The filename carries the PID so concurrent pytest processes -- a second run in
# another checkout, or pytest-xdist workers -- each get their own database. A
# single fixed name here is shared machine-wide (tempdir is not per-worktree),
# so overlapping runs used to drop each other's tables mid-test and fail in
# ways that look like real bugs.
_TMP_DB = os.path.join(tempfile.gettempdir(), f"printer_nanny_test.{os.getpid()}.sqlite3")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402

from central.db import Base, SessionLocal, engine  # noqa: E402


@atexit.register
def _cleanup_tmp_db() -> None:
    """Remove this process's scratch DB. Per-PID names would otherwise pile up."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(_TMP_DB + suffix)
        except OSError:
            pass


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def git_file_mode():
    """A callable giving a path's mode as *git* records it, or None.

    The executable bit on a shipped script is a property of the repository, not
    of whichever filesystem a checkout happened to land on. Git on Windows does
    not materialise the bit in the working tree, so ``stat()`` there says
    nothing about what ships -- it reports 0o666 for a file git has as 100755,
    and a test asserting on it fails for a reason that has nothing to do with
    the packaging it is trying to guard.

    Asking git is the same question the packagers and CI ask. Returns None when
    there is no git checkout to ask (a source tarball, an export), so callers
    can skip rather than fail on a question that cannot be posed.
    """

    def mode(path) -> "str | None":
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-s", "--", str(path)],
                cwd=str(_REPO_ROOT), capture_output=True, text=True,
            )
        except OSError:  # no git on PATH
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return proc.stdout.split()[0]

    return mode


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
