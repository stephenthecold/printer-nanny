"""Admin DB backup & restore.

The SQLite codepath is fully exercised against a real on-disk DB. The
Postgres codepath is verified by stubbing the pg_dump/pg_restore seams --
running real pg_* binaries in CI isn't worth the container overhead, and
the route's logic above the subprocess call is what we own.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from central import models as m
from central.dashboard import backup_routes as br
from central.main import app
from central.security import hash_password


def _admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"),
                  role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


def _tech(db) -> TestClient:
    db.add(m.User(username="tech", password_hash=hash_password("pw"),
                  role=m.UserRole.tech))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "tech", "password": "pw"},
             follow_redirects=False)
    return cli


def test_backup_page_admin_only(db):
    cli = _admin(db)
    resp = cli.get("/admin/backup")
    assert resp.status_code == 200


def test_backup_page_tech_redirected(db):
    cli = _tech(db)
    resp = cli.get("/admin/backup", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_restore_requires_typed_confirm(db, monkeypatch):
    """Click-through protection: an upload alone is NOT enough."""
    called = []
    monkeypatch.setattr(br, "_pg_restore_from_file",
                        lambda p: called.append(p))
    cli = _admin(db)
    resp = cli.post(
        "/admin/backup/restore",
        data={"confirm": "yes"},  # wrong phrase
        files={"backup_file": ("x.dump", b"bytes", "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert called == []


def test_restore_empty_upload_rejected(db):
    cli = _admin(db)
    resp = cli.post(
        "/admin/backup/restore",
        data={"confirm": "RESTORE"},
        files={"backup_file": ("x.dump", b"", "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_sqlite_backup_download_streams_file(db, monkeypatch, tmp_path):
    """End-to-end: write a fake DB on disk, configure settings to point at
    it, hit /admin/backup/download, assert we get the bytes back."""
    src = tmp_path / "pn.db"
    src.write_bytes(b"SQLITE-FAKE-CONTENTS")
    monkeypatch.setattr(br, "_is_sqlite", lambda: True)
    monkeypatch.setattr(br, "_sqlite_path", lambda: src)
    cli = _admin(db)
    resp = cli.get("/admin/backup/download")
    assert resp.status_code == 200
    assert resp.content == b"SQLITE-FAKE-CONTENTS"
    assert "attachment" in resp.headers["content-disposition"]
    assert ".sqlite" in resp.headers["content-disposition"]


def test_sqlite_restore_replaces_file_atomically(db, monkeypatch, tmp_path):
    dest = tmp_path / "pn.db"
    dest.write_bytes(b"OLD")
    monkeypatch.setattr(br, "_is_sqlite", lambda: True)
    monkeypatch.setattr(br, "_sqlite_path", lambda: dest)
    cli = _admin(db)
    resp = cli.post(
        "/admin/backup/restore",
        data={"confirm": "RESTORE"},
        files={"backup_file": ("new.sqlite", b"NEW-BYTES",
                               "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert dest.read_bytes() == b"NEW-BYTES"
    # Staging file should be cleaned up.
    assert not (dest.with_suffix(dest.suffix + ".restoring").exists())


def test_postgres_download_handles_pg_dump_failure(db, monkeypatch):
    monkeypatch.setattr(br, "_is_sqlite", lambda: False)
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: "/usr/bin/pg_dump" if name == "pg_dump" else None)

    def boom(p):
        raise RuntimeError("pg_dump failed (1): connection refused")

    monkeypatch.setattr(br, "_pg_dump_to_file", boom)
    cli = _admin(db)
    resp = cli.get("/admin/backup/download", follow_redirects=False)
    assert resp.status_code == 303  # bounced back with an error
    assert resp.headers["location"] == "/admin/backup"


def test_postgres_restore_invokes_pg_restore(db, monkeypatch):
    monkeypatch.setattr(br, "_is_sqlite", lambda: False)
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: "/usr/bin/pg_restore")

    captured = []
    monkeypatch.setattr(br, "_pg_restore_from_file",
                        lambda path: captured.append(Path(path).read_bytes()))
    cli = _admin(db)
    resp = cli.post(
        "/admin/backup/restore",
        data={"confirm": "RESTORE"},
        files={"backup_file": ("dump", b"BINARY-DUMP-BYTES",
                               "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert captured == [b"BINARY-DUMP-BYTES"]
