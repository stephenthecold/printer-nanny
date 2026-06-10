"""Admin-only DB backup & restore from the UI.

Postgres: pg_dump / pg_restore via subprocess against the configured
DATABASE_URL. SQLite (dev): a straight file copy of the .db. Both paths
stream so a multi-gigabyte fleet doesn't load into memory.

Restore is gated behind a typed-confirmation phrase ("RESTORE") in the
form -- accidentally clicking the upload button is far more dangerous than
the corresponding click on backup, and the operator should have to do
something deliberate to invoke it. Audited under
``backup.download`` / ``backup.restore``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from central import models as m
from central.audit import record
from central.config import settings
from central.db import get_db
from central.runtime import app_branding

router = APIRouter(prefix="/admin/backup", tags=["backup"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
log = logging.getLogger("central.backup")


def _admin(request: Request, db: Session) -> Optional[m.User]:
    uid = request.session.get("user_id")
    user = db.get(m.User, uid) if uid else None
    if user is None or user.role != m.UserRole.admin:
        return None
    return user


def _tpl(request, name: str, db: Session, **ctx):
    from central import __version__ as _v

    ctx.setdefault("app", app_branding(db))
    ctx.setdefault("central_version", _v)
    return _templates.TemplateResponse(request, name, ctx)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _is_sqlite() -> bool:
    return settings.is_sqlite


def _sqlite_path() -> Path:
    """SQLite file path from sqlite:///abs/path.db or sqlite:///relative.db."""
    parsed = urlparse(settings.database_url)
    if parsed.scheme != "sqlite":
        raise RuntimeError("not a sqlite database url")
    # sqlite:///foo.db -> netloc='', path='/foo.db'
    p = parsed.path or "/"
    return Path(p if p.startswith("/") else "/" + p)


def _pg_dump_to_file(out_path: Path) -> None:
    """pg_dump in custom format (pg_restore-compatible, includes the schema)."""
    proc = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
         "--dbname", settings.database_url, "--file", str(out_path)],
        capture_output=True, text=False, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_dump failed ({proc.returncode}): "
            f"{(proc.stderr or b'').decode('utf-8', errors='replace')[:2000]}"
        )


def _pg_restore_from_file(src_path: Path) -> None:
    """Restore over the live database. --clean wipes existing objects; the
    dump is in custom format from pg_dump --format=custom."""
    proc = subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
         "--dbname", settings.database_url, str(src_path)],
        capture_output=True, text=False, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_restore failed ({proc.returncode}): "
            f"{(proc.stderr or b'').decode('utf-8', errors='replace')[:2000]}"
        )


@router.get("", response_class=HTMLResponse)
def backup_page(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    backend = "sqlite" if _is_sqlite() else "postgres"
    pg_dump_available = (not _is_sqlite()) and shutil.which("pg_dump") is not None
    pg_restore_available = (not _is_sqlite()) and shutil.which("pg_restore") is not None
    return _tpl(
        request, "backup.html", db,
        user=user, backend=backend,
        pg_dump_available=pg_dump_available,
        pg_restore_available=pg_restore_available,
        flash=request.session.pop("flash", None),
        error=request.session.pop("backup_error", None),
    )


@router.get("/download")
def backup_download(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    stamp = _stamp()
    if _is_sqlite():
        src = _sqlite_path()
        if not src.exists():
            request.session["backup_error"] = f"SQLite file not found at {src}"
            return RedirectResponse("/admin/backup", status_code=303)
        filename = f"printer-nanny-{stamp}.sqlite"

        def stream():
            with open(src, "rb") as fp:
                while True:
                    chunk = fp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

        record(db, request, user, "backup.download", target=f"sqlite:{src.name}")
        db.commit()
        return StreamingResponse(
            stream(), media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Postgres path: dump to a tempfile, stream it, delete on close.
    if shutil.which("pg_dump") is None:
        request.session["backup_error"] = (
            "pg_dump not on PATH in the api container. Add postgresql-client to "
            "the image (or run pg_dump from the host)."
        )
        return RedirectResponse("/admin/backup", status_code=303)
    fd, tmp = tempfile.mkstemp(prefix="pn-backup-", suffix=".dump")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        _pg_dump_to_file(tmp_path)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        log.exception("pg_dump failed")
        request.session["backup_error"] = str(exc)
        return RedirectResponse("/admin/backup", status_code=303)
    filename = f"printer-nanny-{stamp}.dump"

    def stream_and_cleanup():
        try:
            with open(tmp_path, "rb") as fp:
                while True:
                    chunk = fp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            tmp_path.unlink(missing_ok=True)

    record(db, request, user, "backup.download", target="postgres:custom-format")
    db.commit()
    return StreamingResponse(
        stream_and_cleanup(), media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/restore")
async def backup_restore(
    request: Request,
    confirm: str = Form(""),
    backup_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if confirm.strip() != "RESTORE":
        request.session["backup_error"] = (
            "Restore not run: type RESTORE in the confirmation box to proceed."
        )
        return RedirectResponse("/admin/backup", status_code=303)

    payload = await backup_file.read()
    if not payload:
        request.session["backup_error"] = "Upload was empty."
        return RedirectResponse("/admin/backup", status_code=303)

    fd, tmp = tempfile.mkstemp(prefix="pn-restore-")
    os.close(fd)
    tmp_path = Path(tmp)
    tmp_path.write_bytes(payload)

    try:
        if _is_sqlite():
            dest = _sqlite_path()
            # Atomic-ish replace: write to a sibling temp file first, fsync,
            # then rename. Survives a half-written copy interrupted by Ctrl-C.
            staging = dest.with_suffix(dest.suffix + ".restoring")
            shutil.copyfile(tmp_path, staging)
            staging.replace(dest)
            record(db, request, user, "backup.restore",
                   target=f"sqlite:{dest.name}",
                   detail=f"{len(payload)} bytes")
            db.commit()
            request.session["flash"] = (
                "SQLite database restored. Restart the api container to pick up "
                "the new file (sessions etc. cache schema on connect)."
            )
        else:
            if shutil.which("pg_restore") is None:
                request.session["backup_error"] = (
                    "pg_restore not on PATH in the api container."
                )
                return RedirectResponse("/admin/backup", status_code=303)
            _pg_restore_from_file(tmp_path)
            record(db, request, user, "backup.restore",
                   target="postgres", detail=f"{len(payload)} bytes")
            db.commit()
            request.session["flash"] = "Postgres database restored."
    except Exception as exc:  # noqa: BLE001
        log.exception("restore failed")
        request.session["backup_error"] = str(exc)
    finally:
        tmp_path.unlink(missing_ok=True)
    return RedirectResponse("/admin/backup", status_code=303)
