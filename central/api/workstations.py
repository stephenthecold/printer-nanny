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

    machine, api_key, created = result
    record(
        db,
        request,
        None,
        "machine.enroll" if created else "machine.reenroll",
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
            )
        )
        if is_default:
            default_id = printer.id

    return s.MachineAssignmentsOut(
        machine_id=machine.id,
        resolved_for=(
            (end_user.upn or end_user.email) if end_user is not None else None
        ),
        default_printer_id=default_id,
        printers=printers,
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
