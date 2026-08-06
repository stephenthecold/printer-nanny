"""Remote hands: the operator surface, and the rendering-isolation decision.

The policy lives in ``central/remote.py``; this file is the routes plus the one
thing that cannot live anywhere else -- how a page written by a printer on a
customer LAN is shown to a logged-in operator without becoming script execution
in that operator's session.

THE DECISION: SANDBOXED SAME-ORIGIN FRAME, NOT A SEPARATE ORIGIN, NOT A LINK-OUT
--------------------------------------------------------------------------------
Three options were on the table.

* **Link-out** ("here is the URL, open it yourself") needs no isolation at all,
  and is useless: the operator has no route to the device. That is the entire
  reason this feature exists.
* **A separate origin** is the textbook answer and is genuinely stronger --
  ``device-content.example.com`` cannot touch ``printers.example.com``'s cookies
  whatever the browser does. It is refused because it would make the feature
  depend on a second DNS name and a second certificate on installs that
  routinely have neither: this product ships onto segmented MSP management VLANs
  with no egress, frequently reached by IP or a single internal name. A control
  that only works where the operator can mint hostnames is a control most
  operators will not have.
* **Sandboxed same-origin frame** -- what is built. The captured body is served
  from one route that returns it under a CSP whose first directive is
  ``sandbox``, which drops the document into an **opaque origin**: it is not
  our origin any more, so same-origin access to the parent, to storage and to
  ``document.cookie`` is gone even though the URL is ours. On top of that,
  ``default-src 'none'`` means the document may not load or contact anything at
  all, and the ``<iframe sandbox="">`` attribute on the parent page applies the
  same restrictions a second time from the other side.

WHAT AN EVIL DEVICE CAN DO
--------------------------
* Render arbitrary *visual* content inside the frame -- a fake login form, a
  fake Printer Nanny dialog. It cannot submit that form anywhere
  (``form-action 'none'``, and sandbox without ``allow-forms`` disables
  submission outright), but it can try to convince a human to type something and
  read it aloud. Mitigated by the frame being visibly labelled as the device's
  own page, with the device's address, outside the frame where the device cannot
  paint over it.
* Return a large or slow response. Bounded: the agent caps the body at
  ``MAX_BODY_BYTES`` while reading and gives up on a timeout, and central
  re-checks the cap when the result is posted.
* Return a redirect to somewhere else. The agent does **not** follow it; the
  status and the ``Location`` value are recorded as text so an operator can see
  where the device tried to send us.

WHAT AN EVIL DEVICE CANNOT DO
-----------------------------
* Run script in our origin, or in any origin that can see ours. Scripts are off
  twice (CSP ``default-src 'none'`` and the sandbox with no ``allow-scripts``),
  and the document's origin is opaque regardless.
* Read or set our session cookie. No script; and an opaque origin has no access
  to the cookie jar of the origin the URL belongs to.
* Make any network request -- no image, stylesheet, font, ``fetch``, form post
  or beacon. ``default-src 'none'`` covers every fetch directive.
* Navigate the operator's tab or open a window (no ``allow-top-navigation``, no
  ``allow-popups``).
* Set a cookie on our domain: response headers from the device are dropped
  entirely rather than proxied, so a device ``Set-Cookie`` never reaches a
  browser. We also never echo the device's ``Content-Type`` -- the body is
  always served as ``text/html; charset=utf-8`` with ``nosniff``, and a body the
  device declared as something else is escaped into a ``<pre>`` first, so a
  ``text/plain`` page carrying markup renders as the text it claimed to be.

RESIDUAL RISK, STATED
---------------------
This rests on the browser enforcing CSP ``sandbox``. A browser bug there, or an
operator on something that ignores CSP entirely, loses the isolation -- which is
what the separate origin would have survived. That is the trade the second
bullet above records, made deliberately and written down rather than left to be
discovered. Nothing here is a defence against a *malicious operator*: reaching
any of these routes already requires an authenticated admin/tech session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import remote as policy
from central.audit import record
from central.dashboard.manage import _deny, _flash, _manager, _redirect
from central.db import get_db
from central.runtime import load_settings

router = APIRouter(prefix="/manage", tags=["manage"])

#: Serving the captured body. ``sandbox`` first because it is the directive
#: doing the heavy lifting -- it puts the document in an opaque origin with
#: scripts, forms, popups and top-level navigation all off. The rest is
#: belt-and-braces for a browser that honours some directives and not others.
#: ``style-src 'unsafe-inline'`` is the one concession: an EWS page is unreadable
#: without its inline CSS, and with ``default-src 'none'`` any ``url()`` inside
#: that CSS still resolves against a directive that forbids the fetch.
BODY_CSP = (
    "sandbox; "
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "form-action 'none'; "
    "frame-ancestors 'self'; "
    "base-uri 'none'"
)


def _printer(db: Session, printer_id: int) -> Optional[m.Printer]:
    return db.get(m.Printer, printer_id)


def _enabled(settings: dict) -> bool:
    return bool(settings.get("remote.enabled"))


def _queue(
    db: Session,
    request: Request,
    user: m.User,
    printer: m.Printer,
    kind: m.RemoteRequestKind,
    payload: dict,
    command_type: m.CommandType,
    **fields,
) -> m.RemoteRequest:
    """Create the request row, then the command that carries it.

    Order matters and is not cosmetic: the command payload has to name the
    request id, so the row is flushed first. Both live in one transaction, so an
    agent can never pull a command naming a request that does not exist.
    """
    agent_id = policy.agent_for_printer(db, printer)
    if agent_id is None:
        raise policy.RemoteError("no agent is assigned to this printer's site")

    req = m.RemoteRequest(
        printer_id=printer.id,
        agent_id=agent_id,
        kind=kind,
        requested_by_user_id=user.id,
        requested_by=user.username,
        **fields,
    )
    db.add(req)
    db.flush()

    cmd = m.Command(
        agent_id=agent_id,
        type=command_type,
        payload=dict(payload, request_id=req.id),
    )
    db.add(cmd)
    db.flush()
    req.command_id = cmd.id
    return req


def _refuse(db: Session, request: Request, user, printer, action: str, reason: str):
    """Audit a refusal and tell the operator why.

    Refusals are recorded, not merely rejected. A proxy is a good pivot for
    lateral movement, so "somebody repeatedly tried to reach a device and was
    stopped" has to be visible in the same place as the requests that succeeded
    -- otherwise the audit trail reads as if nobody ever tried.
    """
    record(
        db, request, user, "remote.refused",
        target=f"printer:{printer.id} {printer.ip}",
        detail=f"{action}: {reason}"[:4000],
    )
    db.commit()
    _flash(request, reason, level="error")
    return _redirect(f"/printers/{printer.id}#remote")


# --------------------------------------------------------------------------- #
# Read: fetch one page from the device's embedded web server
# --------------------------------------------------------------------------- #
@router.post("/printers/{printer_id}/remote/fetch")
def remote_fetch(
    printer_id: int,
    request: Request,
    path: str = Form(default="/"),
    scheme: str = Form(default="http"),
    port: str = Form(default="80"),
    db: Session = Depends(get_db),
):
    """Queue a read-only GET of one page on this printer's web server.

    Nothing an operator posts here reaches the network as a host. The address is
    read from ``printers.ip``; the form supplies only a path, a scheme and a
    port, each validated against a closed rule in ``central/remote.py``.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    printer = _printer(db, printer_id)
    if printer is None:
        return _redirect("/manage")

    settings = load_settings(db)
    if not _enabled(settings):
        return _refuse(db, request, user, printer, "fetch",
                       "Remote hands is switched off for this installation.")
    try:
        ip = policy.validate_device_address(printer.ip)
        clean_path = policy.validate_path(path)
        clean_scheme = policy.validate_scheme(scheme)
        clean_port = policy.validate_port(port)
        policy.check_rate_limit(db, printer, settings)
        req = _queue(
            db, request, user, printer,
            kind=m.RemoteRequestKind.fetch,
            command_type=m.CommandType.remote_fetch,
            payload={"ip": ip, "scheme": clean_scheme, "port": clean_port, "path": clean_path},
            scheme=clean_scheme, port=clean_port, path=clean_path,
        )
    except policy.RemoteError as exc:
        db.rollback()
        return _refuse(db, request, user, printer, "fetch", str(exc))

    record(
        db, request, user, "remote.fetch",
        target=f"printer:{printer.id} {printer.ip}",
        detail=f"request:{req.id} {clean_scheme}://{ip}:{clean_port}{clean_path}",
    )
    db.commit()
    _flash(request, "Queued. The agent will fetch this page on its next heartbeat.")
    return _redirect(f"/printers/{printer.id}#remote")


@router.get("/printers/{printer_id}/remote/{request_id}/body", response_class=HTMLResponse)
def remote_body(
    printer_id: int,
    request_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Serve one captured device page, isolated. See the module docstring.

    The response is authenticated (admin/tech) and scoped: the request must
    belong to the printer in the URL, so a captured body cannot be reached
    through some other printer's id.
    """
    user = _manager(request, db)
    if user is None:
        return Response("Login required", status_code=401, media_type="text/plain")
    req = db.get(m.RemoteRequest, request_id)
    if req is None or req.printer_id != printer_id or req.kind != m.RemoteRequestKind.fetch:
        return Response("Not found", status_code=404, media_type="text/plain")

    body = req.body or ""
    declared = (req.content_type or "").split(";")[0].strip().lower()
    if declared not in ("text/html", "application/xhtml+xml"):
        # Anything the device did not call HTML is shown AS TEXT: escaped, then
        # wrapped. A device that declares text/plain and sends markup therefore
        # renders as the text it claimed to be, and we never have to negotiate
        # a content type with a hostile party.
        body = (
            "<pre style=\"white-space:pre-wrap;word-break:break-word;"
            "font:12px ui-monospace,monospace\">%s</pre>" % escape(body)
        )

    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": BODY_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            # A captured device page is not something a shared cache should keep
            # a copy of, and it is not something a browser should re-serve after
            # the operator's session ends.
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Capability: the probe
# --------------------------------------------------------------------------- #
@router.post("/printers/{printer_id}/remote/probe")
def remote_probe(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Ask the agent whether this device accepts a write, by trying a no-op one."""
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    printer = _printer(db, printer_id)
    if printer is None:
        return _redirect("/manage")

    settings = load_settings(db)
    if not _enabled(settings):
        return _refuse(db, request, user, printer, "probe",
                       "Remote hands is switched off for this installation.")
    try:
        ip = policy.validate_device_address(printer.ip)
        policy.check_rate_limit(db, printer, settings)
        req = _queue(
            db, request, user, printer,
            kind=m.RemoteRequestKind.probe,
            command_type=m.CommandType.remote_probe,
            payload={"ip": ip},
        )
    except policy.RemoteError as exc:
        db.rollback()
        return _refuse(db, request, user, printer, "probe", str(exc))

    record(
        db, request, user, "remote.capability_probe",
        target=f"printer:{printer.id} {printer.ip}",
        detail=f"request:{req.id} no-op SNMP SET of sysLocation/sysContact",
    )
    db.commit()
    _flash(request, "Capability check queued. It writes the value the device already holds.")
    return _redirect(f"/printers/{printer.id}#remote")


@router.post("/printers/{printer_id}/remote/pin-read-only")
def remote_pin_read_only(
    printer_id: int,
    request: Request,
    disabled: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Operator's own decision to keep a device read-only, regardless of probe.

    There is deliberately no opposite of this control. Pinning read-only can
    only narrow what we do; a "treat this as writable" pin would be an assertion
    standing in for the evidence the probe exists to gather, and if the device
    really is writable the probe will say so.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    printer = _printer(db, printer_id)
    if printer is None:
        return _redirect("/manage")
    printer.remote_write_disabled = str(disabled).lower() in ("1", "true", "on", "yes")
    record(
        db, request, user, "remote.pin_read_only",
        target=f"printer:{printer.id} {printer.ip}",
        detail="writes pinned off" if printer.remote_write_disabled else "operator pin cleared",
    )
    db.commit()
    _flash(
        request,
        "Writes pinned off for this printer."
        if printer.remote_write_disabled
        else "Operator pin cleared; the capability check decides.",
    )
    return _redirect(f"/printers/{printer.id}#remote")


# --------------------------------------------------------------------------- #
# Write: a named operation, on a device proven to accept one
# --------------------------------------------------------------------------- #
@router.post("/printers/{printer_id}/remote/write")
def remote_write(
    printer_id: int,
    request: Request,
    op: str = Form(...),
    value: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Perform one named write. Every gate is checked here, in order."""
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    printer = _printer(db, printer_id)
    if printer is None:
        return _redirect("/manage")

    settings = load_settings(db)
    if not _enabled(settings):
        return _refuse(db, request, user, printer, "write",
                       "Remote hands is switched off for this installation.")
    blocked = policy.writes_allowed(printer, settings)
    if blocked:
        return _refuse(db, request, user, printer, f"write op={op}", blocked)
    try:
        ip = policy.validate_device_address(printer.ip)
        write_op, wire_value = policy.validate_write(op, value)
        policy.check_rate_limit(db, printer, settings)
        req = _queue(
            db, request, user, printer,
            kind=m.RemoteRequestKind.write,
            command_type=m.CommandType.remote_write,
            payload={
                "ip": ip,
                "op": write_op.key,
                "oid": write_op.oid,
                "snmp_type": write_op.snmp_type,
                "value": wire_value,
                "verify": write_op.verifiable,
            },
            op=write_op.key,
            op_value=wire_value[:400],
        )
    except policy.RemoteError as exc:
        db.rollback()
        return _refuse(db, request, user, printer, f"write op={op}", str(exc))

    # The audit row names the actual operation and the actual value. No write in
    # the vocabulary takes a credential, which is what makes recording the value
    # safe as well as useful -- "somebody restarted this printer" is exactly the
    # thing an audit trail is for.
    record(
        db, request, user, "remote.write",
        target=f"printer:{printer.id} {printer.ip}",
        detail=f"request:{req.id} op={write_op.key} oid={write_op.oid} value={wire_value}",
    )
    db.commit()
    _flash(request, f"{write_op.label} queued for the agent's next heartbeat.")
    return _redirect(f"/printers/{printer.id}#remote")


# --------------------------------------------------------------------------- #
# Panel data (used by the printer detail page)
# --------------------------------------------------------------------------- #
def panel_context(db: Session, printer: m.Printer, user: Optional[m.User]) -> dict:
    """Everything the printer page's remote-hands card needs.

    Called from the detail route rather than being its own page so the operator
    sees capability, history and controls in the place they already look at a
    printer.
    """
    settings = load_settings(db)
    enabled = _enabled(settings)
    if not enabled or user is None or user.role not in (m.UserRole.admin, m.UserRole.tech):
        return {"remote_enabled": False}
    now = datetime.now(timezone.utc)

    # Deliberately does NOT expire anything: this is a GET. Staleness is derived
    # for display (``policy.display_status``) and only written on a POST, which
    # was going to commit anyway.
    requests = list(db.scalars(
        select(m.RemoteRequest)
        .where(m.RemoteRequest.printer_id == printer.id)
        .order_by(m.RemoteRequest.created_at.desc())
        .limit(10)
    ))
    address_error = None
    try:
        policy.validate_device_address(printer.ip)
    except policy.RemoteError as exc:
        address_error = str(exc)
    return {
        "remote_enabled": True,
        "remote_requests": requests,
        "remote_write_ops": list(policy.WRITE_OPS.values()),
        "remote_writes_blocked": policy.writes_allowed(printer, settings),
        "remote_ports": policy.ALLOWED_PORTS,
        "remote_address_error": address_error,
        "remote_agent_id": policy.agent_for_printer(db, printer),
        "remote_status_of": lambda req: policy.display_status(req, now=now),
    }
