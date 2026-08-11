"""HTMX/Jinja dashboard. Server-rendered, session-authenticated."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central import supplies as supplies_lib
from central.branding import branding_for
from central.csrf import rotate_token
from central.dashboard.templating import templates
from central.db import get_db
from central.deps import SESSION_EPOCH_KEY, session_user
from central.health import worker_banner
from central.security import MIN_PASSWORD_LENGTH, hash_password, verify_password

router = APIRouter(tags=["dashboard"])
# One shared Jinja environment (central/dashboard/templating.py): it carries
# the csrf_field()/csrf_token() globals every form depends on.
_templates = templates


def _user(request: Request, db: Session):
    return session_user(request, db)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _forbidden_client(user, client_id) -> bool:
    """A client_readonly user may only see their own client's data."""
    return user.role == m.UserRole.client_readonly and user.client_id != client_id


# --- "Data as of ..." freshness strip ---------------------------------------- #
#: Path the strip polls. One constant so the route, the middleware that keeps it
#: from rolling the session, and every page's hx-get can never disagree.
FRESHNESS_PATH = "/fragments/freshness"


def _scope_label(db: Session, client_id) -> str:
    """"Fleet", or the client's own name when the view is scoped to one."""
    if client_id is None:
        return "Fleet"
    client = db.get(m.Client, client_id)
    return client.name if client is not None else "Fleet"


def _freshness_ctx(request: Request, db: Session, user, client_id=None, scope_label="Fleet"):
    """Context for base.html's freshness strip, or ``{}`` if it can't be measured.

    Added to every page that renders fleet state. Pages that show no device
    state (settings, users, the audit log) deliberately don't get it: a "data as
    of" stamp on a page whose data is not time-sensitive is noise, and it would
    cost those pages three aggregates apiece for nothing.

    Returning ``{}`` on failure is the safe direction -- ``fleet_freshness``
    already swallows its own errors, and a freshness indicator that 500s the
    page it exists to qualify would be a remarkable own goal.
    """
    from central.freshness import fleet_freshness, refresh_seconds

    fresh = fleet_freshness(db, client_id=client_id, scope_label=scope_label, user=user)
    if fresh is None:
        return {}
    endpoint = FRESHNESS_PATH
    if client_id is not None:
        endpoint = f"{FRESHNESS_PATH}?client_id={int(client_id)}"
    return {
        "freshness": fresh,
        "poll_seconds": refresh_seconds(db),
        "fresh_endpoint": endpoint,
    }


#: htmx cancels an element's polling when a response carries this status. Used
#: for every terminal condition -- signed out, or a scope that no longer
#: resolves -- so a dead poll stops asking instead of hammering on, and so a
#: session that expired never gets the whole login page swapped into a 2cm strip.
HTMX_STOP_POLLING = 286


@router.get(FRESHNESS_PATH, response_class=HTMLResponse)
def freshness_fragment(request: Request, db: Session = Depends(get_db)):
    """Re-render just the freshness strip.

    Cheap by construction: three one-row aggregates, no nav count, no page
    queries, no template inheritance. This is what an open NOC tab re-asks on a
    timer, so it must stay that way -- anything added here is multiplied by
    every open tab of every operator.

    It writes nothing. No audit row (a passive read on a timer would bury the
    audit log in noise and drown the events that matter), no session mutation,
    and -- see ``central.main`` -- no session cookie refresh either, so an
    unattended screen still times out on schedule rather than being kept alive
    forever by its own auto-refresh.

    Tenancy: a client_readonly user's scope comes from their own row and the
    ``client_id`` query parameter is ignored outright, so the poll cannot be
    pointed at another tenant by editing a URL.
    """
    from central.freshness import fleet_freshness, refresh_seconds

    user = _user(request, db)
    if user is None:
        return _stop_polling(request, "Signed out — reload the page to sign in again.")

    readonly = user.role == m.UserRole.client_readonly
    if readonly:
        client_id = user.client_id
        if client_id is None:
            return _stop_polling(request, "Reload the page.")
    else:
        raw = (request.query_params.get("client_id") or "").strip()
        try:
            client_id = int(raw) if raw else None
        except ValueError:
            return _stop_polling(request, "Reload the page.")

    scope_label = "Fleet"
    if client_id is not None:
        client = db.get(m.Client, client_id)
        if client is None:
            # The page above this strip would have redirected; stop polling and
            # let a reload take the operator wherever they now belong.
            return _stop_polling(request, "Reload the page.")
        scope_label = client.name

    fresh = fleet_freshness(db, client_id=client_id, scope_label=scope_label, user=user)
    if fresh is None:
        return _stop_polling(request, "Freshness unavailable — reload the page.")
    endpoint = FRESHNESS_PATH
    if client_id is not None:
        endpoint = f"{FRESHNESS_PATH}?client_id={int(client_id)}"
    return _templates.TemplateResponse(
        request,
        "_freshness.html",
        {"freshness": fresh, "poll_seconds": refresh_seconds(db),
         "fresh_endpoint": endpoint},
    )


def _stop_polling(request: Request, message: str) -> HTMLResponse:
    """A terminal strip that replaces itself and cancels its own timer."""
    return _templates.TemplateResponse(
        request, "_freshness_stopped.html", {"message": message},
        status_code=HTMX_STOP_POLLING,
    )


def _render(
    request: Request, template: str, db: Optional[Session] = None, **ctx
) -> HTMLResponse:
    from central import __version__ as _central_version

    ctx.setdefault("user", ctx.get("user"))
    # White-label branding injected into every render. Templates read ``app.name``,
    # ``app.logo_url``, ``app.primary_color``, ``app.support_email``, ``app.footer_text``.
    #
    # ``brand_client`` selects PER-CLIENT branding for the customer-facing
    # surface: the portal passes the client it is rendering (which covers an
    # admin previewing one), and any page a client_readonly user reaches picks
    # up their own client automatically. Staff pages resolve to no client and
    # render the global values exactly as before. See central/branding.py.
    brand_client = ctx.pop("brand_client", None)
    if db is not None:
        ctx.setdefault("app", branding_for(db, ctx.get("user"), brand_client))
    else:
        ctx.setdefault("app", {})
    # Central server version surfaced in the footer of every page so operators
    # can verify a rollout landed without dropping to the shell.
    ctx.setdefault("central_version", _central_version)
    # Pending-discovery count drives the conditional Approvals nav entry. Only
    # staff can reach that route, so never compute (or accidentally expose) a
    # fleet-wide count for a client-scoped or signed-out render.
    nav_user = ctx.get("user")
    if (
        db is not None
        and "nav_pending" not in ctx
        and nav_user is not None
        and nav_user.role != m.UserRole.client_readonly
    ):
        ctx["nav_pending"] = db.scalar(
            select(func.count())
            .select_from(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
        ) or 0
    else:
        ctx.setdefault("nav_pending", 0)
    # Stalled-worker banner (base.html). A dead worker stops marking agents and
    # printers offline, so without this every page renders a green fleet over
    # frozen data. Returns None when healthy; never raises.
    if db is not None and "worker_banner" not in ctx:
        ctx["worker_banner"] = worker_banner(db, ctx.get("user"))
    return _templates.TemplateResponse(request, template, ctx)


# --- Auth ------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, sso_error: str = "", db: Session = Depends(get_db)):
    from central.auth_oidc import oidc_config, oidc_enabled

    cfg = oidc_config(db)
    return _render(
        request, "login.html", db=db, error=None,
        sso_enabled=oidc_enabled(db),
        sso_label=cfg.get("button_label") or "Sign in with SSO",
        sso_error=sso_error,
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from central import ratelimit
    from central.audit import record, record_anonymous
    from central.middleware import FORCE_PASSWORD_CHANGE_KEY
    from central.net import throttle_ip
    from central.runtime import load_settings

    values = load_settings(db)
    policy = ratelimit.Policy.from_settings(values)
    # NOT request.client.host: that is rewritten from X-Forwarded-For by a
    # middleware that trusts any sender, so keying a throttle on it would hand an
    # attacker a fresh bucket per request. See central/net.py.
    source = throttle_ip(request, str(values.get("auth.trusted_proxies", "")))

    throttled = ratelimit.check(db, username=username, ip=source, policy=policy)
    if throttled is not None:
        # Refused BEFORE the password is checked -- a throttle that verifies the
        # password in order to decide whether to apply has already granted the
        # guess it was supposed to ration. Deliberately not audited per attempt:
        # the transition into this state was audited once (below), and an
        # attacker who can write one audit row per request can push everything
        # else out of the operator's fixed-size audit view.
        response = _render(
            request, "login.html", db=db, error=throttled.message()
        )
        # 429 so a reverse proxy / fail2ban can see it, and so an automated
        # client is told to back off rather than reading a 200 as "keep going".
        response.status_code = 429
        return response

    user = db.scalar(select(m.User).where(m.User.username == username))
    # ``password_hash`` is None for SSO-only accounts; passlib will not verify
    # against None, so the guard is what keeps their username from 500ing the
    # login form rather than being rejected like any other bad credential.
    password_ok = (
        user is not None
        and bool(user.password_hash)
        and verify_password(password, user.password_hash)
    )
    if not password_ok or not user.active:
        # Deactivated (deprovisioned) accounts cannot log in even with a valid
        # password -- the same generic message so we don't leak that the account
        # exists-but-is-disabled. This is the SCIM off-boarding gate at the login
        # boundary (current_user enforces it for already-live sessions).
        record_anonymous(
            db, request, username,
            "login.failed" if not password_ok else "login.deactivated",
        )
        filled = ratelimit.record_failure(
            db, username=username, ip=source, policy=policy
        )
        if filled is not None:
            record_anonymous(
                db, request, username, "login.throttled",
                target=f"{filled}:{username if filled == 'user' else source}",
                detail=(f"{policy.user_threshold if filled == 'user' else policy.ip_threshold}"
                        f" failed sign-ins within {policy.window_minutes}m"),
            )
        db.commit()
        return _render(request, "login.html", db=db, error="Invalid credentials")
    # A proven password empties both buckets: the username's, and the source's
    # (see ratelimit.clear for why the second one is safe).
    ratelimit.clear(db, username=username, ip=source)
    # Rotate the CSRF token on the privilege transition, exactly as the OIDC
    # callback does (auth_oidc.py). Without it, a token an attacker observed or
    # fixed while the visitor was anonymous stays valid once they authenticate,
    # which is the session-fixation half of CSRF -- and it was present on the
    # SSO path and missing here, which is the worst arrangement of the two.
    rotate_token(request.session)
    request.session["user_id"] = user.id
    request.session[SESSION_EPOCH_KEY] = user.session_epoch or 0
    if user.must_change_password:
        request.session[FORCE_PASSWORD_CHANGE_KEY] = True
    record(db, request, user, "login")
    db.commit()
    if user.must_change_password:
        return RedirectResponse("/account", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    from central.audit import record

    user = _user(request, db)
    if user is not None:
        # session.clear() only asks the BROWSER to drop the cookie; the signed
        # value it already holds keeps verifying for the full max_age. Bumping
        # the epoch is what makes "log out" mean the session is spent -- which
        # matters most on the shared or borrowed machine where someone clicks it
        # precisely because they do not trust what happens next.
        user.session_epoch = (user.session_epoch or 0) + 1
        record(db, request, user, "logout")
        db.commit()
    request.session.clear()
    return _login_redirect()


# --- Overview --------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    # client_readonly users get the trimmed portal -- one fleet, one view, a
    # "Report a problem" button. The dense admin/tech overview would just be
    # noise for them.
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/portal", status_code=303)
    # The staff dashboard is an operational queue, in the order established by
    # the product interview: end-to-end subnet health, printer-stopping issues,
    # then supplies by runway. The old recent-activity/errors/alerts panels all
    # repeated pieces of those same facts and made the first decision harder.
    from central import reorder as reorder_mod
    from central import setup_checklist

    health = queries.subnet_health(db)
    issues = queries.printer_issue_queue(db, limit=12)
    supply_recs = reorder_mod.recommendations(db)
    return _render(
        request,
        "overview.html",
        db=db,
        user=user,
        summary=queries.fleet_summary(db),
        subnet_health=health,
        issue_queue=issues,
        supply_queue=supply_recs[:12],
        supply_total=len(supply_recs),
        setup=setup_checklist.build_setup_status(db),
        urgency_now=reorder_mod.URGENCY_NOW,
        rollup=queries.per_client_rollup(db),
        printer_label=_printer_label,
        # The overview is the screen most likely to be left open on a wall, and
        # the one whose green tiles are most readily believed.
        **_freshness_ctx(request, db, user),
    )


# --- Customer portal (client_readonly only) --------------------------------- #
@router.get("/portal", response_class=HTMLResponse)
def customer_portal(request: Request, db: Session = Depends(get_db)):
    """Trimmed view for client_readonly users: their fleet status, low
    supplies with "your supplies last ~Nd" forecasts, open alerts, and a
    'Report a problem' form that opens a FreeScout ticket via the existing
    channel (no extra credential plumbing)."""
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly and user.client_id is None:
        # Misconfigured account -- send them to their account screen.
        return RedirectResponse("/account", status_code=303)
    client_id = user.client_id if user.role == m.UserRole.client_readonly \
        else int(request.query_params.get("client_id") or 0) or None
    if client_id is None and user.role != m.UserRole.client_readonly:
        # Admins/techs land on /portal for previewing; pick a client.
        first = db.scalar(select(m.Client).order_by(m.Client.name))
        if first is None:
            return RedirectResponse("/", status_code=303)
        client_id = first.id
    client = db.get(m.Client, client_id)
    if client is None:
        return RedirectResponse("/", status_code=303)
    printers = list(db.scalars(
        select(m.Printer)
        .where(m.Printer.client_id == client.id,
               m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.site_id, m.Printer.ip)
    ))
    runway = queries.supply_runway(db, [p.id for p in printers])
    # Both of these are scoped and capped IN SQL. They used to fetch the fleet
    # and narrow it in Python, which for the alerts meant taking the newest 30
    # alerts across every tenant and only THEN keeping this client's: a customer
    # with an open critical fault saw "No open issues right now" as soon as
    # thirty newer alerts belonged to other customers. Filtering after a LIMIT
    # is not a slow correct answer, it is a wrong one.
    low = queries.low_supplies(db, client_id=client.id, limit=10)
    alerts = queries.open_alerts(db, 10, client_id=client.id)
    from central.runtime import load_settings as _ls
    rt = _ls(db)
    freescout_on = bool(rt.get("freescout.enabled"))
    # ESG / sustainability panel — estimated print footprint for THIS client's
    # fleet, scoped by client_id so one tenant never sees another's totals.
    esg = queries.sustainability_rollup(db, client_id=client.id)
    # Supplies to order, scoped to THIS client in SQL for the same reason the
    # two above are. Recommendations only -- the portal shows a customer what is
    # about to run out; it does not let them (or us) order anything.
    from central import reorder as reorder_mod
    reorder_recs = reorder_mod.recommendations(db, client_id=client.id)
    return _render(
        request, "portal.html", db=db, user=user,
        # The portal is THE white-label surface: it renders in this client's
        # branding, falling back per-field to the global settings. Passed
        # explicitly so an admin/tech previewing ?client_id=N sees what that
        # customer sees rather than the MSP's own chrome.
        brand_client=client,
        client=client, printers=printers, runway=runway,
        low_supplies=low, open_alerts=alerts,
        freescout_enabled=freescout_on,
        esg=esg,
        reorder_recs=reorder_recs,
        reorder_summary=reorder_mod.summarize(reorder_recs),
        urgency_now=reorder_mod.URGENCY_NOW,
        printer_label=_printer_label,
        portal_flash=request.session.pop("portal_flash", None),
        # Scoped to this client, and labelled with their own name rather than
        # "Fleet" -- the portal is the one surface where "the fleet" means
        # theirs. This is also the only view whose reader is never shown the
        # stalled-worker banner, which is why freshness.warn_worker exists.
        **_freshness_ctx(request, db, user, client_id=client.id, scope_label=client.name),
    )


@router.post("/portal/report")
def portal_report(
    request: Request,
    printer_id: str = Form(""),
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    """Open a FreeScout ticket from the portal. Falls back to the alert
    email channel if FreeScout isn't configured -- whichever the operator
    wired up, the report lands somewhere actionable."""
    user = _user(request, db)
    if user is None or user.role != m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    if not subject.strip() or not body.strip():
        request.session["portal_flash"] = "Subject and description are required."
        return RedirectResponse("/portal", status_code=303)
    client = db.get(m.Client, user.client_id) if user.client_id else None
    printer = None
    if printer_id.strip():
        try:
            p = db.get(m.Printer, int(printer_id))
            if p and p.client_id == (user.client_id or -1):
                printer = p
        except ValueError:
            printer = None
    from central.channels import Notification
    from central.channels.email import EmailChannel
    from central.channels.freescout import FreeScoutChannel
    from central.runtime import load_settings as _ls

    rt = _ls(db)
    note = Notification(
        title=f"[Portal report] {subject.strip()}",
        body=f"From: {user.username}\n\n{body.strip()}",
        severity="info",
        client_name=client.name if client else None,
        printer_label=f"{printer.display_name or printer.model or 'printer'} @ {printer.ip}"
            if printer else None,
    )
    sent_via = None
    if rt.get("freescout.enabled"):
        result = FreeScoutChannel("FreeScout", {}, rt).send(note)
        if result.ok:
            sent_via = "ticket"
    if sent_via is None and rt.get("email.default_recipients"):
        from central.db import SessionLocal
        channel = EmailChannel(
            "Reports", {"to": rt["email.default_recipients"]},
            runtime=rt, db_factory=SessionLocal,
        )
        result = channel.send(note)
        if result.ok:
            sent_via = "email"
    from central.audit import record
    record(db, request, user, "portal.report",
           target=f"client:{client.id} {client.name}" if client else "no-client",
           detail=f"via:{sent_via or 'none'}; subject={subject[:80]}")
    db.commit()
    request.session["portal_flash"] = (
        "Thanks -- your request was sent to support." if sent_via
        else "Could not deliver the report. Your operator has not configured a ticket channel yet."
    )
    return RedirectResponse("/portal", status_code=303)


# --- Client drill-down ------------------------------------------------------ #
@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    client = db.get(m.Client, client_id)
    if client is None or _forbidden_client(user, client_id):
        return RedirectResponse("/", status_code=303)
    printers = list(
        db.scalars(
            select(m.Printer)
            .where(
                m.Printer.client_id == client_id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
            .order_by(m.Printer.site_id, m.Printer.ip)
        )
    )
    # Days-until-order forecast per printer (min days-to-empty across its
    # supplies) -- replaces the raw page count in the listing, which told the
    # operator nothing actionable.
    runway = queries.supply_runway(db, [p.id for p in printers])
    # Lowest supply per printer so the listing shows actual levels, not just
    # the forecast: (label, pct) of the most-depleted supply.
    lowest_supply: dict[int, tuple] = {}
    # The operator's configured threshold, not a literal. This route hardcoded
    # 20.0 while ``/`` and the weekly report both resolved
    # ``queries.low_supply_threshold``, so moving the slider in Settings made
    # the overview and this page disagree about the same fleet -- on a page a
    # client_readonly user can reach, which is where an unexplained discrepancy
    # costs the most.
    low_threshold = queries.low_supply_threshold(db)
    # Per-site rollup chips: printers / down / warnings / low supplies /
    # soonest order. Plus a client-level total across all sites.
    site_stats: dict[int, dict] = {}
    client_stats = {
        "printers": 0, "down": 0, "warnings": 0, "low_supplies": 0,
        "soonest_days": None,
    }
    for p in printers:
        stats = site_stats.setdefault(p.site_id, {
            "printers": 0, "down": 0, "warnings": 0, "low_supplies": 0,
            "soonest_days": None,
        })
        stats["printers"] += 1
        client_stats["printers"] += 1
        if p.status in (m.PrinterStatus.error, m.PrinterStatus.offline):
            stats["down"] += 1
            client_stats["down"] += 1
        elif p.status == m.PrinterStatus.warning:
            stats["warnings"] += 1
            client_stats["warnings"] += 1
        # Ranked by HEADROOM, not by the raw level: a waste box reports how full
        # it is, so an emptied one reading 5 was "the most depleted supply" on
        # every row it appeared in and pushed the actually-dying cartridge out of
        # the column. ``remaining_pct`` puts both on one comparable scale; the
        # cell still renders the device's own number, with the flag that says
        # which way to read it. Receptacles are excluded from the low COUNT for
        # the same reason ``queries.low_supplies`` excludes them.
        leveled = [s for s in p.supplies if s.level_pct is not None]
        if leveled:
            worst = min(leveled, key=lambda s: supplies_lib.remaining_pct(s))
            lowest_supply[p.id] = (
                worst.description or worst.color or worst.type.value,
                worst.level_pct,
                worst.is_receptacle,
            )
        low_count = sum(
            1 for s in leveled
            if not s.is_receptacle and s.level_pct <= low_threshold
        )
        stats["low_supplies"] += low_count
        client_stats["low_supplies"] += low_count
        days = (runway.get(p.id) or {}).get("days")
        if days is not None:
            if stats["soonest_days"] is None or days < stats["soonest_days"]:
                stats["soonest_days"] = days
            if client_stats["soonest_days"] is None or days < client_stats["soonest_days"]:
                client_stats["soonest_days"] = days
    client_stats["open_alerts"] = db.scalar(
        select(func.count())
        .select_from(m.Alert)
        .join(m.Printer, m.Printer.id == m.Alert.printer_id)
        .where(m.Printer.client_id == client_id, m.Alert.state == m.AlertState.open)
    ) or 0
    return _render(
        request,
        "client.html",
        db=db,
        user=user,
        client=client,
        sites=client.sites,
        printers=printers,
        runway=runway,
        lowest_supply=lowest_supply,
        site_stats=site_stats,
        client_stats=client_stats,
        printer_label=_printer_label,
        **_freshness_ctx(request, db, user, client_id=client.id, scope_label=client.name),
    )


# --- Fleet-wide printer list + search --------------------------------------- #
@router.get("/printers", response_class=HTMLResponse)
def printer_search(request: Request, db: Session = Depends(get_db)):
    """Every printer in one paginated list, searchable by the identifiers a
    caller actually gives you: IP, hostname, serial, asset tag, model, brand and
    the operator's friendly name.

    WHO CAN SEE IT, AND WHY IT IS NOT STAFF-ONLY. Staff (admin/tech) search the
    whole fleet; a client_readonly user reaches the same page scoped to their own
    client. Making it staff-only was the safer-looking option and is the wrong
    one: a customer with sixty devices across four sites has exactly the same
    'which one is 10.4.7.23?' problem, their portal lists their fleet flat with
    no way to filter it, and refusing them a search over data they are already
    shown buys no security at all. What buys security is where the scope is
    applied -- ``client_id`` goes into the SQL, so there is no code path on which
    a term matches another tenant's printer and is then filtered out afterwards.

    Two further narrowings for that role, both matching what /portal and
    /clients/{id} already do: a client_readonly user sees **approved** devices
    only (a pending device is not yet part of their fleet and is not theirs to
    see), and an account with no ``client_id`` -- a misconfiguration, not a
    tenant -- gets nothing and is sent to /account. Staff see every discovery
    state, badged, because "I cannot find 10.4.7.23" is at its most likely
    precisely when the device is still sitting in the approvals queue.
    """
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    readonly = user.role == m.UserRole.client_readonly
    if readonly and user.client_id is None:
        return RedirectResponse("/account", status_code=303)
    # Parsed defensively rather than declared as `page: int`: a truncated or
    # hand-edited URL should land on page 1, not on a 422 validation page.
    try:
        page = int(request.query_params.get("page") or 1)
    except ValueError:
        page = 1
    result = queries.search_printers(
        db,
        q=request.query_params.get("q") or "",
        client_id=user.client_id if readonly else None,
        states=[m.DiscoveryState.approved] if readonly else None,
        page=page,
    )
    return _render(
        request,
        "printer_search.html",
        db=db,
        user=user,
        result=result,
        scoped_to_client=readonly,
        printer_label=_printer_label,
        # Same scoping the search itself uses: a client_readonly user's strip
        # counts their own fleet, never everyone's.
        **_freshness_ctx(
            request, db, user,
            client_id=user.client_id if readonly else None,
            scope_label=_scope_label(db, user.client_id if readonly else None),
        ),
    )


# --- Printer detail --------------------------------------------------------- #
@router.get("/printers/{printer_id}", response_class=HTMLResponse)
def printer_detail(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    printer = db.get(m.Printer, printer_id)
    if printer is None or _forbidden_client(user, printer.client_id):
        return RedirectResponse("/", status_code=303)
    history = queries.page_count_history(db, printer_id, 60)
    events = list(
        db.scalars(
            select(m.PrinterEvent)
            .where(m.PrinterEvent.printer_id == printer_id)
            .order_by(m.PrinterEvent.ts.desc())
            .limit(25)
        )
    )
    maint = list(
        db.scalars(
            select(m.MaintenanceRecord)
            .where(m.MaintenanceRecord.printer_id == printer_id)
            .order_by(m.MaintenanceRecord.performed_at.desc())
        )
    )
    from central.dashboard.remote import panel_context

    return _render(
        request,
        "printer.html",
        db=db,
        user=user,
        printer=printer,
        supplies=printer.supplies,
        history=history,
        events=events,
        maintenance=maint,
        spark=_sparkline_points([r.page_count for r in history]),
        printer_label=_printer_label,
        # Remote hands. Returns {"remote_enabled": False} for a client_readonly
        # user or a switched-off install, so the card simply is not rendered --
        # the controls do not exist in the DOM to be found and posted to.
        **panel_context(db, printer, user),
    )


# --- Supplies to order and location-specific order ledger ------------------- #
@router.get("/supplies/reorder", response_class=HTMLResponse)
def supplies_reorder(request: Request, db: Session = Depends(get_db)):
    """Recommendations plus the operator's location-specific order ledger.

    client_readonly users are sent to their portal, which carries the same
    recommendations scoped to their own fleet -- the same treatment
    /security/posture gets. Admin/tech may narrow to one client with
    ``?client_id=``, which is validated against the clients that actually exist
    rather than trusted from the query string.
    """
    from central import reorder as reorder_mod
    from central import shipping_mailbox as shipping_mod
    from central import supply_inventory as inventory_mod
    from central import supply_orders as order_mod

    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/portal", status_code=303)

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    raw_client_id = (request.query_params.get("client_id") or "").strip()
    selected_id = None
    if raw_client_id:
        try:
            candidate = int(raw_client_id)
        except ValueError:
            candidate = None
        # Only a real client id narrows the view. An unparseable or unknown one
        # falls back to the whole fleet rather than silently returning an empty
        # page that reads as "nothing to order".
        if candidate is not None and any(c.id == candidate for c in clients):
            selected_id = candidate

    thresholds = reorder_mod.ReorderThresholds.load(db)
    recs = reorder_mod.recommendations(db, client_id=selected_id, thresholds=thresholds)
    orders = order_mod.active_orders(db, client_id=selected_id)
    stock = inventory_mod.stock_groups(db, client_id=selected_id)
    unresolved = inventory_mod.unresolved_usages(db, client_id=selected_id)
    shipping_notices = shipping_mod.pending_notices(db, client_id=selected_id)
    by_signature = order_mod.orders_by_signature(orders)
    orders_for_supply = {
        rec.supply_id: by_signature.get(
            order_mod.signature(rec.site_id, rec.model, rec.supply_type, rec.color), []
        )
        for rec in recs
    }
    return _render(
        request,
        "reorder.html",
        db=db,
        user=user,
        groups=reorder_mod.group_by_client(recs),
        summary=reorder_mod.summarize(recs),
        thresholds=thresholds,
        clients=clients,
        selected_client_id=selected_id,
        active_orders=orders,
        pending_shipping_notices=shipping_notices,
        shipping_options=shipping_mod.assignment_options(
            db, shipping_notices, client_id=selected_id
        ),
        shipping_by_order=shipping_mod.shipping_by_order(
            db, [order.id for order in orders]
        ),
        stock=stock,
        stock_compatible_printers=order_mod.compatible_printers(
            db, [item.order for item in stock]
        ),
        unresolved_usages=unresolved,
        usage_options=inventory_mod.assignment_options(db, unresolved),
        compatible_printers=order_mod.compatible_printers(db, orders),
        orders_for_supply=orders_for_supply,
        default_delivery_date=order_mod.default_delivery_date(),
        supply_flash=request.session.pop("supply_flash", None),
        urgency_now=reorder_mod.URGENCY_NOW,
        # A reorder list computed from supply levels nobody has refreshed since
        # yesterday is exactly as wrong as a green fleet that is on fire.
        **_freshness_ctx(request, db, user, client_id=selected_id,
                         scope_label=_scope_label(db, selected_id)),
    )


@router.post("/supplies/orders")
def supply_order_create(
    request: Request,
    supply_id: int = Form(...),
    quantity: int = Form(1),
    manufacturer: str = Form(""),
    sku: str = Form(""),
    vendor: str = Form("Sun Data Supply"),
    estimated_delivery_at: str = Form(""),
    db: Session = Depends(get_db),
):
    from central import supply_orders as order_mod
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    supply = db.get(m.Supply, supply_id)
    printer = db.get(m.Printer, supply.printer_id) if supply is not None else None
    if supply is None or printer is None or printer.discovery_state != m.DiscoveryState.approved:
        request.session["supply_flash"] = "That supply is no longer available to order."
        return RedirectResponse("/supplies/reorder", status_code=303)
    if quantity < 1 or quantity > 100:
        request.session["supply_flash"] = "Order quantity must be between 1 and 100."
        return RedirectResponse("/supplies/reorder", status_code=303)
    eta = None
    if estimated_delivery_at.strip():
        try:
            eta = date.fromisoformat(estimated_delivery_at.strip())
        except ValueError:
            request.session["supply_flash"] = "Estimated delivery must be a valid date."
            return RedirectResponse("/supplies/reorder", status_code=303)

    key = order_mod.signature(
        printer.site_id, printer.model or "", supply.type.value, supply.color
    )
    existing = order_mod.orders_by_signature(order_mod.active_orders(db)).get(key, [])
    if existing:
        request.session["supply_flash"] = (
            "An active order already covers this cartridge at this location."
        )
        return RedirectResponse(
            f"/supplies/reorder?client_id={printer.client_id}", status_code=303
        )

    order = m.SupplyOrder(
        site_id=printer.site_id,
        printer_id=printer.id,
        supply_id=supply.id,
        model=order_mod.one_line(printer.model, 200) or "Unknown model",
        supply_type=supply.type.value,
        color=order_mod.one_line(supply.color, 40),
        description=order_mod.one_line(supply.description, 200),
        manufacturer=(
            order_mod.one_line(manufacturer, 100)
            or order_mod.one_line(printer.brand, 100)
        ),
        sku=order_mod.one_line(sku, 120),
        quantity=quantity,
        vendor=order_mod.one_line(vendor, 160) or "Sun Data Supply",
        estimated_delivery_at=eta or order_mod.default_delivery_date(),
        created_by_user_id=user.id,
    )
    db.add(order)
    db.flush()
    record(
        db,
        request,
        user,
        "supply_order.create",
        target=f"supply_order:{order.id} site:{order.site_id}",
        detail=f"quantity={quantity}; supply={order.supply_type}/{order.color or 'unspecified'}",
    )
    db.commit()
    request.session["supply_flash"] = "Order recorded for this location."
    return RedirectResponse(
        f"/supplies/reorder?client_id={printer.client_id}", status_code=303
    )


@router.post("/supplies/orders/{order_id}/delivered")
def supply_order_delivered(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    order = db.get(m.SupplyOrder, order_id)
    if order is None:
        request.session["supply_flash"] = "That order no longer exists."
        return RedirectResponse("/supplies/reorder", status_code=303)
    order.status = m.SupplyOrderStatus.delivered
    order.delivered_at = datetime.now(timezone.utc)
    record(db, request, user, "supply_order.delivered", target=f"supply_order:{order.id}")
    db.commit()
    request.session["supply_flash"] = "Order marked delivered."
    return RedirectResponse("/supplies/reorder", status_code=303)


@router.post("/supplies/orders/{order_id}/delete")
def supply_order_delete(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    order = db.get(m.SupplyOrder, order_id)
    if order is None:
        request.session["supply_flash"] = "That order was already removed."
        return RedirectResponse("/supplies/reorder", status_code=303)
    if db.scalar(
        select(m.SupplyUsage.id).where(m.SupplyUsage.supply_order_id == order.id).limit(1)
    ) is not None:
        request.session["supply_flash"] = (
            "That stock record has cartridge use attached and cannot be deleted."
        )
        return RedirectResponse("/supplies/reorder", status_code=303)
    record(db, request, user, "supply_order.delete", target=f"supply_order:{order.id}")
    db.delete(order)
    db.commit()
    request.session["supply_flash"] = "Order record removed."
    return RedirectResponse("/supplies/reorder", status_code=303)


@router.post("/supplies/usages/{usage_id}/assign")
def supply_usage_assign(
    usage_id: int,
    request: Request,
    order_id: int = Form(...),
    db: Session = Depends(get_db),
):
    from central import supply_inventory as inventory_mod
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    usage = db.scalar(
        select(m.SupplyUsage)
        .where(m.SupplyUsage.id == usage_id)
        .with_for_update()
    )
    order = db.scalar(
        select(m.SupplyOrder)
        .where(m.SupplyOrder.id == order_id)
        .with_for_update()
    )
    if usage is None or order is None:
        request.session["supply_flash"] = "That stock assignment is no longer available."
        return RedirectResponse("/supplies/reorder", status_code=303)
    if not inventory_mod.assign_usage(db, usage, order, user_id=user.id):
        request.session["supply_flash"] = (
            "That cartridge is incompatible, already assigned, or no longer in stock."
        )
        return RedirectResponse("/supplies/reorder", status_code=303)
    record(
        db,
        request,
        user,
        "supply_usage.assign",
        target=f"supply_usage:{usage.id} supply_order:{order.id} site:{usage.site_id}",
    )
    client_id = usage.site.client_id
    db.commit()
    request.session["supply_flash"] = "Cartridge use assigned to location stock."
    return RedirectResponse(
        f"/supplies/reorder?client_id={client_id}", status_code=303
    )


@router.post("/supplies/usages/{usage_id}/delete")
def supply_usage_delete(
    usage_id: int, request: Request, db: Session = Depends(get_db)
):
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    usage = db.scalar(
        select(m.SupplyUsage)
        .where(m.SupplyUsage.id == usage_id)
        .with_for_update()
    )
    if usage is None:
        request.session["supply_flash"] = "That cartridge-use record was already removed."
        return RedirectResponse("/supplies/reorder", status_code=303)
    if usage.supply_order_id is not None:
        request.session["supply_flash"] = (
            "Assigned cartridge use cannot be deleted from the reconciliation queue."
        )
        return RedirectResponse("/supplies/reorder", status_code=303)
    client_id = usage.site.client_id
    record(
        db,
        request,
        user,
        "supply_usage.delete",
        target=f"supply_usage:{usage.id} site:{usage.site_id}",
    )
    db.delete(usage)
    db.commit()
    request.session["supply_flash"] = "Unmatched cartridge-use record removed."
    return RedirectResponse(
        f"/supplies/reorder?client_id={client_id}", status_code=303
    )


@router.post("/supplies/shipping/{notice_id}/assign")
def shipping_notice_assign(
    notice_id: int,
    request: Request,
    order_id: int = Form(...),
    db: Session = Depends(get_db),
):
    from central import shipping_mailbox
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    notice = db.scalar(
        select(m.ShippingNotice)
        .where(m.ShippingNotice.id == notice_id)
        .with_for_update()
    )
    order = db.scalar(
        select(m.SupplyOrder)
        .where(m.SupplyOrder.id == order_id)
        .with_for_update()
    )
    if notice is None or order is None:
        request.session["supply_flash"] = "That shipping assignment is no longer available."
        return RedirectResponse("/supplies/reorder", status_code=303)
    if not shipping_mailbox.assign_notice(notice, order):
        request.session["supply_flash"] = (
            "That notice is already assigned or the order is no longer active."
        )
        return RedirectResponse("/supplies/reorder", status_code=303)
    record(
        db,
        request,
        user,
        "shipping_notice.assign",
        target=f"shipping_notice:{notice.id} supply_order:{order.id} site:{order.site_id}",
    )
    client_id = order.site.client_id
    db.commit()
    request.session["supply_flash"] = "Shipping update assigned to the order."
    return RedirectResponse(
        f"/supplies/reorder?client_id={client_id}", status_code=303
    )


@router.post("/supplies/shipping/{notice_id}/delete")
def shipping_notice_delete(
    notice_id: int, request: Request, db: Session = Depends(get_db)
):
    from central.audit import record

    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect()
    notice = db.scalar(
        select(m.ShippingNotice)
        .where(m.ShippingNotice.id == notice_id)
        .with_for_update()
    )
    if notice is None:
        request.session["supply_flash"] = "That shipping notice was already removed."
        return RedirectResponse("/supplies/reorder", status_code=303)
    if notice.supply_order_id is not None:
        request.session["supply_flash"] = (
            "An assigned shipping notice cannot be deleted from the review queue."
        )
        return RedirectResponse("/supplies/reorder", status_code=303)
    client_id = notice.site.client_id if notice.site is not None else None
    record(
        db,
        request,
        user,
        "shipping_notice.delete",
        target=f"shipping_notice:{notice.id} site:{notice.site_id or 'unmatched'}",
    )
    db.delete(notice)
    db.commit()
    request.session["supply_flash"] = "Shipping notice removed."
    target = "/supplies/reorder"
    if client_id is not None:
        target = f"{target}?client_id={client_id}"
    return RedirectResponse(target, status_code=303)


# --- Device security posture ------------------------------------------------ #
@router.get("/security/posture", response_class=HTMLResponse)
def security_posture(request: Request, db: Session = Depends(get_db)):
    """Operator-facing "treat printers like endpoints" report: per-device SNMP
    transport grade and firmware visibility, plus a fleet summary.

    STAFF ONLY, and that is a product decision rather than an oversight. This
    report is an MSP remediation worklist, not a customer deliverable: every
    finding on it is about *our* monitoring configuration (the SNMP version and
    community string on a subnet row we own), and every fix for it lives on
    /manage/agents, which a client_readonly user cannot open. Publishing it to
    the portal would hand customers a red list they can neither act on nor
    verify, generating tickets whose only true answer is "yes, we know, and it
    is ours to change". Exposing it per-tenant is a deliberate later decision
    with wording written for that audience -- not a flag flip. The matching CSV
    export refuses client_readonly for the same reason.

    ``?client_id=N`` narrows to one client for staff, which is how the report
    is actually used (remediate one customer at a time). An unknown id is
    ignored rather than 404'd -- it is a filter, and the honest answer to a
    stale bookmark is the whole fleet, not an error page.
    """
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/portal", status_code=303)

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    try:
        selected_client_id = int(request.query_params.get("client_id") or 0) or None
    except ValueError:
        selected_client_id = None
    if selected_client_id is not None and not any(c.id == selected_client_id for c in clients):
        selected_client_id = None

    posture = queries.security_posture_rollup(db, client_id=selected_client_id)
    return _render(
        request,
        "security_posture.html",
        db=db,
        user=user,
        rows=posture["rows"],
        summary=posture["summary"],
        clients=clients,
        selected_client_id=selected_client_id,
        # Labels/tones for the flags and grades live with the logic that
        # decides them -- see queries.POSTURE_FLAG_META.
        flag_meta=queries.POSTURE_FLAG_META,
        grade_meta=queries.SNMP_GRADE_META,
        printer_label=_printer_label,
        **_freshness_ctx(request, db, user, client_id=selected_client_id,
                         scope_label=_scope_label(db, selected_client_id)),
    )


# --- Pending discovery approvals -------------------------------------------- #
@router.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    pending = list(
        db.scalars(
            select(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
            .order_by(m.Printer.created_at.desc())
        )
    )
    agents_by_id = {a.id: a for a in db.scalars(select(m.Agent))}
    return _render(
        request, "approvals.html", db=db, user=user,
        pending=pending, agents_by_id=agents_by_id,
        approvals_flash=request.session.pop("approvals_flash", None),
    )


@router.post("/approvals/{printer_id}/{action}", response_class=HTMLResponse)
def approval_action(
    printer_id: int, action: str, request: Request, db: Session = Depends(get_db)
):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    printer = db.get(m.Printer, printer_id)
    if printer is not None and action in ("approve", "ignore"):
        from central.audit import record

        printer.discovery_state = (
            m.DiscoveryState.approved if action == "approve" else m.DiscoveryState.ignored
        )
        record(db, request, user, f"printer.{action}",
               target=f"printer:{printer.id} {printer.ip}")
        db.commit()
    # HTMX swaps out the row; return empty so the row disappears.
    return HTMLResponse("")


@router.post("/approvals/bulk")
def approval_bulk(
    request: Request,
    printer_ids: list[int] = Form(default=[]),
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    """Approve or ignore a whole discovery batch in one submit.

    A new site arrives as tens of devices at once, and clearing that queue one
    button at a time is the single biggest click cost in onboarding.

    Every id is re-fetched and re-checked here rather than trusted from the
    form: the posted list is attacker-controllable, so an id that does not
    exist, or that names a printer which is no longer pending, is skipped
    instead of acted on. That also makes a double-submit harmless -- the second
    one finds nothing pending and changes nothing, rather than flipping a
    device somebody has since ignored back to approved.
    """
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    if action not in ("approve", "ignore"):
        return RedirectResponse("/approvals", status_code=303)

    from central.audit import record

    new_state = (
        m.DiscoveryState.approved if action == "approve" else m.DiscoveryState.ignored
    )
    changed = []
    for printer_id in printer_ids:
        printer = db.get(m.Printer, printer_id)
        if printer is None or printer.discovery_state != m.DiscoveryState.pending:
            continue
        printer.discovery_state = new_state
        changed.append(printer)

    if changed:
        # One audit row for the batch, but naming every printer it touched --
        # "approved 40 devices" without the list is not an audit trail. Capped
        # so a huge sweep can't blow the detail column, with the overflow
        # disclosed rather than silently truncated.
        shown = [f"{p.id}:{p.ip}" for p in changed[:50]]
        overflow = len(changed) - len(shown)
        detail = ", ".join(shown) + (f" (+{overflow} more)" if overflow else "")
        record(db, request, user, f"printer.bulk_{action}",
               target=f"{len(changed)} printers", detail=detail)
        db.commit()
        request.session["approvals_flash"] = (
            f"{len(changed)} device{'s' if len(changed) != 1 else ''} "
            f"{'approved' if action == 'approve' else 'ignored'}."
        )
    else:
        request.session["approvals_flash"] = "Nothing to do — those devices are no longer pending."
    return RedirectResponse("/approvals", status_code=303)


# --- Alerts inbox ----------------------------------------------------------- #
@router.get("/alerts", response_class=HTMLResponse)
def alerts_inbox(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    rows = list(
        db.scalars(
            select(m.Alert)
            .where(m.Alert.state != m.AlertState.resolved)
            # The inbox is a work queue, not an activity log. A newer warning
            # must never push a printer-stopping critical fault below the fold.
            .order_by(
                case(
                    (m.Alert.severity == m.EventSeverity.critical, 0),
                    (m.Alert.severity == m.EventSeverity.warning, 1),
                    else_=2,
                ),
                m.Alert.created_at.desc(),
            )
        )
    )
    # Notifications that were owed but never delivered (no channel enabled, or a
    # dead-letter on a still-live alert) are invisible otherwise -- an alert with
    # no badges reads as "not notified yet", not "nobody was told and nobody will
    # be". Surface them at the top of the inbox with the fix.
    return _render(
        request, "alerts.html", db=db, user=user, alerts=rows,
        undelivered=queries.undelivered_notifications(db),
        # An empty alerts inbox is the most reassuring screen in the product and
        # the one most worth qualifying: "no open alerts" over four-hour-old
        # readings means nobody has looked, not that nothing is wrong.
        **_freshness_ctx(request, db, user),
    )


@router.post("/alerts/{alert_id}/{action}", response_class=HTMLResponse)
def alert_action(alert_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    alert = db.get(m.Alert, alert_id)
    if alert is not None:
        from central.audit import record

        if action == "ack":
            alert.state = m.AlertState.acknowledged
        elif action == "resolve":
            alert.state = m.AlertState.resolved
            alert.resolved_at = datetime.now(timezone.utc)
            # An operator closing an alert by hand is as real a resolution as
            # the worker observing the condition clear, and a subscriber that
            # only heard about the open is left holding a fault that no longer
            # exists. Same emit, same idempotency key, so the worker resolving
            # the same alert a moment later adds nothing.
            from central.events.emit import emit_alert_resolved

            emit_alert_resolved(db, alert)
        record(db, request, user, f"alert.{action}",
               target=f"alert:{alert.id}", detail=alert.title or "")
        db.commit()
    return HTMLResponse("")


# --- My account: self-service profile + password change -------------------- #
@router.get("/account", response_class=HTMLResponse)
def account_view(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    return _render(
        request, "account.html", db=db, user=user,
        flash=request.session.pop("account_flash", None),
        error=request.session.pop("account_error", None),
        must_change_password=bool(user.must_change_password),
    )


@router.post("/account/password")
def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    # SSO-only users have no local password to change.
    if user.auth_provider != "local" or user.password_hash is None:
        request.session["account_error"] = (
            "Your account is managed by SSO — change your password with the identity provider."
        )
        return RedirectResponse("/account", status_code=303)
    if not verify_password(current_password, user.password_hash):
        request.session["account_error"] = "Current password is incorrect."
        return RedirectResponse("/account", status_code=303)
    if len(new_password) < MIN_PASSWORD_LENGTH:
        request.session["account_error"] = "New password must be at least 8 characters."
        return RedirectResponse("/account", status_code=303)
    if new_password != confirm_password:
        request.session["account_error"] = "New password and confirmation don't match."
        return RedirectResponse("/account", status_code=303)
    # Refusing to re-set the same password is what makes a forced rotation
    # actually rotate: otherwise the generated password printed in the container
    # log can be "changed" to itself and stays live.
    if verify_password(new_password, user.password_hash):
        request.session["account_error"] = "New password must differ from the current one."
        return RedirectResponse("/account", status_code=303)
    from central.audit import record
    from central.middleware import FORCE_PASSWORD_CHANGE_KEY

    user.password_hash = hash_password(new_password)
    was_forced = bool(user.must_change_password)
    user.must_change_password = False
    request.session.pop(FORCE_PASSWORD_CHANGE_KEY, None)
    # Changing a password is the thing you do when you think somebody else has
    # it, so every other session for this account has to stop -- otherwise the
    # attacker's cookie outlives the credential it came from by up to 12h. This
    # session is re-stamped below rather than revoked, so the person doing the
    # rotation is not logged out by their own action.
    user.session_epoch = (user.session_epoch or 0) + 1
    request.session[SESSION_EPOCH_KEY] = user.session_epoch
    record(db, request, user, "account.password_change",
           detail="forced rotation of a generated password" if was_forced else "")
    db.commit()
    request.session["account_flash"] = "Password changed."
    return RedirectResponse("/account", status_code=303)


# --- Discovery ---------------------------------------------------------------
# Folded into /manage/agents (each agent card carries its subnets' discovery
# status + rescan). The old URL redirects so bookmarks keep working; the
# rescan POST stays for anything that still targets it.
@router.get("/discovery")
def discovery_status(request: Request):
    return RedirectResponse("/manage/agents", status_code=303)


@router.post("/discovery/agents/{agent_id}/rescan")
def discovery_rescan(agent_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect() if user is None else RedirectResponse("/", status_code=303)
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        return RedirectResponse("/manage/agents", status_code=303)
    db.add(m.Command(agent_id=agent.id, type=m.CommandType.rescan, payload=None))
    db.commit()
    request.session["flash"] = (
        f"Rescan queued for {agent.name} — the agent will discover on its next heartbeat."
    )
    return RedirectResponse("/manage/agents", status_code=303)


# --- helpers ---------------------------------------------------------------- #
def _printer_label(printer: m.Printer) -> str:
    # Friendly name first: operators name printers so alerts and dashboards
    # say "Front Desk" instead of a model number + IP.
    name = printer.display_name or printer.model or printer.hostname or "printer"
    return f"{name} @ {printer.ip}"


def _sparkline_points(values: list, width: int = 280, height: int = 48) -> str:
    """Build an SVG polyline points string from a numeric series."""
    nums = [v for v in values if v is not None]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1
    step = width / (len(nums) - 1)
    pts = []
    for i, v in enumerate(nums):
        x = i * step
        y = height - ((v - lo) / span) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)
