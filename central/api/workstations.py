"""Workstation client API: enroll, poll for assignments, check in.

Three endpoints, and the split between them is the security model.

``/enroll`` is the only one that accepts the client-scoped enroll key, and all
it can do is mint a machine plus that machine's own key. Everything afterwards
authenticates as the machine, so the long-lived credential in the installer is
never a read credential -- losing it does not disclose a fleet.

``/assignments`` is the one that returns real data, and it returns only what
this machine needs to provision right now: the printers for the signed-in
person, resolved by the same ``effective_printers_for`` the dashboard uses. It
deliberately does not accept a client or tenant argument -- the machine's row
supplies that, so a workstation cannot ask about a tenant it does not belong to.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import schemas as s
from central import services
from central.audit import record
from central.db import get_db
from central.deps import authenticated_machine

router = APIRouter(prefix="/api/v1/workstations", tags=["workstations"])


@router.post("/enroll", response_model=s.MachineEnrolled)
def enroll_workstation(
    payload: s.MachineEnrollIn,
    request: Request,
    db: Session = Depends(get_db),
) -> s.MachineEnrolled:
    """Redeem a client enroll key for a machine and its own API key.

    The API key is returned exactly once and stored only as a hash, so a client
    that discards it must re-enroll. That is survivable precisely because
    re-enrolling the same ``machine_uid`` rotates rather than duplicates.
    """
    result = services.redeem_enroll_key(
        db,
        payload.enroll_key,
        machine_uid=payload.machine_uid,
        name=payload.name,
    )
    if result is None:
        # Unknown and revoked are the same answer on purpose: distinguishing
        # them lets a holder of one key probe for the existence of others.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid enrollment key")

    machine, api_key, created, adopted = result
    # Adoption gets its own action rather than sharing "reenroll". One machine
    # taking over another record's printers by name is a different event from a
    # known machine refreshing its key, and an operator asking "why does this PC
    # have those queues?" needs to find it.
    if created:
        action = "machine.enroll"
    elif adopted:
        action = "machine.adopt"
    else:
        action = "machine.reenroll"
    record(
        db,
        request,
        None,
        action,
        f"machine:{machine.id}",
        # The uid identifies the PC and is safe to record; the key never is.
        f"client={machine.client_id} uid={machine.machine_uid} name={machine.name!r}",
    )
    db.commit()
    return s.MachineEnrolled(
        machine_id=machine.id,
        api_key=api_key,
        client_id=machine.client_id,
        created=created,
        adopted=adopted,
    )


@router.get("/{machine_id}/assignments", response_model=s.MachineAssignmentsOut)
def machine_assignments(
    machine: m.Machine = Depends(authenticated_machine),
    user: Optional[str] = None,
    db: Session = Depends(get_db),
) -> s.MachineAssignmentsOut:
    """What this machine should provision for the signed-in person.

    ``user`` is the Windows UPN of whoever is logged in, or absent at the
    sign-in screen. It is matched against ``upn`` first and ``email`` second,
    because those are different values that drift apart in real tenants -- a
    mailbox move changes the email while the UPN stays put, and matching only
    one strands the person on whichever changed.

    Matching is scoped to this machine's client, never global: two customers
    each having a "jsmith" is normal, and a global match would hand one
    customer's queue list to the other's PC.

    An unmatched or absent user is **not** an error. A workstation at the login
    screen, or one where a person has not been synced yet, still gets the
    machine's own printers -- that is the entire point of a machine assignment.
    """
    printers: list = []
    default_id: Optional[int] = None

    end_user = None
    wanted = (user or "").strip().lower()
    if wanted:
        # UPN first: it is what Windows hands us, and it is the identifier that
        # survives a mailbox move. Email is the fallback for tenants that never
        # populated a UPN. Both exact -- no fuzzy or local-part matching, which
        # across a tenant's staff is how one person gets another's printers.
        end_user = db.scalar(
            select(m.EndUser).where(
                m.EndUser.client_id == machine.client_id,
                m.EndUser.upn == wanted,
            )
        ) or db.scalar(
            select(m.EndUser).where(
                m.EndUser.client_id == machine.client_id,
                m.EndUser.email == wanted,
            )
        )

    if end_user is not None:
        resolved = services.effective_printers_for(db, end_user, machine)
    else:
        # No person to resolve for, so the machine's own assignments stand
        # alone. Built through the same resolver rather than a second code path:
        # a divergent "machine only" query is how the two disagree later.
        resolved = services.printers_for_machine(db, machine)

    for printer, is_default, _via in resolved:
        printers.append(
            s.MachinePrinterOut(
                printer_id=printer.id,
                name=printer.display_name or printer.ip,
                ip=printer.ip,
                is_default=is_default,
                # The override is the operator's decision and the plain column
                # is what was observed; the client acts on the decision when one
                # exists. Sent as a plain string so a tier added later does not
                # break an already-deployed client's deserialiser.
                driver_tier=(
                    (printer.driver_tier_override or printer.driver_tier).value
                    if (printer.driver_tier_override or printer.driver_tier)
                    else None
                ),
                ipp_endpoint=printer.ipp_endpoint,
                driver=_driver_for(db, printer),
            )
        )
        if is_default:
            default_id = printer.id

    from central.runtime import load_settings

    return s.MachineAssignmentsOut(
        machine_id=machine.id,
        manage_default_printer=bool(
            load_settings(db).get("workstation.manage_default_printer", True)
        ),
        resolved_for=(
            (end_user.upn or end_user.email) if end_user is not None else None
        ),
        default_printer_id=default_id,
        printers=printers,
    )


def _driver_for(db: Session, printer: m.Printer) -> "Optional[s.MachineDriverOut]":
    """Attach a vendor driver only where one is actually needed and usable.

    Deliberately not sent for driverless printers: Windows picks the inbox class
    driver for those, and handing the client a vendor package as well would give
    it a choice it has no business making.
    """
    tier = printer.driver_tier_override or printer.driver_tier
    if tier != m.DriverTier.driver_required:
        return None

    pkg = services.driver_package_for(db, printer)
    if pkg is None:
        return None
    return s.MachineDriverOut(
        package_id=pkg.id,
        driver_name=pkg.driver_name,
        inf_relpath=pkg.inf_relpath,
        sha256=pkg.sha256,
        size=pkg.size,
    )


@router.get("/{machine_id}/drivers/{package_id}")
def download_driver(
    package_id: int,
    machine: m.Machine = Depends(authenticated_machine),
    db: Session = Depends(get_db),
):
    """Serve a driver package to an enrolled machine.

    Tenant-scoped against the MACHINE's client, not a caller-supplied one -- a
    machine must never be able to pull another customer's driver package by
    guessing an id. A package belonging to another client is a plain 404, the
    same answer as one that does not exist, so this cannot be used to probe
    which ids are real.
    """
    from fastapi.responses import FileResponse

    from central import driver_store

    pkg = db.get(m.DriverPackage, package_id)
    if pkg is None or pkg.client_id != machine.client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such driver package")
    if driver_store.missing(pkg.stored_at):
        # The row survived a database restore and the bytes did not. A distinct
        # code because it is an operator action (re-upload), not a client bug.
        raise HTTPException(
            status.HTTP_410_GONE, "Driver package file is missing; re-upload it"
        )

    return FileResponse(
        path=pkg.stored_at,
        media_type="application/octet-stream",
        # Named from the id, never from anything an operator typed.
        filename=f"driver-{pkg.id}.pkg",
    )


@router.post("/{machine_id}/checkin", response_model=s.MachineCheckinOut)
def machine_checkin(
    payload: s.MachineCheckinIn,
    machine: m.Machine = Depends(authenticated_machine),
    db: Session = Depends(get_db),
) -> s.MachineCheckinOut:
    """Liveness plus a refreshed display name.

    The name is refreshed here because a PC gets renamed and the operator should
    see the current one -- the GUID is what identifies the machine, precisely so
    that the name is free to change without consequence.
    """
    machine.last_seen_at = services._now()
    clean = (payload.name or "").strip()[:255]
    if clean:
        machine.name = clean
    db.commit()
    return s.MachineCheckinOut(machine_id=machine.id, ok=True)
