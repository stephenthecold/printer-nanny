"""Machines: the workstations running the print client, and their enroll keys.

Sits beside People rather than inside it. A machine is not a person -- it is
where a person stands -- and folding the two into one page would put a device
inventory into the screen operators use to manage staff. The assignment story is
the shared one: both pages assign printers, through the same service layer and
the same tenancy checks.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import services
from central.audit import record
from central.dashboard.manage import (
    _flash,
    _manager,
    _pop_flash,
    _redirect,
    _tpl,
)
from central.db import get_db
from central.security import generate_enroll_key, hash_enroll_key

router = APIRouter(prefix="/manage", tags=["manage"])


def _msi_cap():
    """Whether this central image can build an MSI at all.

    Probed rather than assumed: msitools is installed by deploy/Dockerfile, so a
    developer running central outside the container has no wixl, and a button
    that 500s is worse than one that explains itself.
    """
    from central.msi_builder import msi_build_available

    return msi_build_available()


def _resolve_client(db: Session, raw: Optional[str]) -> Optional[m.Client]:
    """Resolve the selected client, falling back to the first.

    Same shape as the People page: a stale bookmark carrying a deleted client id
    lands on a real page rather than a stack trace.
    """
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    if not clients:
        return None
    if raw:
        try:
            wanted = int(raw)
        except (TypeError, ValueError):
            return clients[0]
        for c in clients:
            if c.id == wanted:
                return c
    return clients[0]


@router.get("/machines", response_class=HTMLResponse)
def machines_page(
    request: Request,
    client_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    client = _resolve_client(db, client_id)
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))

    machines: list = []
    keys: list = []
    printers: list = []
    assignments: dict = {}
    if client is not None:
        machines = list(
            db.scalars(
                select(m.Machine)
                .where(m.Machine.client_id == client.id)
                .order_by(m.Machine.name, m.Machine.id)
            )
        )
        keys = list(
            db.scalars(
                select(m.WorkstationEnrollKey)
                .where(m.WorkstationEnrollKey.client_id == client.id)
                .order_by(
                    m.WorkstationEnrollKey.revoked_at.is_(None).desc(),
                    m.WorkstationEnrollKey.id.desc(),
                )
            )
        )
        printers = list(
            db.scalars(
                select(m.Printer)
                .where(m.Printer.client_id == client.id)
                .order_by(m.Printer.display_name, m.Printer.ip)
            )
        )
        if machines:
            rows = db.scalars(
                select(m.PrinterAssignment).where(
                    m.PrinterAssignment.machine_id.in_([x.id for x in machines])
                )
            ).all()
            by_printer = {p.id: p for p in printers}
            for a in rows:
                assignments.setdefault(a.machine_id, []).append(
                    (by_printer.get(a.printer_id), a.is_default)
                )

    return _tpl(
        request,
        "machines.html",
        db,
        user=user,
        client=client,
        clients=clients,
        machines=machines,
        keys=keys,
        printers=printers,
        assignments=assignments,
        flash=_pop_flash(request),
        msi_available=_msi_cap().available,
        msi_reason=_msi_cap().reason,
        # Shown exactly once, straight after minting, then gone. Held in the
        # session rather than the database because storing it would defeat the
        # point of hashing it in the first place.
        new_key=request.session.pop("new_enroll_key", None),
    )


@router.post("/machines/keys/create")
def create_enroll_key(
    request: Request,
    client_id: int = Form(...),
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "That client no longer exists.")
        return _redirect("/manage/machines")

    key = generate_enroll_key()
    row = m.WorkstationEnrollKey(
        client_id=client.id,
        key_hash=hash_enroll_key(key),
        label=(label or "").strip()[:120] or f"{client.name} workstations",
        created_by_user_id=user.id,
    )
    db.add(row)
    db.flush()
    record(
        db,
        request,
        user,
        "workstation_enroll_key.create",
        f"enroll_key:{row.id}",
        # The label and client, never the key -- audit detail is rendered in the
        # UI and dumped in diagnostics.
        f"client={client.id} label={row.label!r}",
    )
    db.commit()

    request.session["new_enroll_key"] = key
    _flash(request, "Enrollment key created. Copy it now — it is not shown again.")
    return _redirect(f"/manage/machines?client_id={client.id}")


@router.post("/machines/keys/{key_id}/revoke")
def revoke_enroll_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    row = db.get(m.WorkstationEnrollKey, key_id)
    if row is None:
        _flash(request, "That key no longer exists.")
        return _redirect("/manage/machines")

    if row.revoked_at is None:
        row.revoked_at = services._now()
        record(
            db,
            request,
            user,
            "workstation_enroll_key.revoke",
            f"enroll_key:{row.id}",
            f"client={row.client_id} label={row.label!r}",
        )
        db.commit()
        _flash(request, "Key revoked. Machines already enrolled keep working — they "
            "authenticate with their own keys.",
        )
    return _redirect(f"/manage/machines?client_id={row.client_id}")


@router.post("/machines/{machine_id}/active")
def set_machine_active(
    machine_id: int,
    request: Request,
    active: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    machine = db.get(m.Machine, machine_id)
    if machine is None:
        _flash(request, "That machine no longer exists.")
        return _redirect("/manage/machines")

    machine.active = active == "1"
    record(
        db,
        request,
        user,
        "machine.active" if machine.active else "machine.deactivate",
        f"machine:{machine.id}",
        f"client={machine.client_id} name={machine.name!r}",
    )
    db.commit()
    _flash(
        request,
        f"{machine.name or 'Machine'} "
        + ("reactivated." if machine.active else "retired — it stops provisioning now."),
    )
    return _redirect(f"/manage/machines?client_id={machine.client_id}")


@router.post("/machines/{machine_id}/default-wins")
def set_default_wins(
    machine_id: int,
    request: Request,
    default_wins: str = Form("0"),
    db: Session = Depends(get_db),
):
    """Toggle "this machine's default beats the person's own".

    Posted with an explicit value rather than read as a checkbox presence: an
    unchecked box posts nothing, so a handler that reads it directly cannot tell
    "unchecked" from "this form didn't carry the field" -- the same failure the
    subnet `trusted` flag hit.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    machine = db.get(m.Machine, machine_id)
    if machine is None:
        _flash(request, "That machine no longer exists.")
        return _redirect("/manage/machines")

    machine.default_wins = default_wins == "1"
    record(
        db,
        request,
        user,
        "machine.default_wins",
        f"machine:{machine.id}",
        f"client={machine.client_id} default_wins={machine.default_wins}",
    )
    db.commit()
    _flash(
        request,
        (
            f"{machine.name or 'Machine'} now overrides each person's own default."
            if machine.default_wins
            else f"{machine.name or 'Machine'} no longer overrides personal defaults."
        ),
    )
    return _redirect(f"/manage/machines?client_id={machine.client_id}")


@router.post("/machines/assign")
def assign_to_machine(
    request: Request,
    machine_id: int = Form(...),
    printer_id: int = Form(...),
    is_default: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    machine = db.get(m.Machine, machine_id)
    printer = db.get(m.Printer, printer_id)
    if machine is None or printer is None:
        _flash(request, "That machine or printer no longer exists.")
        return _redirect("/manage/machines")

    try:
        services.assign_printer(
            db,
            printer=printer,
            machine=machine,
            is_default=is_default == "1",
            operator_id=user.id,
        )
    except services.TenancyError as exc:
        # Audited as its own action: an operator's form typo and a reach into
        # another customer's fleet are different events and must not share a
        # line in the log. The operator is told it was refused, not whose fleet
        # it was -- that is another customer's information.
        record(
            db,
            request,
            user,
            "printer_assignment.refused",
            f"machine:{machine.id}",
            str(exc),
        )
        db.commit()
        _flash(request, "That printer belongs to a different client.")
        return _redirect(f"/manage/machines?client_id={machine.client_id}")

    record(
        db,
        request,
        user,
        "printer_assignment.create",
        f"machine:{machine.id}",
        f"printer={printer.id} default={is_default == '1'}",
    )
    db.commit()
    _flash(request, f"Assigned {printer.display_name or printer.ip} to {machine.name}.")
    return _redirect(f"/manage/machines?client_id={machine.client_id}")


@router.post("/machines/unassign")
def unassign_from_machine(
    request: Request,
    machine_id: int = Form(...),
    printer_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    machine = db.get(m.Machine, machine_id)
    if machine is None:
        _flash(request, "That machine no longer exists.")
        return _redirect("/manage/machines")

    row = db.scalar(
        select(m.PrinterAssignment).where(
            m.PrinterAssignment.machine_id == machine_id,
            m.PrinterAssignment.printer_id == printer_id,
        )
    )
    if row is not None:
        db.delete(row)
        record(
            db,
            request,
            user,
            "printer_assignment.remove",
            f"machine:{machine_id}",
            f"printer={printer_id}",
        )
        db.commit()
        _flash(request, "Assignment removed.")
    return _redirect(f"/manage/machines?client_id={machine.client_id}")


@router.post("/machines/msi")
def build_workstation_msi_route(
    request: Request,
    client_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Build this client's workstation installer, minting the key it carries.

    WHY THIS MINTS A KEY RATHER THAN REUSING ONE
    --------------------------------------------
    Enrollment keys are SHA-256 at rest, so there is no way to read an existing
    one back out to bake it in -- which is the property that makes a database
    dump useless, and it is not worth weakening to save a click.

    Minting per build turns out to be better than reuse anyway: each installer
    carries its own individually-revocable key, so an MSI that leaks (a file
    share, a ticket attachment, a laptop that left with someone) is revoked
    without disturbing any other installer or any machine already enrolled.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "That client no longer exists.")
        return _redirect("/manage/machines")

    from central.msi_builder import build_workstation_msi, msi_build_available

    # Capability first here, unlike the agent's claim-code build: there is no
    # caller-supplied credential to validate, so the only way to fail early is
    # the toolchain. Minting a key we then could not bake into anything would
    # leave a live credential in the database that nobody holds.
    cap = msi_build_available()
    if not cap.available:
        record(db, request, user, "workstation.msi_build",
               target=f"client:{client.id}", detail=f"unavailable: {cap.reason}")
        db.commit()
        _flash(request, cap.reason)
        return _redirect(f"/manage/machines?client_id={client.id}")

    from central.runtime import load_settings
    rt = load_settings(db)
    central_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    embed_url = str(rt.get("agent.python_embed_url") or "").strip() or None

    key = generate_enroll_key()
    row = m.WorkstationEnrollKey(
        client_id=client.id,
        key_hash=hash_enroll_key(key),
        label=f"MSI build for {client.name}"[:120],
        created_by_user_id=user.id,
    )
    db.add(row)
    db.flush()

    out_dir = Path(tempfile.mkdtemp(prefix="pn-ws-msi-"))
    try:
        result = build_workstation_msi(
            client_name=client.name,
            client_id=client.id,
            central_url=central_url,
            enroll_key=key,
            enroll_key_id=row.id,
            slug=f"client-{client.id}",
            out_dir=out_dir,
            embed_url=embed_url,
        )
    except Exception as exc:  # noqa: BLE001 - a build failure is a flash, not a 500
        shutil.rmtree(out_dir, ignore_errors=True)
        # Roll the key back. A key minted for an installer that was never
        # produced is a live credential nobody holds and nobody will think to
        # revoke -- worse than no key, because it looks legitimate in the list.
        db.rollback()
        record(db, request, user, "workstation.msi_build",
               target=f"client:{client.id}", detail=f"failed: {exc}")
        db.commit()
        _flash(request, f"MSI build failed: {exc}")
        return _redirect(f"/manage/machines?client_id={client.id}")

    record(
        db, request, user, "workstation.msi_build",
        target=f"client:{client.id}",
        # The key id, never the key. This is what ties an installer in the wild
        # back to the row an operator revokes.
        detail=(f"ok enroll_key={row.id} size={result.size} "
                f"agent_version={result.agent_version} "
                f"product={result.product_version}"),
    )
    db.commit()

    safe = "".join(ch for ch in client.name if ch.isalnum() or ch in "-_")[:40] or "client"
    return FileResponse(
        path=str(result.path),
        media_type="application/x-msi",
        filename=f"printer-nanny-workstation-{safe}.msi",
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
    )
