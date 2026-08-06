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

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import services
from central.audit import record
from central.dashboard import _keystore
from central.dashboard.manage import (
    _deny,
    _flash,
    _manager,
    _pop_flash,
    _redirect,
    _tpl,
)
from central.db import get_db
from central.security import generate_enroll_key, hash_enroll_key

router = APIRouter(prefix="/manage", tags=["manage"])


def _pkg_cap():
    """Whether the macOS bundle can be assembled here. Cheap: it never shells out
    to a macOS tool, because it never runs one."""
    from central.pkg_builder import pkg_bundle_available

    return pkg_bundle_available()


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
        return _deny(request, db)

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

    drivers = []
    if client is not None:
        from central import driver_store

        for row in db.scalars(
            select(m.DriverPackage)
            .where(m.DriverPackage.client_id == client.id)
            .order_by(m.DriverPackage.name)
        ):
            # "missing" is the expected state after restoring a database onto a
            # fresh host, so it is shown rather than left to be discovered when
            # a workstation fails to provision.
            drivers.append((row, driver_store.missing(row.stored_at)))

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
        pkg_available=_pkg_cap().available,
        pkg_reason=_pkg_cap().reason,
        msi_reason=_msi_cap().reason,
        drivers=drivers,
        # Shown exactly once, straight after minting, then gone. Held in the
        # session rather than the database because storing it would defeat the
        # point of hashing it in the first place.
        new_key=_keystore.pop(request.session.pop("new_enroll_key_token", None)),
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
        return _deny(request, db)

    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "That client no longer exists.", level="error")
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

    # Server-side, with only a random token in the session. Starlette SIGNS the
    # session cookie but does not ENCRYPT it, so anything parked there is
    # base64-readable by the browser holding it -- and this is a live,
    # long-lived, multi-use credential that enrolls machines into this client.
    # _keystore exists for exactly this and says so in its docstring; every
    # other minted secret already uses it (agent keys, claim codes, event
    # signing secrets). This was the one call site that did not.
    request.session["new_enroll_key_token"] = _keystore.put({"key": key})
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
        return _deny(request, db)

    row = db.get(m.WorkstationEnrollKey, key_id)
    if row is None:
        _flash(request, "That key no longer exists.", level="error")
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
        return _deny(request, db)

    machine = db.get(m.Machine, machine_id)
    if machine is None:
        _flash(request, "That machine no longer exists.", level="error")
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
        return _deny(request, db)

    machine = db.get(m.Machine, machine_id)
    if machine is None:
        _flash(request, "That machine no longer exists.", level="error")
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
        return _deny(request, db)

    machine = db.get(m.Machine, machine_id)
    printer = db.get(m.Printer, printer_id)
    if machine is None or printer is None:
        _flash(request, "That machine or printer no longer exists.", level="error")
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
        _flash(request, "That printer belongs to a different client.", level="error")
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
        return _deny(request, db)

    machine = db.get(m.Machine, machine_id)
    if machine is None:
        _flash(request, "That machine no longer exists.", level="error")
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
        return _deny(request, db)

    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "That client no longer exists.", level="error")
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
        _flash(request, cap.reason, level="error")
        return _redirect(f"/manage/machines?client_id={client.id}")

    from central.runtime import load_settings
    rt = load_settings(db)
    central_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    embed_url = str(rt.get("agent.python_embed_url") or "").strip() or None
    embed_sha256 = str(rt.get("agent.python_embed_sha256") or "").strip() or None

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
            embed_sha256=embed_sha256,
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
        _flash(request, f"MSI build failed: {exc}", level="error")
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


@router.post("/machines/pkg")
def download_macos_pkg_bundle(
    request: Request,
    client_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Build this client's macOS installer bundle, minting the key it carries.

    A BUNDLE RATHER THAN A FINISHED .pkg, AND WHY THAT IS NOT A SHORTCUT
    -------------------------------------------------------------------
    ``pkgbuild``, ``productsign`` and ``xcrun notarytool`` are macOS-only, and
    notarytool is a closed binary talking to an Apple service. A Mac is therefore
    required to sign, whatever we do here -- so central assembles the payload and
    mints the key (the parts only central can do) and hands over a bundle carrying
    the build script. See ``central/pkg_builder`` for the alternative that was
    rejected and why.

    THE RESPONSE IS A CREDENTIAL
    ---------------------------
    The bundle contains this client's enrollment key, so it is served
    no-store: a proxy or browser cache holding it is a copy nobody knows about.
    Same reasoning as the MSI, sharper here because a .tar.gz is a thing people
    save and commit.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)

    from central.pkg_builder import build_workstation_pkg_bundle, pkg_bundle_available

    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "That client no longer exists.", level="error")
        return _redirect("/manage/machines")

    cap = pkg_bundle_available()
    if not cap.available:
        record(db, request, user, "workstation.pkg_bundle",
               target=f"client:{client.id}", detail=f"unavailable: {cap.reason}")
        db.commit()
        _flash(request, cap.reason, level="error")
        return _redirect(f"/manage/machines?client_id={client.id}")

    from central.runtime import load_settings

    rt = load_settings(db)
    central_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")

    key = generate_enroll_key()
    row = m.WorkstationEnrollKey(
        client_id=client.id,
        key_hash=hash_enroll_key(key),
        label=f"macOS pkg build for {client.name}"[:120],
        created_by_user_id=user.id,
    )
    db.add(row)
    db.flush()

    out_dir = Path(tempfile.mkdtemp(prefix="pn-ws-pkg-"))
    try:
        result = build_workstation_pkg_bundle(
            client_name=client.name,
            client_id=client.id,
            central_url=central_url,
            enroll_key=key,
            enroll_key_id=row.id,
            out_dir=out_dir,
        )
    except Exception as exc:  # noqa: BLE001 - a build failure is a flash, not a 500
        shutil.rmtree(out_dir, ignore_errors=True)
        # Roll the key back, exactly as the MSI path does: a key minted for an
        # installer that was never produced is a live credential nobody holds and
        # nobody will think to revoke, which is worse than no key because it looks
        # legitimate in the list.
        db.rollback()
        record(db, request, user, "workstation.pkg_bundle",
               target=f"client:{client.id}", detail=f"failed: {exc}")
        db.commit()
        _flash(request, f"macOS bundle build failed: {exc}", level="error")
        return _redirect(f"/manage/machines?client_id={client.id}")

    record(
        db, request, user, "workstation.pkg_bundle",
        target=f"client:{client.id}",
        # The key id, never the key. This is what ties a bundle in the wild back
        # to the row an operator revokes.
        detail=(f"ok enroll_key={row.id} size={result.size} "
                f"agent_version={result.agent_version} "
                f"wheels={result.wheel_count} identifier={result.identifier}"),
    )
    db.commit()

    safe = "".join(ch for ch in client.name if ch.isalnum() or ch in "-_")[:40] or "client"
    return FileResponse(
        path=str(result.path),
        media_type="application/gzip",
        filename=f"printer-nanny-workstation-{safe}-macos-bundle.tar.gz",
        # no-store, not merely no-cache: this body is a credential.
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
    )


@router.post("/machines/drivers/upload")
async def upload_driver_package(
    request: Request,
    client_id: int = Form(...),
    name: str = Form(""),
    driver_name: str = Form(...),
    inf_relpath: str = Form(""),
    model: str = Form(""),
    platform: str = Form("windows"),
    macos_kind: str = Form(""),
    macos_ref: str = Form(""),
    package: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """Accept a vendor driver package.

    WHAT AN ADMIN IS ACTUALLY DOING HERE
    ------------------------------------
    Uploading a binary that the workstation service will unpack and install with
    ``pnputil`` as LocalSystem on every machine in this client that needs it.
    That is code execution across a fleet -- it is what driver installation is,
    and it is why this route is manager-only and audited, why the bytes are
    checksummed on the way in and re-verified on the machine, and why the client
    refuses archive entries that escape their target directory.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)

    from central import driver_store

    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "That client no longer exists.", level="error")
        return _redirect("/manage/machines")

    plat = (platform or "windows").strip().lower()
    if plat not in ("windows", "macos"):
        _flash(request, "Unknown platform.", level="error")
        return _redirect(f"/manage/machines?client_id={client.id}")

    kind = (macos_kind or "").strip().lower() if plat == "macos" else ""
    ref = (macos_ref or "").strip()
    if plat == "macos":
        if kind not in ("ppd", "pkg", "system"):
            _flash(request, "Choose how the macOS driver is supplied.", level="error")
            return _redirect(f"/manage/machines?client_id={client.id}")
        if not ref:
            _flash(
                request,
                "A macOS package needs the PPD path: inside the archive for a "
                "PPD, or the absolute installed path for MDM/.pkg.",
                level="error",
            )
            return _redirect(f"/manage/machines?client_id={client.id}")
        # `system` records a path to a driver an MDM already installed, so there
        # is nothing to upload -- and requiring a file would make the operator
        # invent one.
        if kind != "system" and package is None:
            _flash(request, "That macOS driver kind needs a package file.", level="error")
            return _redirect(f"/manage/machines?client_id={client.id}")
        # `pkg` as well as `system`: for both, `ref` is the path of the PPD that
        # ends up INSTALLED on the Mac, and the client validates it the same way.
        # Only `system` was checked here, so a `pkg` row could be saved with a
        # relative ref -- which the client could never resolve, which read to it
        # as "not installed yet", which ran the vendor installer as root and then
        # failed. Every poll. The client refuses that shape now too; this stops
        # it being savable in the first place, where the operator can still see
        # what they typed.
        if kind in ("system", "pkg") and (
            ref.startswith("..") or not ref.startswith("/")
        ):
            _flash(request, "An installed PPD path must be absolute.", level="error")
            return _redirect(f"/manage/machines?client_id={client.id}")
    elif package is None or not inf_relpath.strip():
        _flash(request, "A Windows package needs a file and an INF path.", level="error")
        return _redirect(f"/manage/machines?client_id={client.id}")

    tag = (model or "").strip()
    if tag and len(tag) < services.MIN_DRIVER_MODEL_TAG:
        # A 1-2 character tag is a substring of nearly every model string, so it
        # would bind this driver to the client's whole fleet.
        _flash(
            request,
            "Model match needs at least 3 characters, or leave it blank.",
            level="error",
        )
        return _redirect(f"/manage/machines?client_id={client.id}")

    row = m.DriverPackage(
        client_id=client.id,
        name=(name or "").strip()[:200] or (driver_name or "").strip()[:200],
        driver_name=(driver_name or "").strip()[:255],
        inf_relpath=(inf_relpath or "").strip()[:500],
        model=tag[:200],
        platform=plat,
        macos_kind=kind or None,
        macos_ref=ref[:1000] or None,
        sha256="",
        created_by_user_id=user.id,
    )
    db.add(row)
    db.flush()  # need the id: the storage path is built from ids, never a filename

    if plat == "macos" and kind == "system":
        # No bytes at all. Recorded and audited exactly like an upload, because
        # it is the same decision -- which driver a fleet of Macs binds -- reached
        # without us carrying the payload.
        record(
            db, request, user, "driver_package.record",
            f"driver_package:{row.id}",
            f"client={client.id} platform=macos kind=system ppd={row.macos_ref!r} "
            f"model={row.model!r}",
        )
        db.commit()
        _flash(request, f"Recorded {row.name} (MDM-installed PPD, no upload).")
        return _redirect(f"/manage/machines?client_id={client.id}")

    try:
        sha, size = driver_store.save(client.id, row.id, package.file)
    except Exception as exc:  # noqa: BLE001 - an upload failure is a flash, not a 500
        db.rollback()
        record(db, request, user, "driver_package.upload",
               f"client:{client_id}", f"failed: {exc}")
        db.commit()
        _flash(request, f"Upload failed: {exc}", level="error")
        return _redirect(f"/manage/machines?client_id={client_id}")

    row.sha256, row.size = sha, size
    row.stored_at = str(driver_store.path_for(client.id, row.id))
    record(
        db, request, user, "driver_package.upload", f"driver_package:{row.id}",
        # The digest identifies exactly what will run on the fleet, which is the
        # thing worth being able to check later.
        f"client={client.id} platform={row.platform} "
        f"kind={row.macos_kind or 'inf'} driver={row.driver_name!r} "
        f"model={row.model!r} sha256={sha} size={size}",
    )
    db.commit()
    _flash(request, f"Uploaded {row.name} ({size // 1024} KB).")
    return _redirect(f"/manage/machines?client_id={client.id}")


@router.post("/machines/drivers/{package_id}/delete")
def delete_driver_package(
    package_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)

    from central import driver_store

    row = db.get(m.DriverPackage, package_id)
    if row is None:
        _flash(request, "That package no longer exists.", level="error")
        return _redirect("/manage/machines")

    client_id = row.client_id
    driver_store.delete(row.stored_at)
    record(db, request, user, "driver_package.delete", f"driver_package:{row.id}",
           f"client={client_id} driver={row.driver_name!r} sha256={row.sha256}")
    db.delete(row)
    db.commit()
    _flash(request, "Driver package removed. Printers using it are skipped again.")
    return _redirect(f"/manage/machines?client_id={client_id}")
