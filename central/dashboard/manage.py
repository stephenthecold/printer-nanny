"""Management UI: create/edit/delete clients, sites, printers, and enroll agents.

Plain server-rendered forms (POST + redirect) -- robust and JS-free. Viewing and
creating require any logged-in user; deletes require admin.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from central import branding as branding_lib
from central import models as m
from central import services
from central import suppression
from central.audit import record
from central.branding import branding_for
from central.dashboard import _keystore
from central.db import get_db
from central.health import worker_banner
from central.runtime import load_settings
from central.security import generate_api_key, hash_api_key, hash_password


def _split_tags(raw: str) -> Optional[list[str]]:
    """Parse a comma-separated tag input into a clean list (None if empty)."""
    tags = [t.strip() for t in (raw or "").split(",") if t.strip()]
    return tags or None

router = APIRouter(prefix="/manage", tags=["manage"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_MANAGER_ROLES = {m.UserRole.admin, m.UserRole.tech}


def _user(request: Request, db: Session) -> Optional[m.User]:
    uid = request.session.get("user_id")
    user = db.get(m.User, uid) if uid else None
    # A deactivated (SCIM-deprovisioned) account is treated as logged out so a
    # live cookie stops working on its next request, not just at next login.
    return user if (user is not None and user.active) else None


def _manager(request: Request, db: Session) -> Optional[m.User]:
    """Management is for admin/tech only -- client_readonly users get nothing here."""
    user = _user(request, db)
    return user if (user is not None and user.role in _MANAGER_ROLES) else None


def _admin(request: Request, db: Session) -> Optional[m.User]:
    """Admin-only routes (user management, white-label settings) use this."""
    user = _user(request, db)
    return user if (user is not None and user.role == m.UserRole.admin) else None


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _pop_flash(request: Request) -> Optional[str]:
    return request.session.pop("flash", None)


def _tpl(request: Request, template: str, db: Session, **ctx) -> HTMLResponse:
    """Local render helper that always injects ``app.*`` branding into context.

    Keeps every manage template (nav, login, footer) in sync with the operator's
    Settings -> Branding values without each callsite having to remember.

    ``branding_for`` resolves to the global values for admin/tech -- an
    operator's chrome must not change identity as they move between tenants --
    and to their own client's branding for a client_readonly user, so a
    customer who reaches one of these pages does not see the portal's branding
    swap back to the MSP's halfway through their session.
    """
    from central import __version__ as _central_version

    ctx.setdefault("app", branding_for(db, ctx.get("user")))
    ctx.setdefault("central_version", _central_version)
    # Conditional Approvals nav: link only renders when something is pending.
    if "nav_pending" not in ctx:
        ctx["nav_pending"] = db.scalar(
            select(func.count())
            .select_from(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
        ) or 0
    # Stalled-worker banner (base.html) -- see central.health.worker_banner.
    if "worker_banner" not in ctx:
        ctx["worker_banner"] = worker_banner(db, ctx.get("user"))
    return _templates.TemplateResponse(request, template, ctx)


# --------------------------------------------------------------------------- #
# Clients & sites
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
def manage_home(request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    # All clients are exposed in a top-level "Add site" form, so we always need
    # the full list -- even when the operator just wants to click into a client.
    return _tpl(
        request, "manage_clients.html", db,
        user=user, clients=clients, flash=_pop_flash(request),
    )


@router.post("/clients")
def create_client(
    request: Request, name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)
):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Client(name=name.strip(), notes=notes.strip() or None))
        record(db, request, actor, "client.create", target=f"client:{name.strip()}")
        db.commit()
        _flash(request, f"Client '{name}' added.")
    return _redirect("/manage")


def _timezone_choices() -> list:
    """IANA zone names for the client-edit datalist, common ones first.

    Falls back to a small hand-list if the tz database isn't installed (slim
    containers), so the field is still usable rather than empty.
    """
    try:
        from zoneinfo import available_timezones

        return sorted(available_timezones())
    except Exception:  # noqa: BLE001
        return [
            "UTC", "America/New_York", "America/Chicago", "America/Denver",
            "America/Los_Angeles", "Europe/London", "Europe/Dublin",
            "Europe/Paris", "Europe/Berlin", "Australia/Sydney",
        ]


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_manage(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    printers = list(
        db.scalars(select(m.Printer).where(m.Printer.client_id == client_id).order_by(m.Printer.ip))
    )
    runtime = load_settings(db)
    # Whether this client has uploaded logo BYTES (as opposed to an external
    # URL), for the preview + remove controls. Guarded on the table existing
    # for the same reason /settings is: an operator who restarted the api
    # before migrations ran should get a page without the logo panel, not a
    # 500 on the client screen.
    has_brand_logo = (
        sa_inspect(db.get_bind()).has_table(m.AppAsset.__tablename__)
        and db.get(m.AppAsset, branding_lib.client_logo_asset_name(client.id)) is not None
    )
    return _tpl(
        request, "client_manage.html", db,
        user=user, client=client, sites=client.sites,
        printers=printers, flash=_pop_flash(request),
        default_tz=(runtime.get("alerts.default_timezone") or "UTC"),
        tz_choices=_timezone_choices(),
        has_brand_logo=has_brand_logo,
        # Sanitised here rather than in the template: the swatch renders it
        # into a style attribute, which is the same CSS sink base.html has.
        brand_swatch=branding_lib.safe_css_color(
            client.brand_primary_color, fallback=""
        ),
    )


@router.post("/clients/{client_id}")
def update_client(
    client_id: int, request: Request,
    name: str = Form(...), notes: str = Form(""),
    client_timezone: str = Form(""),
    db: Session = Depends(get_db),
):
    """``client_timezone`` is the IANA zone quiet hours are read in for this
    client. Blank means "inherit alerts.default_timezone". Rejected rather than
    stored when unresolvable: a bad zone silently falling back to UTC would move
    someone's quiet hours by hours without telling them."""
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client:
        tz = client_timezone.strip()
        if tz and not suppression.valid_timezone(tz):
            _flash(request, f"'{tz}' isn't a recognised timezone — left unchanged.")
            return _redirect(f"/manage/clients/{client_id}")
        client.name = name.strip() or client.name
        client.notes = notes.strip() or None
        client.timezone = tz or None
        record(db, request, actor, "client.update",
               target=f"client:{client.id} {client.name}",
               detail=f"timezone={client.timezone or 'inherit'}")
        db.commit()
        _flash(request, "Client updated.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/clients/{client_id}/branding")
def update_client_branding(
    client_id: int, request: Request,
    brand_name: str = Form(""),
    brand_primary_color: str = Form(""),
    brand_logo_url: str = Form(""),
    db: Session = Depends(get_db),
):
    """Per-client white-label overrides for the customer portal.

    Every field is optional and blank means "inherit the global setting", so a
    client with nothing set renders exactly as it does today.

    The colour is **validated, not escaped**: it is interpolated into a CSS
    declaration in base.html, where escaping stops an attribute breakout but
    not ``red; background-image: url(https://attacker/?c=)``. Anything that is
    not ``#rgb``/``#rrggbb`` is refused with a message rather than coerced --
    silently dropping it would leave an operator staring at an unchanged nav
    bar wondering which of the two fields they got wrong.

    The logo URL is constrained to http(s) or a site-relative path. Nothing is
    fetched server-side, so this is not an SSRF surface; the constraint is
    about what ends up in an ``<img src>``.
    """
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")

    name = (brand_name or "").strip()[:120]

    color_raw = (brand_primary_color or "").strip()
    color = branding_lib.normalise_hex_color(color_raw) if color_raw else None
    if color_raw and color is None:
        _flash(request, f"'{color_raw[:40]}' is not a #RRGGBB colour — branding unchanged.")
        record(db, request, actor, "client.branding.refused",
               target=f"client:{client.id} {client.name}",
               detail="primary_color rejected (not #rrggbb)")
        db.commit()
        return _redirect(f"/manage/clients/{client_id}")

    url_raw = (brand_logo_url or "").strip()
    # An upload points this at our own route; re-typing that path by hand is
    # equally valid, so there is nothing to special-case here.
    url = branding_lib.safe_logo_url(url_raw) if url_raw else None
    if url_raw and url is None:
        _flash(request, "Logo URL must be an https:// address or a /path — branding unchanged.")
        record(db, request, actor, "client.branding.refused",
               target=f"client:{client.id} {client.name}",
               detail="logo_url rejected (scheme or length)")
        db.commit()
        return _redirect(f"/manage/clients/{client_id}")

    before = (client.brand_name, client.brand_primary_color, client.brand_logo_url)
    client.brand_name = name or None
    client.brand_primary_color = color
    client.brand_logo_url = url
    after = (client.brand_name, client.brand_primary_color, client.brand_logo_url)
    if before != after:
        # Values, not just key names: a brand name, a hex colour and a logo URL
        # are operator-visible configuration, never secrets.
        record(db, request, actor, "client.branding.update",
               target=f"client:{client.id} {client.name}",
               detail="name={} color={} logo={}".format(
                   client.brand_name or "inherit",
                   client.brand_primary_color or "inherit",
                   client.brand_logo_url or "inherit",
               ))
    db.commit()
    _flash(request, "Portal branding saved.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/clients/{client_id}/branding/logo")
async def upload_client_branding_logo(
    client_id: int, request: Request,
    logo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload this client's portal logo.

    Deliberately the SAME uploader as Settings -> Branding: one size cap, one
    allow-list, one magic-byte check (``central.branding.validate_logo``), and
    the same ``app_assets`` blob store under a namespaced key. A second
    uploader is a second set of rules to get wrong.

    The uploaded filename is never used -- not as a key, not as a path, not in
    the response -- so there is no traversal to defend against. What is stored
    is the SNIFFED content type, and what is served is tenant-scoped.
    """
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")

    data = await logo.read()
    content_type, error = branding_lib.validate_logo(logo.content_type or "", data)
    if error:
        _flash(request, f"Logo upload: {error}")
        record(db, request, actor, "client.branding.logo.refused",
               target=f"client:{client.id} {client.name}", detail=error[:200])
        db.commit()
        return _redirect(f"/manage/clients/{client_id}")

    key = branding_lib.client_logo_asset_name(client.id)
    existing = db.get(m.AppAsset, key)
    if existing is None:
        db.add(m.AppAsset(
            name=key, content_type=content_type, data=data,
            updated_at=datetime.now(timezone.utc),
        ))
    else:
        existing.content_type = content_type
        existing.data = data
        existing.updated_at = datetime.now(timezone.utc)
    # Point the client's logo URL at the route that serves these bytes, the
    # same way the global upload wires up app.logo_url.
    client.brand_logo_url = branding_lib.client_logo_path(client.id)
    record(db, request, actor, "client.branding.logo",
           target=f"client:{client.id} {client.name}",
           detail=f"type={content_type} bytes={len(data)}")
    db.commit()
    _flash(request, f"Portal logo uploaded ({len(data) // 1024} KB).")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/clients/{client_id}/branding/logo/delete")
def delete_client_branding_logo(
    client_id: int, request: Request, db: Session = Depends(get_db)
):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    key = branding_lib.client_logo_asset_name(client.id)
    existing = db.get(m.AppAsset, key)
    if existing is not None:
        db.delete(existing)
    # Clear the URL only if it pointed at the upload -- an operator who pasted
    # an external CDN address keeps it, same rule as the global logo.
    if (client.brand_logo_url or "") == branding_lib.client_logo_path(client.id):
        client.brand_logo_url = None
    record(db, request, actor, "client.branding.logo.delete",
           target=f"client:{client.id} {client.name}")
    db.commit()
    _flash(request, "Portal logo removed.")
    return _redirect(f"/manage/clients/{client_id}")


def _printer_delete_blockers(db: Session, *, client_id=None, site_id=None):
    """Why a client or site cannot be deleted yet, or ``None`` when it can.

    Counts printers in **every** discovery state, not just approved ones. A
    pending or ignored row is still a NOT NULL ``client_id`` that the delete
    would try to blank, so filtering to approved would make the check pass and
    the delete fail exactly as before -- a guard that reports safety it does not
    have is worse than no guard.

    The counts are the message: "3 printers" sends an operator looking, "3
    printers (1 awaiting approval)" tells them where the third one is hiding,
    which is precisely the row they would not have thought to look for.
    """
    stmt = select(m.Printer.discovery_state, func.count()).group_by(
        m.Printer.discovery_state
    )
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)
    if site_id is not None:
        stmt = stmt.where(m.Printer.site_id == site_id)
    by_state = {state: count for state, count in db.execute(stmt)}
    total = sum(by_state.values())
    if not total:
        return None
    parts = [
        f"{count} {state.value}"
        for state, count in sorted(by_state.items(), key=lambda kv: kv[0].value)
    ]
    return {"total": total, "breakdown": ", ".join(parts)}


def _client_delete_blockers(db: Session, client_id: int):
    """Flash message + audit detail for a refused client delete, or ``None``."""
    blocked = _printer_delete_blockers(db, client_id=client_id)
    if blocked is None:
        return None
    noun = "printer" if blocked["total"] == 1 else "printers"
    return {
        "message": (
            f"Can't delete this client: it still has {blocked['total']} {noun} "
            f"({blocked['breakdown']}). Deleting them also deletes their entire "
            "reading history and page meters, which past invoices are derived "
            "from — so remove the printers first, deliberately."
        ),
        "detail": f"printers={blocked['total']} ({blocked['breakdown']})",
    }


@router.post("/clients/{client_id}/delete")
def delete_client(client_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a client, REFUSING while it still owns printers.

    WHY IT REFUSES RATHER THAN CASCADING. It used to do neither: ``printers``
    carries a plain ``ForeignKey`` to ``clients`` with no ``ondelete`` and the
    relationship declares no cascade, so SQLAlchemy issued
    ``UPDATE printers SET client_id=NULL`` against a NOT NULL column and the
    operator got a bare 500 (verified on both SQLite and Postgres). The fix is
    one of two behaviours, and the choice matters:

    * Cascading destroys the tenant's entire measurement history -- readings,
      rollups and page meters. Invoices in this product are **derived on demand,
      never stored** (see central.billing), so those rows are the only evidence
      any past invoice ever existed. There is no undo and no backup-shaped
      recovery that is not a whole-database restore.
    * It also cannot be delegated to the database here. This project runs SQLite
      for dev and the whole test suite and installs **no** ``PRAGMA
      foreign_keys`` listener, so ``ondelete="CASCADE"`` is inert there: adding
      it would cascade correctly on Postgres and silently orphan every reading
      on SQLite. A correct cascade would therefore have to be re-implemented in
      application code across a dozen tables, and every ``client_id`` column
      added afterwards becomes a silent orphan-leak the day somebody forgets it.

    Refusing has none of that surface, and it costs the operator a deliberate,
    per-printer, individually audited act instead of one irreversible click. The
    message names the blocker so the refusal is an instruction rather than a
    wall -- which is the whole difference from the 500 it replaces.
    """
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete clients.")
        return _redirect(f"/manage/clients/{client_id}")
    client = db.get(m.Client, client_id)
    if client:
        blocked = _client_delete_blockers(db, client_id)
        if blocked:
            # Audited as a refusal, not silently dropped: an admin pressing
            # Delete on a live tenant is worth a row either way.
            record(db, request, user, "client.delete_refused",
                   target=f"client:{client.id} {client.name}",
                   detail=blocked["detail"])
            db.commit()
            _flash(request, blocked["message"])
            return _redirect(f"/manage/clients/{client_id}")
        record(db, request, user, "client.delete",
               target=f"client:{client.id} {client.name}")
        # The branding columns go with the row, but the logo BYTES live in
        # app_assets keyed by client id and have no foreign key to cascade
        # along. Leaving them is not merely untidy: SQLite hands out the next
        # free rowid, so a client created after this delete can be given the
        # same id -- and would inherit the deleted tenant's logo. Delete it
        # with the client.
        asset = db.get(m.AppAsset, branding_lib.client_logo_asset_name(client.id))
        if asset is not None:
            db.delete(asset)
        db.delete(client)
        db.commit()
        _flash(request, "Client deleted.")
    return _redirect("/manage")


@router.post("/sites")
def create_site(
    request: Request, client_id: int = Form(...), name: str = Form(...),
    address: str = Form(""), contact: str = Form(""), db: Session = Depends(get_db),
):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Site(
            client_id=client_id, name=name.strip(),
            address=address.strip() or None, contact=contact.strip() or None,
        ))
        record(db, request, actor, "site.create",
               target=f"site:{name.strip()} (client:{client_id})")
        db.commit()
        _flash(request, f"Site '{name}' added.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/sites/{site_id}/delete")
def delete_site(site_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a site, REFUSING while it still holds printers.

    The same defect as ``delete_client``, one level down and reachable from the
    adjacent button: ``printers.site_id`` is NOT NULL with no ``ondelete`` and
    ``Site.printers`` declares no cascade, so this raised the identical
    IntegrityError. Fixing only the client route would have left a 500 two
    clicks away -- and this one is worse, because deleting a client cascades to
    its sites, so the client path reaches this failure through BOTH foreign keys.
    """
    user = _manager(request, db)
    site = db.get(m.Site, site_id)
    if user is None or site is None:
        return _redirect("/manage")
    client_id = site.client_id
    if user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete sites.")
        return _redirect(f"/manage/clients/{client_id}")
    blocked = _printer_delete_blockers(db, site_id=site_id)
    if blocked:
        noun = "printer" if blocked["total"] == 1 else "printers"
        record(db, request, user, "site.delete_refused",
               target=f"site:{site.id} {site.name}",
               detail=f"printers={blocked['total']} ({blocked['breakdown']})")
        db.commit()
        _flash(
            request,
            f"Can't delete this site: it still has {blocked['total']} {noun} "
            f"({blocked['breakdown']}). Move them to another site or delete "
            "them first — deleting a printer also deletes its reading history."
        )
        return _redirect(f"/manage/clients/{client_id}")
    record(db, request, user, "site.delete",
           target=f"site:{site.id} {site.name}")
    db.delete(site)
    db.commit()
    _flash(request, "Site deleted.")
    return _redirect(f"/manage/clients/{client_id}")


# --------------------------------------------------------------------------- #
# Printers (manual add / edit)
# --------------------------------------------------------------------------- #
@router.get("/printers/new", response_class=HTMLResponse)
def printer_new(
    request: Request, client_id: int, site_id: Optional[int] = None, db: Session = Depends(get_db)
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    return _tpl(
        request, "printer_form.html", db,
        user=user, client=client, sites=client.sites,
        printer=None, selected_site_id=site_id,
    )


@router.get("/printers/{printer_id}/edit", response_class=HTMLResponse)
def printer_edit(
    printer_id: int, request: Request,
    from_approvals: int = 0,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer is None:
        return _redirect("/manage")
    client = db.get(m.Client, printer.client_id)
    return _tpl(
        request, "printer_form.html", db,
        user=user, client=client, sites=client.sites,
        printer=printer, selected_site_id=printer.site_id,
        from_approvals=bool(from_approvals),
    )


@router.post("/printers")
def printer_create(
    request: Request,
    client_id: int = Form(...), site_id: int = Form(...), ip: str = Form(...),
    hostname: str = Form(""), brand: str = Form(""), model: str = Form(""),
    serial: str = Form(""), location: str = Form(""),
    snmp_version: str = Form("2c"), snmp_community: str = Form("public"),
    asset_tag: str = Form(""), tags: str = Form(""), notes: str = Form(""),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = m.Printer(
        client_id=client_id, site_id=site_id, ip=ip.strip(),
        display_name=display_name.strip() or None,
        hostname=hostname.strip() or None, brand=brand.strip() or None,
        model=model.strip() or None, serial=serial.strip() or None,
        location=location.strip() or None, snmp_version=snmp_version,
        snmp_community=snmp_community.strip() or "public",
        asset_tag=asset_tag.strip() or None,
        tags=_split_tags(tags),
        notes=notes.strip() or None,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    record(db, request, _manager(request, db), "printer.create",
           target=f"printer:{printer.ip} (client:{client_id})")
    db.commit()
    _flash(request, f"Printer {ip} added.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/printers/{printer_id}")
def printer_update(
    printer_id: int, request: Request,
    site_id: int = Form(...), ip: str = Form(...), hostname: str = Form(""),
    brand: str = Form(""), model: str = Form(""), serial: str = Form(""),
    location: str = Form(""), snmp_version: str = Form("2c"),
    snmp_community: str = Form("public"),
    asset_tag: str = Form(""), tags: str = Form(""), notes: str = Form(""),
    approve: str = Form(""),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save printer edits. If ``approve=1`` and the printer is pending, also approve it."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.site_id = site_id
        printer.ip = ip.strip()
        printer.display_name = display_name.strip() or None
        printer.hostname = hostname.strip() or None
        printer.brand = brand.strip() or None
        printer.model = model.strip() or None
        printer.serial = serial.strip() or None
        printer.location = location.strip() or None
        printer.snmp_version = snmp_version
        printer.snmp_community = snmp_community.strip() or "public"
        printer.asset_tag = asset_tag.strip() or None
        printer.tags = _split_tags(tags)
        printer.notes = notes.strip() or None
        approved_now = False
        if approve and printer.discovery_state != m.DiscoveryState.approved:
            printer.discovery_state = m.DiscoveryState.approved
            approved_now = True
        record(db, request, _manager(request, db),
               "printer.approve" if approved_now else "printer.update",
               target=f"printer:{printer.id} {printer.ip}")
        db.commit()
        _flash(request, "Printer approved." if approved_now else "Printer updated.")
        # After approval (typically reached from /approvals), bounce back there
        # so the operator can keep working through the queue.
        if approved_now:
            return _redirect("/approvals")
        return _redirect(f"/manage/clients/{printer.client_id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/delete")
def printer_delete(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    printer = db.get(m.Printer, printer_id)
    if user is None or printer is None:
        return _redirect("/manage")
    client_id = printer.client_id
    record(db, request, user, "printer.delete",
           target=f"printer:{printer.id} {printer.ip}")
    db.delete(printer)
    db.commit()
    _flash(request, "Printer deleted.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/printers/{printer_id}/ignore")
def printer_ignore(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Move a printer back to the ignored state so the agent stops polling it."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.discovery_state = m.DiscoveryState.ignored
        record(db, request, _manager(request, db), "printer.ignore",
               target=f"printer:{printer.id} {printer.ip}")
        db.commit()
        _flash(request, f"Stopped monitoring printer {printer.ip}.")
        return _redirect(f"/manage/clients/{printer.client_id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/approve")
def printer_approve(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """One-click approve from the detail page (no field edits)."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.discovery_state = m.DiscoveryState.approved
        record(db, request, _manager(request, db), "printer.approve",
               target=f"printer:{printer.id} {printer.ip}")
        db.commit()
        _flash(request, f"Printer {printer.ip} approved.")
        return _redirect(f"/printers/{printer.id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/driver-tier")
def printer_driver_tier_override(
    printer_id: int,
    request: Request,
    driver_tier_override: str = Form(""),
    db: Session = Depends(get_db),
):
    """Pin (or clear) the workstation driver tier for one printer.

    The probe is right about most devices and wrong about some -- a printer that
    advertises IPP/2.0 and PWG Raster but whose firmware lies, or a model an
    operator knows needs its vendor driver for a feature the standard cannot
    express. This is the escape hatch, kept in its own column so a later re-probe
    refreshes what we *observed* without discarding what a human *decided*.

    Only the two actionable tiers are settable. ``ipp_disabled`` / ``unreachable``
    / ``error`` describe a failure to reach or decode the device -- states you fix
    on the device or the network, not opinions to pin -- so accepting them would
    let an operator record a diagnosis the client cannot act on.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer is None:
        return _redirect("/manage")

    raw = (driver_tier_override or "").strip()
    if not raw:
        new_value = None
    else:
        try:
            candidate = m.DriverTier(raw)
        except ValueError:
            _flash(request, f"Unknown driver tier {raw!r}.")
            return _redirect(f"/printers/{printer.id}")
        if candidate not in m.OVERRIDABLE_DRIVER_TIERS:
            _flash(request, f"{candidate.value} describes a probe failure and cannot be pinned.")
            return _redirect(f"/printers/{printer.id}")
        new_value = candidate

    printer.driver_tier_override = new_value
    # Audit both sides: "who decided this device needs a vendor driver, and what
    # were we detecting at the time" is the question asked when a queue misbehaves.
    observed = printer.driver_tier.value if printer.driver_tier else "unprobed"
    record(
        db,
        request,
        user,
        "printer.driver_tier_override",
        target=f"printer:{printer.id} {printer.ip}",
        detail=f"set={new_value.value if new_value else 'cleared'} observed={observed}",
    )
    db.commit()
    _flash(
        request,
        f"Driver tier pinned to {new_value.value}." if new_value
        else "Driver tier override cleared; using the probe result.",
    )
    return _redirect(f"/printers/{printer.id}")


@router.post("/printers/{printer_id}/poll")
def printer_poll_now(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Enqueue an immediate poll for one printer on its owning agent.

    The agent picks the command up on its next heartbeat and polls just this IP,
    so an operator can refresh a single device without waiting for the normal
    cycle. Falls back to any agent at the printer's site if no discoverer is set.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer is None:
        return _redirect("/manage")

    agent_id = printer.discovered_by_agent_id
    if agent_id is None:
        agent = db.scalar(select(m.Agent).where(m.Agent.site_id == printer.site_id))
        agent_id = agent.id if agent else None
    if agent_id is None:
        _flash(request, "No agent is assigned to this site -- cannot poll.")
        return _redirect(f"/printers/{printer.id}")

    db.add(m.Command(
        agent_id=agent_id,
        type=m.CommandType.poll_printer,
        payload={"printer_id": printer.id, "ip": printer.ip},
    ))
    record(db, request, _manager(request, db), "printer.poll_now",
           target=f"printer:{printer.id} {printer.ip}")
    db.commit()
    _flash(request, "Poll queued. The agent will refresh this printer on its next heartbeat.")
    return _redirect(f"/printers/{printer.id}")


# --------------------------------------------------------------------------- #
# Agents & subnets (enrollment)
# --------------------------------------------------------------------------- #
@router.get("/agents", response_class=HTMLResponse)
def agents_home(request: Request, db: Session = Depends(get_db)):
    from central import __version__ as central_version
    from central.agent_release import bundled_agent_version, update_state
    from central.msi_builder import msi_build_available
    from central.runtime import load_settings

    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    msi_cap = msi_build_available()
    agents = list(db.scalars(select(m.Agent).order_by(m.Agent.id)))
    # Version the central server serves -> what each agent should be at. Compare
    # by SemVer base (ignoring the install-time marker suffix) so we flag agents
    # whose *code* is older, not just ones that were installed at a different time.
    target_agent_version = bundled_agent_version()
    agent_update_state = {a.id: update_state(a.version, target_agent_version) for a in agents}
    outdated_count = sum(1 for st in agent_update_state.values() if st == "outdated")
    sites = list(db.scalars(
        select(m.Site).join(m.Client).order_by(m.Client.name, m.Site.name)
    ))
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    rt = load_settings(db)
    # Prefer the operator-pinned public URL so the agent install command always
    # uses the public HTTPS hostname even if this request hit the API directly
    # via an internal address. Falls back to the request URL otherwise.
    public_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    # Group sites by client so the cross-site subnet picker can render an
    # optgroup-style picker -- making the multi-client agent pattern visible.
    sites_by_client: dict[int, list[m.Site]] = {}
    for site in sites:
        sites_by_client.setdefault(site.client_id, []).append(site)
    # Discovery status lives here now (the standalone Discovery page folded
    # in): per-site pending-approval counts so each subnet row can show how
    # many discovered devices are waiting.
    pending_by_site = {
        site_id: count
        for site_id, count in db.execute(
            select(m.Printer.site_id, func.count())
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
            .group_by(m.Printer.site_id)
        ).all()
    }
    # Collector redundancy. ``collector_state`` is per subnet and says which of
    # held / lapsed / released it is in right now -- a lapsed lease is not the
    # same as a live one and an operator reading "collector: Agent A" would
    # otherwise be told a dead agent is collecting.
    from central import collector as _collector

    now = datetime.now(timezone.utc)
    collector_state = {}
    standby_for: dict[int, list] = {}
    for subnet in db.scalars(select(m.Subnet)):
        collector_state[subnet.id] = _collector.holder_state(subnet, now)
        if subnet.standby_agent_id is not None:
            standby_for.setdefault(subnet.standby_agent_id, []).append(subnet)
    agent_names = {a.id: a.name for a in agents}
    return _tpl(
        request, "agents.html", db,
        user=user, agents=agents, sites=sites,
        clients=clients,
        sites_by_client=sites_by_client,
        pending_by_site=pending_by_site,
        collector_state=collector_state,
        standby_for=standby_for,
        agent_names=agent_names,
        new_key=_keystore.pop(request.session.pop("new_agent_key_token", None)),
        new_claim=_keystore.pop(request.session.pop("new_claim_code_token", None)),
        central_url=public_url,
        pip_source=rt["agent.pip_source"],
        docker_image=rt["agent.docker_image"],
        central_version=central_version,
        target_agent_version=target_agent_version,
        agent_update_state=agent_update_state,
        outdated_count=outdated_count,
        msi_available=msi_cap.available,
        msi_reason=msi_cap.reason,
        flash=_pop_flash(request),
    )


@router.post("/agents")
def agent_create(
    request: Request, site_id: int = Form(...), name: str = Form(...),
    db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    api_key = generate_api_key()
    agent = m.Agent(site_id=site_id, name=name.strip() or "agent", api_key_hash=hash_api_key(api_key))
    db.add(agent)
    record(db, request, _manager(request, db), "agent.create",
           target=f"agent:{name.strip() or 'agent'} (site:{site_id})")
    db.commit()
    # Surface the plaintext key exactly once. Keep it server-side (not in the
    # signed-but-readable session cookie); the session holds only a one-shot token.
    request.session["new_agent_key_token"] = _keystore.put(
        {"id": agent.id, "name": agent.name, "key": api_key}
    )
    return _redirect("/manage/agents")


@router.get("/onboard", response_class=HTMLResponse)
def onboard_form(request: Request, db: Session = Depends(get_db)):
    """One screen for "a new customer signed, get them monitored".

    The pieces already existed -- client, site, subnet, agent -- but as four
    forms on three pages, none of which carried context to the next. Nothing
    here is a new concept; it is the same four objects created in one
    transaction, ending on the install command instead of leaving the operator
    to go find it.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    runtime = load_settings(db)
    return _tpl(
        request, "onboard.html", db, user=user,
        clients=list(db.scalars(select(m.Client).order_by(m.Client.name))),
        tz_choices=_timezone_choices(),
        default_tz=(runtime.get("alerts.default_timezone") or "UTC"),
        flash=_pop_flash(request),
    )


@router.post("/onboard")
def onboard_submit(
    request: Request,
    client_name: str = Form(...),
    client_timezone: str = Form(""),
    site_name: str = Form(...),
    agent_name: str = Form(""),
    cidr: str = Form(""),
    snmp_community: str = Form("public"),
    snmp_version: str = Form("2c"),
    trusted: str = Form(""),
    ttl_minutes: int = Form(1440),
    db: Session = Depends(get_db),
):
    """Create client + site + (optional) subnet, and mint the claim code.

    An existing client of the same name is reused rather than rejected --
    onboarding a second site for a customer you already have is the common
    case, and ``clients.name`` is unique, so creating blindly would just 500.
    The site is not reused: two sites with the same name under one client is a
    legitimate thing to want, and silently merging them would be worse than a
    duplicate an operator can rename.

    Everything commits together. A half-built customer (client, no site, no
    code) is the state that makes an operator start over by hand, so the whole
    point is that this either lands or doesn't.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    client_name = client_name.strip()
    site_name = site_name.strip()
    if not client_name or not site_name:
        _flash(request, "Client and site names are both required.")
        return _redirect("/manage/onboard")

    tz = client_timezone.strip()
    if tz and not suppression.valid_timezone(tz):
        _flash(request, f"'{tz}' isn't a recognised timezone — pick one from the list.")
        return _redirect("/manage/onboard")

    client = db.scalar(select(m.Client).where(m.Client.name == client_name))
    if client is None:
        client = m.Client(name=client_name, timezone=tz or None)
        db.add(client)
        db.flush()
        record(db, request, user, "client.create", target=f"client:{client_name}",
               detail="via onboarding")
        # Only for a brand-new client. Re-applying to an existing one would
        # stack a second copy of every default and overwrite nothing usefully.
        applied = services.apply_onboarding_defaults(db, client, load_settings(db))
        if applied:
            record(db, request, user, "client.defaults_applied",
                   target=f"client:{client.id} {client_name}",
                   detail="; ".join(applied))
    elif tz and not client.timezone:
        # Only fill a gap; never overwrite a zone somebody already set.
        client.timezone = tz

    site = m.Site(client_id=client.id, name=site_name)
    db.add(site)
    db.flush()
    record(db, request, user, "site.create",
           target=f"site:{site.id} {site_name} (client:{client.id})",
           detail="via onboarding")

    cidr = cidr.strip()
    if cidr:
        is_trusted = bool(trusted.strip())
        # agent_id stays NULL: the agent does not exist yet and will adopt this
        # subnet when it redeems the claim code (see services.redeem_claim_token).
        db.add(m.Subnet(
            site_id=site.id, agent_id=None, cidr=cidr,
            snmp_community=snmp_community.strip() or "public",
            snmp_version=snmp_version, trusted=is_trusted,
        ))
        record(db, request, user, "subnet.create",
               target=f"subnet:{cidr} site:{site.id}",
               detail=f"snmp v{snmp_version} via onboarding"
                      + (" trusted=on (auto-approves discoveries)" if is_trusted else ""))

    name = agent_name.strip() or f"{client_name} {site_name} agent"
    token, code = services.mint_claim_token(
        db, site_id=site.id, agent_name=name,
        ttl_minutes=ttl_minutes, created_by_user_id=user.id,
    )
    record(db, request, user, "agent.claim_code_mint",
           target=f"claim_token:{token.id} site:{site.id}",
           detail=f"via onboarding, expires {token.expires_at:%Y-%m-%d %H:%M} UTC")
    db.commit()

    request.session["new_claim_code_token"] = _keystore.put({
        "code": code,
        "site": f"{client_name} / {site_name}",
        "name": name,
        "expires_at": token.expires_at,
    })
    _flash(request, f"{client_name} / {site_name} is ready — run the install command below.")
    return _redirect("/manage/agents")


@router.post("/agents/claim-code")
def agent_claim_code(
    request: Request,
    site_id: int = Form(...),
    name: str = Form(""),
    ttl_minutes: int = Form(60),
    db: Session = Depends(get_db),
):
    """Mint a single-use claim code so an agent can enroll itself.

    The alternative this replaces is pasting a long-lived API key into whatever
    channel reaches the site. That key stays valid wherever it was pasted; a
    claim code stops being worth anything the moment it is used, so the copy
    left behind in a chat log is inert.

    Shown once, like the API key it replaces, and via the same server-side
    one-shot store -- the session cookie is signed but readable, so a live
    credential must not travel in it.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    site = db.get(m.Site, site_id)
    if site is None:
        _flash(request, "Pick a site for the claim code.")
        return _redirect("/manage/agents")

    token, code = services.mint_claim_token(
        db, site_id=site.id, agent_name=name.strip(),
        ttl_minutes=ttl_minutes, created_by_user_id=user.id,
    )
    # Target/detail carry the token id and its window, never the code itself.
    record(db, request, user, "agent.claim_code_mint",
           target=f"claim_token:{token.id} site:{site.id}",
           detail=f"expires {token.expires_at:%Y-%m-%d %H:%M} UTC")
    db.commit()
    request.session["new_claim_code_token"] = _keystore.put({
        "code": code,
        "site": f"{site.client.name} / {site.name}" if site.client else site.name,
        "name": name.strip() or f"Agent for {site.name}",
        "expires_at": token.expires_at,
    })
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/rotate-key")
def agent_rotate_key(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Issue a fresh API key for an agent (e.g. if the original was lost)."""
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent:
        api_key = generate_api_key()
        agent.api_key_hash = hash_api_key(api_key)
        record(db, request, _manager(request, db), "agent.rotate_key",
               target=f"agent:{agent.id} {agent.name}")
        db.commit()
        request.session["new_agent_key_token"] = _keystore.put(
            {"id": agent.id, "name": agent.name, "key": api_key}
        )
    return _redirect("/manage/agents")


@router.post("/agents/msi")
def agent_build_msi(
    request: Request,
    agent_id: int = Form(...),
    api_key: str = Form(...),
    db: Session = Depends(get_db),
):
    """Build and stream a self-contained Windows ``.msi`` for one enrolled agent.

    Driven from the post-enrollment "install command" block, where the plaintext
    API key is on screen exactly once -- the same key is carried here in a hidden
    field and baked into the MSI's config.toml. The build bundles the Python
    embeddable runtime + the agent + NSSM, so the target Windows Server needs no
    Python, no winget, and only outbound HTTPS to central. Manager-gated and
    audited; the key never appears in the audit detail or logs.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")

    from central.msi_builder import build_msi, msi_build_available

    cap = msi_build_available()
    if not cap.available:
        record(db, request, user, "agent.msi_build",
               target=f"agent:{agent.id} {agent.name}", detail=f"unavailable: {cap.reason}")
        db.commit()
        _flash(request, cap.reason)
        return _redirect("/manage/agents")

    from central.runtime import load_settings
    rt = load_settings(db)
    central_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    embed_url = str(rt.get("agent.python_embed_url") or "").strip() or None

    out_dir = Path(tempfile.mkdtemp(prefix="pn-msi-"))
    try:
        result = build_msi(
            agent_id=agent.id, agent_name=agent.name,
            central_url=central_url, api_key=api_key, verify_tls=True,
            out_dir=out_dir, embed_url=embed_url,
        )
    except Exception as exc:  # noqa: BLE001 - any build failure -> flash, not a 500
        shutil.rmtree(out_dir, ignore_errors=True)
        record(db, request, user, "agent.msi_build",
               target=f"agent:{agent.id} {agent.name}", detail=f"failed: {exc}")
        db.commit()
        _flash(request, f"MSI build failed: {exc}")
        return _redirect("/manage/agents")

    record(db, request, user, "agent.msi_build", target=f"agent:{agent.id} {agent.name}",
           detail=f"ok size={result.size} agent_version={result.agent_version} "
                  f"product={result.product_version}")
    db.commit()
    # Stream the artifact, then clean the temp dir (which holds the only on-disk
    # copy of the key, inside the MSI) once the response is fully sent.
    return FileResponse(
        path=str(result.path),
        media_type="application/x-msi",
        filename=f"printer-nanny-agent-{agent.id}.msi",
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
    )


@router.post("/agents/claim-msi")
def agent_build_claim_msi(
    request: Request,
    claim_code: str = Form(...),
    site_label: str = Form(""),
    db: Session = Depends(get_db),
):
    """Build a Windows MSI that carries a single-use claim code instead of a key.

    Same installer, better credential. The pre-minted variant bakes an API key
    that stays valid for the life of the agent, so every copy of that .msi --
    the file share it was staged on, the ticket it was attached to -- remains a
    working credential. A claim code is spent by the first machine that installs
    it, which makes a stale MSI inert.

    That single-use property is also the operational catch, and it is stated on
    the page: this artifact enrolls exactly ONE machine. Imaging a fleet from it
    gets you one agent and a queue of 401s, which is the correct outcome for a
    credential that must not be shared but is worth saying out loud.

    The code is never audited or logged -- it is live until redeemed.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    from central.msi_builder import build_msi, msi_build_available

    # Validate the credential BEFORE probing the toolchain or doing any build
    # work. Checking capability first would let an unknown or spent code still
    # produce build-related audit rows, which is both misleading in the trail
    # and work done on behalf of something that was never valid.
    from central.security import hash_claim_code

    token = db.scalar(
        select(m.AgentClaimToken).where(
            m.AgentClaimToken.token_hash == hash_claim_code(claim_code.strip())
        )
    )
    if token is None or token.used_at is not None:
        # Already redeemed, or never existed. Same message either way, matching
        # the redemption endpoint -- no oracle here either.
        _flash(request, "That claim code is no longer valid. Mint a fresh one.")
        return _redirect("/manage/agents")

    cap = msi_build_available()
    if not cap.available:
        record(db, request, user, "agent.msi_build",
               target=f"claim_token:{token.id} site:{token.site_id}",
               detail=f"unavailable: {cap.reason}")
        db.commit()
        _flash(request, cap.reason)
        return _redirect("/manage/agents")

    from central.runtime import load_settings
    rt = load_settings(db)
    central_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    embed_url = str(rt.get("agent.python_embed_url") or "").strip() or None

    out_dir = Path(tempfile.mkdtemp(prefix="pn-msi-"))
    try:
        result = build_msi(
            agent_name=token.agent_name or "Printer Nanny agent",
            central_url=central_url, claim_code=claim_code.strip(),
            slug=f"claim-{token.id}", verify_tls=True,
            out_dir=out_dir, embed_url=embed_url,
        )
    except Exception as exc:  # noqa: BLE001 - any build failure -> flash, not a 500
        shutil.rmtree(out_dir, ignore_errors=True)
        record(db, request, user, "agent.msi_build",
               target=f"claim_token:{token.id} site:{token.site_id}",
               detail=f"failed: {exc}")
        db.commit()
        _flash(request, f"MSI build failed: {exc}")
        return _redirect("/manage/agents")

    record(db, request, user, "agent.msi_build",
           target=f"claim_token:{token.id} site:{token.site_id}",
           detail=f"ok claim-code build size={result.size} "
                  f"agent_version={result.agent_version} product={result.product_version}")
    db.commit()
    safe_label = "".join(
        ch for ch in (site_label or "site") if ch.isalnum() or ch in "-_"
    )[:40] or "site"
    return FileResponse(
        path=str(result.path),
        media_type="application/x-msi",
        filename=f"printer-nanny-agent-{safe_label}.msi",
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
    )


@router.post("/agents/{agent_id}/update")
def agent_update_command(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue an update_agent command. The agent picks it up on its next
    heartbeat (~60s), pip-installs the configured agent.pip_source, then exits
    so the service manager restarts it against the new code.

    Operator-driven only: there's no automatic rolling-update story yet (the
    design doc lists "secure auto-update path eventually" -- this is the
    eventually). A confirmation dialog is in the template.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")
    from central.runtime import load_settings
    rt = load_settings(db)
    pip_source = str(rt.get("agent.pip_source") or "").strip()
    if not pip_source or "your-org" in pip_source:
        _flash(
            request,
            "Set Settings -> Agent install -> Pip source to your real repo "
            "before pushing updates; the placeholder won't install.",
        )
        return _redirect("/manage/agents")
    db.add(m.Command(
        agent_id=agent.id,
        type=m.CommandType.update_agent,
        payload={"pip_source": pip_source},
    ))
    record(db, request, _manager(request, db), "agent.update_queued",
           target=f"agent:{agent.id} {agent.name}", detail=pip_source)
    db.commit()
    _flash(request, f"Update queued for '{agent.name}' (picks up on next heartbeat).")
    return _redirect("/manage/agents")


@router.post("/agents/update-outdated")
@router.post("/agents/update-all")  # legacy path alias -> same outdated-only action
def agents_update_outdated(request: Request, db: Session = Depends(get_db)):
    """Queue update_agent for every OUTDATED enrolled agent. Admin only -- one
    command per agent so a single failure doesn't cascade.

    "Outdated" = the agent's reported SemVer base is strictly older than the
    version central serves (``bundled_agent_version()``). Agents that are
    current, ahead, or never reported a version are skipped: we don't push a
    blind update to an agent we can't confirm needs one. An agent that already
    has a pending ``update_agent`` command is also skipped so a double-click /
    repeat doesn't pile up duplicate commands before the heartbeat drains them.
    """
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can mass-update agents.")
        return _redirect("/manage/agents")
    from central.agent_release import bundled_agent_version, needs_update
    from central.runtime import load_settings
    rt = load_settings(db)
    pip_source = str(rt.get("agent.pip_source") or "").strip()
    if not pip_source or "your-org" in pip_source:
        _flash(
            request,
            "Set Settings -> Agent install -> Pip source to your real repo first.",
        )
        return _redirect("/manage/agents")
    target = bundled_agent_version()
    # Agents that already have an update_agent command waiting (not yet picked
    # up) -- don't double-queue those.
    already_pending = set(db.scalars(
        select(m.Command.agent_id).where(
            m.Command.type == m.CommandType.update_agent,
            m.Command.status == m.CommandStatus.pending,
        )
    ))
    agents = list(db.scalars(select(m.Agent)))
    queued = 0
    skipped_pending = 0
    for agent in agents:
        if not needs_update(agent.version, target):
            continue
        if agent.id in already_pending:
            skipped_pending += 1
            continue
        db.add(m.Command(
            agent_id=agent.id,
            type=m.CommandType.update_agent,
            payload={"pip_source": pip_source},
        ))
        queued += 1
    record(db, request, user, "agent.update_outdated",
           detail=(f"queued={queued}; skipped_pending={skipped_pending}; "
                   f"target={target}; source={pip_source}"))
    db.commit()
    if queued:
        msg = f"Update queued for {queued} outdated agent(s) (target {target})."
    elif skipped_pending:
        msg = f"No new updates queued -- {skipped_pending} already pending."
    else:
        msg = f"All agents are up to date (target {target}); nothing to update."
    _flash(request, msg)
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/rescan")
def agent_rescan(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue a discovery rescan for this agent (picked up on next heartbeat).

    Mirror of POST /discovery/agents/{agent_id}/rescan but lives on /manage/agents
    so the operator doesn't have to leave the Agents page to trigger a sweep.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")
    db.add(m.Command(agent_id=agent.id, type=m.CommandType.rescan, payload=None))
    record(db, request, _manager(request, db), "agent.rescan",
           target=f"agent:{agent.id} {agent.name}")
    db.commit()
    _flash(
        request,
        f"Rescan queued for '{agent.name}' (picks up on next heartbeat ~60s).",
    )
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/poll-now")
def agent_poll_now(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue a full poll cycle for this agent (every approved printer it serves).

    Cuts the wait from the poll interval (default 5 min) down to the heartbeat
    interval (default 60s) so a tech can verify a fix landed without standing
    around.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")
    db.add(m.Command(agent_id=agent.id, type=m.CommandType.poll_now, payload=None))
    record(db, request, _manager(request, db), "agent.poll_now",
           target=f"agent:{agent.id} {agent.name}")
    db.commit()
    _flash(
        request,
        f"Poll-now queued for '{agent.name}' (picks up on next heartbeat ~60s).",
    )
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/delete")
def agent_delete(agent_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete agents.")
        return _redirect("/manage/agents")
    agent = db.get(m.Agent, agent_id)
    if agent:
        record(db, request, user, "agent.delete",
               target=f"agent:{agent.id} {agent.name}")
        db.delete(agent)
        db.commit()
        _flash(request, "Agent deleted.")
    return _redirect("/manage/agents")


def _build_v3_blob(
    *,
    user: str, security_level: str,
    auth_protocol: str, auth_password: str,
    priv_protocol: str, priv_password: str,
    context_name: str = "",
) -> Optional[dict]:
    """Build the snmp_v3 JSON blob from form fields. Returns None when no
    user was supplied (so toggling back to v1/v2c clears the blob)."""
    user = user.strip()
    if not user:
        return None
    blob = {
        "user": user,
        "security_level": security_level.strip() or "noAuthNoPriv",
    }
    from central.secrets import encrypt_value

    if auth_protocol.strip():
        blob["auth_protocol"] = auth_protocol.strip()
    if auth_password:
        # USM passwords are encrypted at rest; the agent-config endpoint
        # decrypts them on the way out to the (authenticated) agent.
        blob["auth_password"] = encrypt_value(auth_password)
    if priv_protocol.strip():
        blob["priv_protocol"] = priv_protocol.strip()
    if priv_password:
        blob["priv_password"] = encrypt_value(priv_password)
    if context_name.strip():
        blob["context_name"] = context_name.strip()
    return blob


@router.post("/agents/{agent_id}/subnets")
def subnet_add(
    agent_id: int, request: Request, cidr: str = Form(...),
    snmp_community: str = Form("public"), snmp_version: str = Form("2c"),
    bind_interface: str = Form(""),
    site_id: str = Form(""),
    snmp_v3_user: str = Form(""),
    snmp_v3_security_level: str = Form("noAuthNoPriv"),
    snmp_v3_auth_protocol: str = Form(""),
    snmp_v3_auth_password: str = Form(""),
    snmp_v3_priv_protocol: str = Form(""),
    snmp_v3_priv_password: str = Form(""),
    snmp_v3_context_name: str = Form(""),
    trusted: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a subnet and bind it to this agent.

    By default the subnet lives at the agent's home site. Pass an explicit
    ``site_id`` to create the subnet at a DIFFERENT site -- the multi-client
    pattern where one HQ agent collects for several client sites whose
    tunnels are terminated locally.

    SNMPv3 fields are picked up when ``snmp_version="3"``; for v1/v2c they're
    ignored. The agent receives the v3 blob through the existing /config
    endpoint and uses it to build a USM auth context.

    ``trusted`` opts this subnet into auto-approving discoveries. Absent (an
    unchecked box posts nothing) means false, which is both the form default
    and the safe one.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent and cidr.strip():
        effective_site_id = agent.site_id
        if site_id.strip():
            try:
                effective_site_id = int(site_id)
            except ValueError:
                pass
        is_trusted = bool(trusted.strip())
        db.add(m.Subnet(
            site_id=effective_site_id, agent_id=agent.id, cidr=cidr.strip(),
            snmp_community=snmp_community.strip() or "public",
            snmp_version=snmp_version,
            trusted=is_trusted,
            bind_interface=bind_interface.strip() or None,
            snmp_v3=_build_v3_blob(
                user=snmp_v3_user,
                security_level=snmp_v3_security_level,
                auth_protocol=snmp_v3_auth_protocol,
                auth_password=snmp_v3_auth_password,
                priv_protocol=snmp_v3_priv_protocol,
                priv_password=snmp_v3_priv_password,
                context_name=snmp_v3_context_name,
            ),
        ))
        record(db, request, _manager(request, db), "subnet.create",
               target=f"subnet:{cidr.strip()} agent:{agent.id}",
               detail=f"snmp v{snmp_version}"
                      + (" trusted=on (auto-approves discoveries)" if is_trusted else ""))
        db.commit()
        _flash(request, f"Subnet {cidr} assigned.")
    return _redirect("/manage/agents")


@router.post("/subnets/{subnet_id}/delete")
def subnet_delete(subnet_id: int, request: Request, db: Session = Depends(get_db)):
    if _manager(request, db) is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet:
        record(db, request, _manager(request, db), "subnet.delete",
               target=f"subnet:{subnet.id} {subnet.cidr}")
        db.delete(subnet)
        db.commit()
        _flash(request, "Subnet removed.")
    return _redirect("/manage/agents")


@router.post("/subnets/{subnet_id}")
def subnet_update(
    subnet_id: int, request: Request,
    label: str = Form(""),
    snmp_community: str = Form(""),
    snmp_version: str = Form(""),
    bind_interface: str = Form(""),
    agent_id: str = Form(""),
    snmp_v3_user: str = Form(""),
    snmp_v3_security_level: str = Form(""),
    snmp_v3_auth_protocol: str = Form(""),
    snmp_v3_auth_password: str = Form(""),
    snmp_v3_priv_protocol: str = Form(""),
    snmp_v3_priv_password: str = Form(""),
    snmp_v3_context_name: str = Form(""),
    snmp_v3_clear: str = Form(""),
    trusted: str = Form(""),
    trusted_present: str = Form(""),
    standby_agent_id: str = Form(""),
    standby_present: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit a subnet's friendly label, SNMP creds, source-bind address, and
    optionally reassign it to a different agent (potentially in a different
    site -- one agent at HQ can collect for several client sites whose
    tunnels are terminated there).

    Empty fields are ignored so the inline label-edit form on the agents page
    doesn't accidentally wipe other settings when an operator just renames.
    ``snmp_v3_clear=1`` is the explicit "blow away v3 creds" signal -- without
    it, omitting v3 form fields keeps the existing creds.

    ``trusted`` needs the same care for the opposite reason: an unchecked
    checkbox posts NOTHING, so reading it directly would make every form that
    omits the control -- the inline rename, for one -- silently turn
    auto-approval off. ``trusted_present`` is the "this form carried the
    checkbox" marker, so absence means "not my field" and only an actual
    unchecked box clears the flag.

    ``standby_agent_id`` needs the marker for the same reason and one more: an
    empty value is a real instruction here ("no standby"), which is
    indistinguishable from a form that never carried the field. Clearing a
    standby by accident would silently take a subnet's redundancy away, which
    nobody would notice until the day it was needed.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet:
        standby_note = _apply_standby(db, request, subnet, standby_agent_id, standby_present)
        subnet.label = label.strip() or None
        if snmp_community.strip():
            subnet.snmp_community = snmp_community.strip()
        if snmp_version.strip():
            subnet.snmp_version = snmp_version.strip()
        # bind_interface: empty string clears it (one explicit interface ->
        # OS default route); operator can intentionally remove it.
        subnet.bind_interface = bind_interface.strip() or None
        # SNMPv3: explicit clear vs. partial update. A partial v3 update where
        # only ``snmp_v3_user`` is present means the operator wants to rebuild
        # the blob from these form values. If no v3 fields at all are present
        # we leave the existing blob alone (so renaming a subnet doesn't blow
        # away creds).
        if snmp_v3_clear.strip():
            subnet.snmp_v3 = None
        elif snmp_v3_user.strip():
            subnet.snmp_v3 = _build_v3_blob(
                user=snmp_v3_user,
                security_level=snmp_v3_security_level or "noAuthNoPriv",
                auth_protocol=snmp_v3_auth_protocol,
                auth_password=snmp_v3_auth_password,
                priv_protocol=snmp_v3_priv_protocol,
                priv_password=snmp_v3_priv_password,
                context_name=snmp_v3_context_name,
            )
        # agent_id: optional reassignment. Accept any agent regardless of
        # site -- that's the whole point of the multi-client agent path.
        if agent_id.strip():
            try:
                new_agent = db.get(m.Agent, int(agent_id))
            except ValueError:
                new_agent = None
            if new_agent is not None:
                subnet.agent_id = new_agent.id
        # Only a form that actually carried the checkbox may change the trust
        # gate, and a flip is audited on its own line: this is the switch that
        # decides whether devices enter a tenant's fleet without a human.
        trust_note = ""
        if trusted_present.strip():
            new_trusted = bool(trusted.strip())
            if new_trusted != subnet.trusted:
                trust_note = (
                    " trusted=on (auto-approves discoveries)" if new_trusted
                    else " trusted=off"
                )
            subnet.trusted = new_trusted
        record(db, request, _manager(request, db), "subnet.update",
               target=f"subnet:{subnet.id} {subnet.cidr}",
               detail=(trust_note + " " + standby_note).strip())
        db.commit()
        _flash(request, f"Subnet {subnet.cidr} updated." + (
            f" {standby_note.strip()}" if standby_note else ""
        ))
    return _redirect("/manage/agents")


def _apply_standby(
    db: Session, request: Request, subnet: m.Subnet, raw: str, present: str
) -> str:
    """Set or clear this subnet's standby collector. Returns a note for the audit.

    Refusals rather than resolutions, because a standby that is wrong is worse
    than none: it hands a second agent this subnet's SNMP credentials and this
    site's fleet the moment it takes over.

    * **A subnet with no primary cannot have a standby.** There would be nothing
      to stand by for and nobody to seed the lease to, so ``admits()`` would
      refuse every reading for a subnet that looks configured.
    * **The standby cannot be the primary.** An agent standing by for itself is
      redundancy that reads as configured and covers nothing.
    * **An unknown agent id is ignored**, not treated as "clear" -- a typo must
      not silently remove redundancy.

    Turning redundancy ON seeds the lease to the agent that is already
    collecting, so there is no window in which nobody holds it and the primary's
    own readings are refused. Turning it OFF releases the lease rather than
    deleting it outright: the barrier that release leaves behind is what
    guarantees the (possibly still-sweeping) holder is finished before anything
    else touches the subnet.
    """
    from central import collector

    if not present.strip():
        return ""
    raw = (raw or "").strip()
    new_id: Optional[int] = None
    if raw:
        try:
            candidate = db.get(m.Agent, int(raw))
        except ValueError:
            candidate = None
        if candidate is None:
            return ""
        if subnet.agent_id is None:
            _flash(request, f"Assign a collector to {subnet.cidr} before adding a standby.")
            return ""
        if candidate.id == subnet.agent_id:
            _flash(request, f"{candidate.name} already collects {subnet.cidr}.")
            return ""
        new_id = candidate.id
    if new_id == subnet.standby_agent_id:
        return ""

    now = datetime.now(timezone.utc)
    ttl, _after, _auto = collector.lease_settings(load_settings(db))
    previous = subnet.standby_agent_id
    subnet.standby_agent_id = new_id
    if new_id is not None and previous is None:
        collector.seed_lease(db, subnet, now=now, ttl_seconds=ttl)
        return f"standby=agent:{new_id} (redundancy on; lease seeded to agent:{subnet.agent_id})"
    if new_id is None:
        holder = subnet.collector_agent_id
        if holder is not None:
            # Flush BEFORE expiring. The session is ``autoflush=False``, so the
            # pending ``standby_agent_id = None`` above is still only in memory
            # and ``expire`` would discard it -- the subnet would keep its
            # standby, silently, having reported that it was cleared.
            db.flush()
            collector.release_lease(db, subnet.id, holder_id=holder, now=now)
            db.expire(subnet)
        return f"standby cleared (was agent:{previous}); lease released"
    return f"standby=agent:{new_id} (was agent:{previous})"


@router.post("/subnets/{subnet_id}/handback")
def subnet_handback(subnet_id: int, request: Request, db: Session = Depends(get_db)):
    """Return a subnet to its primary collector -- the only hand-back there is.

    Failover is deliberately one-way: when a primary comes back it does NOT
    reclaim its subnets, because the standby is collecting correctly and the
    characteristic failure of a dying collector is flapping, which automatic
    hand-back would turn into an oscillation. So the decision is a human's, made
    once, when they can see that the primary is actually well.

    It is a **release**, not a reassignment, and that distinction is the whole
    safety of it. Pointing the lease straight at the primary would leave the
    standby's own deadline live -- it would keep sweeping while its replacement
    started, which is precisely the overlap this feature exists to prevent.
    Releasing clears the holder but keeps the expiry as a barrier, so the
    primary picks the subnet up on the first heartbeat after the standby must
    already have stopped. That costs a gap of at most one lease, and a gap is
    recoverable in a way that a double-counted meter is not.
    """
    from central import collector

    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet is None:
        return _redirect("/manage/agents")
    holder = subnet.collector_agent_id
    if holder is None or holder == subnet.agent_id:
        _flash(request, f"{subnet.cidr} is already collected by its primary.")
        return _redirect("/manage/agents")
    if subnet.agent_id is None:
        _flash(request, f"{subnet.cidr} has no primary agent to hand back to.")
        return _redirect("/manage/agents")
    collector.release_lease(db, subnet.id, holder_id=holder, now=datetime.now(timezone.utc))
    db.expire(subnet)
    record(db, request, user, "subnet.collector_handback",
           target=f"subnet:{subnet.id} {subnet.cidr}",
           detail=f"released from agent:{holder}; returns to agent:{subnet.agent_id} "
                  f"once the released lease elapses")
    db.commit()
    _flash(
        request,
        f"{subnet.cidr} released from its standby. Its primary picks it up once the "
        f"current lease elapses — the gap is deliberate, it is what stops both "
        f"agents collecting at once.",
    )
    return _redirect("/manage/agents")


# --------------------------------------------------------------------------- #
# Maintenance schedules (admin/tech) + service-log entries
# --------------------------------------------------------------------------- #
def _parse_date(raw: str):
    """YYYY-MM-DD -> tz-aware datetime, or None. The form's <input type='date'>
    posts in that shape; tolerated naive bare-date strings just as well."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    if not raw or not raw.strip():
        return None
    try:
        d = _dt.strptime(raw.strip(), "%Y-%m-%d")
        return d.replace(tzinfo=_tz.utc)
    except ValueError:
        return None


@router.get("/maintenance", response_class=HTMLResponse)
def maintenance_home(request: Request, db: Session = Depends(get_db)):
    """All schedules (per-printer or model-wide) + recent service-log entries.

    Drives the operator side of the worker's maintenance-due alert
    pipeline: rolling next_due forward (by logging a service entry, or
    editing inline) auto-resolves the corresponding alert on the next
    worker cycle.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    schedules = list(
        db.scalars(
            select(m.MaintenanceSchedule).order_by(
                m.MaintenanceSchedule.next_due.asc().nulls_last(),
                m.MaintenanceSchedule.id.desc(),
            )
        )
    )
    printers = list(db.scalars(
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.site_id, m.Printer.ip)
    ))
    records = list(
        db.scalars(
            select(m.MaintenanceRecord)
            .order_by(m.MaintenanceRecord.performed_at.desc())
            .limit(50)
        )
    )
    printers_by_id = {p.id: p for p in db.scalars(select(m.Printer))}
    return _tpl(
        request, "maintenance.html", db,
        user=user, schedules=schedules, printers=printers,
        records=records, printers_by_id=printers_by_id,
        types=[t.value for t in m.MaintenanceType],
        component_types=list(m.MaintenanceSchedule.COMPONENT_TYPES),
        flash=_pop_flash(request),
    )


@router.post("/maintenance/schedules")
def schedule_create(
    request: Request,
    name: str = Form(...),
    printer_id: str = Form(""),
    model: str = Form(""),
    interval_days: str = Form(""),
    page_threshold: str = Form(""),
    component_type: str = Form(""),
    life_threshold: str = Form(""),
    next_due: str = Form(""),
    db: Session = Depends(get_db),
):
    """Either printer_id or model identifies the scope; both empty -> a
    model-wide schedule that matches every printer with that model. interval
    OR page threshold drives the worker's due-check (the worker considers
    a schedule due when next_due <= now AND page_count >= threshold). A
    component_type + life_threshold triggers when the matching component-life
    Supply row (fuser / drum / belt / laser / PF kit) drops to/below that %."""
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    name = name.strip()
    if not name:
        _flash(request, "Schedule name is required.")
        return _redirect("/manage/maintenance")
    pid: Optional[int] = None
    if printer_id.strip():
        try:
            pid = int(printer_id)
        except ValueError:
            pid = None
    try:
        interval = int(interval_days) if interval_days.strip() else None
    except ValueError:
        interval = None
    try:
        threshold = int(page_threshold) if page_threshold.strip() else None
    except ValueError:
        threshold = None
    ctype = component_type.strip() or None
    if ctype is not None and ctype not in m.MaintenanceSchedule.COMPONENT_TYPES:
        ctype = None
    try:
        life = float(life_threshold) if life_threshold.strip() else None
    except ValueError:
        life = None
    # Component trigger only fires when both halves are present.
    if ctype is None or life is None:
        ctype = life = None
    sched = m.MaintenanceSchedule(
        name=name, printer_id=pid,
        model=model.strip() or None,
        interval_days=interval, page_threshold=threshold,
        component_type=ctype, life_threshold=life,
        next_due=_parse_date(next_due),
    )
    db.add(sched)
    record(db, request, actor, "maintenance_schedule.create",
           target=f"sched:{name}",
           detail=f"printer:{pid or '-'} model:{model.strip() or '-'} "
                  f"every:{interval or '-'}d threshold:{threshold or '-'} "
                  f"component:{ctype or '-'}@{life if life is not None else '-'}%")
    db.commit()
    _flash(request, f"Schedule '{name}' added.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/schedules/{sched_id}")
def schedule_update(
    sched_id: int, request: Request,
    name: str = Form(""),
    interval_days: str = Form(""),
    page_threshold: str = Form(""),
    component_type: str = Form(""),
    life_threshold: str = Form(""),
    next_due: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    sched = db.get(m.MaintenanceSchedule, sched_id)
    if sched is None:
        return _redirect("/manage/maintenance")
    if name.strip():
        sched.name = name.strip()
    try:
        sched.interval_days = int(interval_days) if interval_days.strip() else None
    except ValueError:
        pass
    try:
        sched.page_threshold = int(page_threshold) if page_threshold.strip() else None
    except ValueError:
        pass
    ctype = component_type.strip() or None
    if ctype is not None and ctype not in m.MaintenanceSchedule.COMPONENT_TYPES:
        ctype = None
    try:
        life = float(life_threshold) if life_threshold.strip() else None
    except ValueError:
        life = None
    if ctype is None or life is None:
        ctype = life = None
    sched.component_type = ctype
    sched.life_threshold = life
    parsed = _parse_date(next_due)
    if parsed is not None or next_due == "":
        sched.next_due = parsed
    record(db, request, actor, "maintenance_schedule.update",
           target=f"sched:{sched.id} {sched.name}")
    db.commit()
    _flash(request, f"Schedule '{sched.name}' updated.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/schedules/{sched_id}/delete")
def schedule_delete(sched_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    sched = db.get(m.MaintenanceSchedule, sched_id)
    if sched is not None:
        record(db, request, actor, "maintenance_schedule.delete",
               target=f"sched:{sched.id} {sched.name}")
        db.delete(sched)
        db.commit()
        _flash(request, f"Schedule '{sched.name}' removed.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/schedules/{sched_id}/log")
def schedule_log_service(
    sched_id: int, request: Request,
    performed_by: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Operator clicked 'Mark serviced': record a MaintenanceRecord and roll
    next_due forward by interval_days (when set). The worker's reconcile pass
    will see next_due > now on the next cycle and auto-resolve the
    maintenance-due alert."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    sched = db.get(m.MaintenanceSchedule, sched_id)
    if sched is None:
        return _redirect("/manage/maintenance")
    now = _dt.now(_tz.utc)
    next_due = (
        now + _td(days=sched.interval_days) if sched.interval_days else None
    )
    rec = m.MaintenanceRecord(
        printer_id=sched.printer_id,
        type=m.MaintenanceType.scheduled,
        performed_by=performed_by.strip() or actor.username,
        performed_at=now,
        notes=(notes.strip() or sched.name) + f" (schedule #{sched.id})",
        next_due=next_due,
    )
    db.add(rec)
    if next_due is not None:
        sched.next_due = next_due
    record(db, request, actor, "maintenance.log",
           target=f"sched:{sched.id} {sched.name}",
           detail=f"by:{performed_by.strip() or actor.username}")
    db.commit()
    _flash(request, f"Service logged for '{sched.name}'.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/records/{rec_id}/delete")
def record_delete(rec_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _manager(request, db)
    if actor is None or actor.role != m.UserRole.admin:
        _flash(request, "Only admins can remove service records.")
        return _redirect("/manage/maintenance")
    rec = db.get(m.MaintenanceRecord, rec_id)
    if rec is not None:
        record(db, request, actor, "maintenance.record_delete",
               target=f"record:{rec.id} printer:{rec.printer_id}")
        db.delete(rec)
        db.commit()
        _flash(request, "Service record removed.")
    return _redirect("/manage/maintenance")


# --------------------------------------------------------------------------- #
# Users (admin only)
# --------------------------------------------------------------------------- #
def _coerce_role(raw: str) -> m.UserRole:
    try:
        return m.UserRole(raw)
    except ValueError:
        return m.UserRole.tech



@router.get("/users", response_class=HTMLResponse)
def users_home(request: Request, db: Session = Depends(get_db)):
    if _admin(request, db) is None:
        return _redirect("/login" if _user(request, db) is None else "/")
    users = list(db.scalars(select(m.User).order_by(m.User.username)))
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    return _tpl(
        request, "manage_users.html", db,
        user=_admin(request, db), users=users, clients=clients,
        roles=[r.value for r in m.UserRole],
        flash=_pop_flash(request),
    )


# --------------------------------------------------------------------------- #
# Audit trail (admin only)
# --------------------------------------------------------------------------- #
@router.get("/audit", response_class=HTMLResponse)
def audit_home(request: Request, q: str = "", db: Session = Depends(get_db)):
    """Latest audit rows, newest first. ``?q=`` filters by substring across
    action / target / username -- enough for 'what did tech2 touch last week'
    without building a query designer."""
    admin = _admin(request, db)
    if admin is None:
        return _redirect("/login" if _user(request, db) is None else "/")
    stmt = select(m.AuditLog).order_by(m.AuditLog.ts.desc()).limit(200)
    if q.strip():
        needle = f"%{q.strip()}%"
        stmt = (
            select(m.AuditLog)
            .where(
                m.AuditLog.action.ilike(needle)
                | m.AuditLog.target.ilike(needle)
                | m.AuditLog.username.ilike(needle)
            )
            .order_by(m.AuditLog.ts.desc())
            .limit(200)
        )
    rows = list(db.scalars(stmt))
    return _tpl(
        request, "audit.html", db,
        user=admin, rows=rows, q=q.strip(),
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit(user_id: int, request: Request, db: Session = Depends(get_db)):
    if _admin(request, db) is None:
        return _redirect("/login" if _user(request, db) is None else "/")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    return _tpl(
        request, "user_form.html", db,
        user=_admin(request, db), target=target, clients=clients,
        roles=[r.value for r in m.UserRole],
    )


@router.post("/users")
def user_create(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    role: str = Form("tech"),
    client_id: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    if _admin(request, db) is None:
        return _redirect("/login")
    username = username.strip()
    if not username:
        _flash(request, "Username is required.")
        return _redirect("/manage/users")
    if db.scalar(select(m.User).where(m.User.username == username)) is not None:
        _flash(request, f"Username '{username}' is already taken.")
        return _redirect("/manage/users")
    role_enum = _coerce_role(role)
    # client_readonly users MUST be pinned to a client -- otherwise they'd see
    # every client (defeating the role's purpose). Other roles ignore client_id.
    pinned_client_id: Optional[int] = None
    if client_id.strip():
        try:
            pinned_client_id = int(client_id)
        except ValueError:
            pinned_client_id = None
    if role_enum == m.UserRole.client_readonly and pinned_client_id is None:
        _flash(request, "client_readonly users must be assigned to a client.")
        return _redirect("/manage/users")
    new_user = m.User(
        username=username,
        email=email.strip() or None,
        password_hash=hash_password(password) if password else None,
        role=role_enum,
        client_id=pinned_client_id if role_enum == m.UserRole.client_readonly else None,
        auth_provider="local" if password else "oidc",
    )
    db.add(new_user)
    record(db, request, _admin(request, db), "user.create",
           target=f"user:{username}", detail=f"role={role_enum.value}")
    db.commit()
    _flash(request, f"User '{username}' created.")
    return _redirect("/manage/users")


@router.post("/users/{user_id}")
def user_update(
    user_id: int, request: Request,
    email: str = Form(""),
    role: str = Form("tech"),
    client_id: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    new_role = _coerce_role(role)
    # Last-admin guard: refuse to demote the only remaining admin (lockout).
    if (target.role == m.UserRole.admin and new_role != m.UserRole.admin
            and db.query(m.User).filter_by(role=m.UserRole.admin).count() <= 1):
        _flash(request, "Refused: this is the only admin. Promote another user first.")
        return _redirect(f"/manage/users/{user_id}/edit")
    pinned_client_id: Optional[int] = None
    if client_id.strip():
        try:
            pinned_client_id = int(client_id)
        except ValueError:
            pinned_client_id = None
    if new_role == m.UserRole.client_readonly and pinned_client_id is None:
        _flash(request, "client_readonly users must be assigned to a client.")
        return _redirect(f"/manage/users/{user_id}/edit")
    target.email = email.strip() or None
    target.role = new_role
    target.client_id = pinned_client_id if new_role == m.UserRole.client_readonly else None
    record(db, request, actor, "user.update",
           target=f"user:{target.username}", detail=f"role={new_role.value}")
    db.commit()
    _flash(request, f"User '{target.username}' updated.")
    return _redirect("/manage/users")


@router.post("/users/{user_id}/reset-password")
def user_reset_password(
    user_id: int, request: Request,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if _admin(request, db) is None:
        return _redirect("/login")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    if len(new_password) < 8:
        _flash(request, "Password must be at least 8 characters.")
        return _redirect(f"/manage/users/{user_id}/edit")
    target.password_hash = hash_password(new_password)
    target.auth_provider = "local"  # they can now sign in locally
    record(db, request, _admin(request, db), "user.reset_password",
           target=f"user:{target.username}")
    db.commit()
    _flash(request, f"Password reset for '{target.username}'.")
    return _redirect("/manage/users")


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    if actor.id == target.id:
        _flash(request, "Refused: you can't delete the account you're logged in as.")
        return _redirect("/manage/users")
    if (target.role == m.UserRole.admin
            and db.query(m.User).filter_by(role=m.UserRole.admin).count() <= 1):
        _flash(request, "Refused: this is the only admin.")
        return _redirect("/manage/users")
    record(db, request, actor, "user.delete",
           target=f"user:{target.username}", detail=f"role={target.role.value}")
    db.delete(target)
    db.commit()
    _flash(request, f"User '{target.username}' deleted.")
    return _redirect("/manage/users")


# --------------------------------------------------------------------------- #
# Suppression windows — quiet hours + planned maintenance.
#
# Recurring windows are stored as LOCAL minutes-from-midnight, so the form takes
# HH:MM and converts. Everything here is audit-logged: silencing alerts is
# exactly the kind of change an operator needs to be able to account for later
# ("why didn't we hear about Saturday?"), and the audit row is where that
# question gets answered.
# --------------------------------------------------------------------------- #
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_hhmm(raw: str):
    """'HH:MM' -> minutes from midnight, or None. Accepts '24:00' as end-of-day."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    try:
        hh, _, mm = text.partition(":")
        hours, minutes = int(hh), int(mm or 0)
    except ValueError:
        return None
    if not (0 <= hours <= 24) or not (0 <= minutes < 60):
        return None
    total = hours * 60 + minutes
    return total if 0 <= total <= 24 * 60 else None


def _fmt_hhmm(minutes) -> str:
    if minutes is None:
        return ""
    minutes = int(minutes)
    return "%02d:%02d" % (minutes // 60 % 25, minutes % 60)


def _parse_local_dt(raw: str):
    """'YYYY-MM-DDTHH:MM' from <input type=datetime-local> -> tz-aware UTC.

    Read as UTC deliberately: a maintenance window is a single absolute range,
    and the form labels the fields UTC rather than guessing which client's clock
    an admin meant while scheduling across several of them.
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    if not raw or not raw.strip():
        return None
    text = raw.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.strptime(text, fmt).replace(tzinfo=_tz.utc)
        except ValueError:
            continue
    return None


def _parse_weekdays(raw_list) -> Optional[list]:
    """Form checkbox values -> sorted [0..6], or None for 'every day'."""
    out = set()
    for raw in raw_list or []:
        try:
            day = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            out.add(day)
    # All seven selected is the same policy as none, stored as None so the
    # evaluator takes its cheaper "every day" path.
    if not out or len(out) == 7:
        return None
    return sorted(out)


def _window_scope(scope: str, scope_id: str):
    """Validate scope/scope_id together; a scoped window needs a target id."""
    try:
        scope_enum = m.AlertScope(scope)
    except ValueError:
        scope_enum = m.AlertScope.global_
    if scope_enum == m.AlertScope.global_:
        return scope_enum, None
    try:
        target = int(scope_id)
    except (TypeError, ValueError):
        return None, None
    return scope_enum, target


@router.get("/suppression", response_class=HTMLResponse)
def suppression_home(request: Request, db: Session = Depends(get_db)):
    """Quiet hours + maintenance windows, with a live 'active now' indicator."""
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    windows = list(db.scalars(
        select(m.SuppressionWindow).order_by(
            m.SuppressionWindow.kind, m.SuppressionWindow.id.desc()
        )
    ))
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    sites = list(db.scalars(select(m.Site).order_by(m.Site.name)))
    printers = list(db.scalars(
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.ip)
    ))
    # "Is it on right now?" per window, evaluated against a representative
    # printer in its scope so the client timezone in play is the real one.
    now = datetime.now(timezone.utc)
    runtime = load_settings(db)
    active_ids = set()
    for window in windows:
        probe = _scope_probe_printer(db, window, printers)
        for hit, _ends in suppression.active_windows(
            db, probe, now, runtime=runtime, windows=[window]
        ):
            active_ids.add(hit.id)
    return _tpl(
        request, "suppression.html", db,
        user=user, windows=windows, clients=clients, sites=sites,
        printers=printers, active_ids=active_ids,
        weekday_labels=list(enumerate(_WEEKDAY_LABELS)),
        fmt_hhmm=_fmt_hhmm,
        default_tz=(runtime.get("alerts.default_timezone") or "UTC"),
        flash=_pop_flash(request),
    )


def _scope_probe_printer(db: Session, window, printers: list):
    """A printer inside ``window``'s scope, for evaluating it in the right zone.

    Global windows have no single client, so the first approved printer stands in
    -- the "active now" badge is informational, and picking any in-scope device
    gives the operator a truthful answer for at least one of them.
    """
    if window.scope == m.AlertScope.printer:
        return db.get(m.Printer, window.scope_id) if window.scope_id else None
    if window.scope == m.AlertScope.site:
        return next((p for p in printers if p.site_id == window.scope_id), None)
    if window.scope == m.AlertScope.client:
        return next((p for p in printers if p.client_id == window.scope_id), None)
    return printers[0] if printers else None


@router.post("/suppression")
def suppression_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form("quiet_hours"),
    action: str = Form(""),
    scope: str = Form("global"),
    scope_id: str = Form(""),
    min_severity_breakthrough: str = Form("critical"),
    start_time: str = Form(""),
    end_time: str = Form(""),
    weekdays: List[str] = Form(default=[]),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a window. Validation refuses a half-specified one rather than
    storing something that silently never matches -- a suppression window that
    quietly does nothing is worse than an error message, because the operator
    believes they are covered."""
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    name = name.strip()
    if not name:
        _flash(request, "Window name is required.")
        return _redirect("/manage/suppression")

    try:
        kind_enum = m.SuppressionKind(kind)
    except ValueError:
        kind_enum = m.SuppressionKind.quiet_hours
    # Default the action per kind: a digest of expected maintenance noise
    # defeats the point of declaring the window, so maintenance suppresses.
    if action:
        try:
            action_enum = m.SuppressionAction(action)
        except ValueError:
            action_enum = m.SuppressionAction.defer
    else:
        action_enum = (
            m.SuppressionAction.suppress
            if kind_enum == m.SuppressionKind.maintenance
            else m.SuppressionAction.defer
        )
    # "none" is a deliberate choice, not a parse failure: it means nothing breaks
    # through. Anything unrecognised still falls back to the safe default.
    allow_breakthrough = min_severity_breakthrough.strip().lower() != "none"
    try:
        floor = m.EventSeverity(min_severity_breakthrough)
    except ValueError:
        floor = m.EventSeverity.critical

    scope_enum, target = _window_scope(scope, scope_id)
    if scope_enum is None:
        _flash(request, "Pick a target for a client/site/printer-scoped window.")
        return _redirect("/manage/suppression")

    start = end = None
    days = None
    begins = finishes = None
    if kind_enum == m.SuppressionKind.quiet_hours:
        start = _parse_hhmm(start_time)
        end = _parse_hhmm(end_time)
        if start is None or end is None:
            _flash(request, "Quiet hours need a start and end time (HH:MM).")
            return _redirect("/manage/suppression")
        days = _parse_weekdays(weekdays)
    else:
        begins = _parse_local_dt(starts_at)
        finishes = _parse_local_dt(ends_at)
        if begins is None or finishes is None:
            _flash(request, "A maintenance window needs a start and end date/time.")
            return _redirect("/manage/suppression")
        if finishes <= begins:
            _flash(request, "The maintenance window must end after it starts.")
            return _redirect("/manage/suppression")

    window = m.SuppressionWindow(
        name=name, kind=kind_enum, action=action_enum,
        scope=scope_enum, scope_id=target,
        min_severity_breakthrough=floor,
        allow_breakthrough=allow_breakthrough,
        start_minute=start, end_minute=end, weekdays=days,
        starts_at=begins, ends_at=finishes,
        enabled=True,
    )
    db.add(window)
    record(db, request, actor, "suppression_window.create",
           target=f"window:{name}",
           detail=(f"kind={kind_enum.value} action={action_enum.value} "
                   f"scope={scope_enum.value}:{target or '-'} "
                   f"breakthrough={floor.value if allow_breakthrough else 'none'} "
                   f"local={_fmt_hhmm(start)}-{_fmt_hhmm(end)} "
                   f"days={days or 'all'} "
                   f"utc={begins.isoformat() if begins else '-'}"
                   f"..{finishes.isoformat() if finishes else '-'}"))
    db.commit()
    _flash(request, f"Window '{name}' added.")
    return _redirect("/manage/suppression")


@router.post("/suppression/{window_id}/toggle")
def suppression_toggle(window_id: int, request: Request, db: Session = Depends(get_db)):
    """Enable/disable without deleting -- the usual way a seasonal policy is parked."""
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    window = db.get(m.SuppressionWindow, window_id)
    if window is None:
        _flash(request, "That window no longer exists.")
        return _redirect("/manage/suppression")
    window.enabled = not window.enabled
    record(db, request, actor, "suppression_window.update",
           target=f"window:{window.name}",
           detail=f"enabled={window.enabled}")
    db.commit()
    _flash(request, f"Window '{window.name}' {'enabled' if window.enabled else 'disabled'}.")
    return _redirect("/manage/suppression")


@router.post("/suppression/{window_id}/delete")
def suppression_delete(window_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    window = db.get(m.SuppressionWindow, window_id)
    if window is None:
        _flash(request, "That window no longer exists.")
        return _redirect("/manage/suppression")
    record(db, request, actor, "suppression_window.delete",
           target=f"window:{window.name}",
           detail=f"kind={window.kind.value} scope={window.scope.value}")
    db.delete(window)
    db.commit()
    _flash(request, f"Window '{window.name}' deleted.")
    return _redirect("/manage/suppression")


# --------------------------------------------------------------------------- #
# Alert rules
#
# Rules existed from the beginning but had no operator surface at all: the four
# defaults came from central.seed and per-client ones from onboarding defaults,
# and after that the only way to change a threshold was SQL. That was tolerable
# while every condition type was a single number an installer could pick a
# sensible value for; occurrence_rate is not -- "ten jams a day" is three
# operator decisions (what counts, how many, over how long) and none of them has
# a defensible default.
# --------------------------------------------------------------------------- #
# What an operator may type into a rate rule's window. The evaluator clamps
# independently (worker.jobs.OCCURRENCE_MAX_WINDOW_MINUTES) -- this is the half
# that explains the refusal instead of silently narrowing the rule.
_MAX_RULE_WINDOW_MINUTES = 60 * 24 * 30

# Condition types an operator can create here: label, and what `threshold` means
# for each. ``predicted_depletion`` is deliberately absent -- it is raised by the
# forecast pass against alerts.reorder_lead_days, not by a rule, so offering it
# would create a row that never fires.
_CONDITION_LABELS = {
    m.AlertConditionType.supply_below: ("Supply below (%)", "percent"),
    m.AlertConditionType.error_severity: ("Printer error at/above severity", None),
    m.AlertConditionType.offline_minutes: ("Agent offline (minutes)", "minutes"),
    m.AlertConditionType.printer_offline: ("Printer offline (minutes)", "minutes"),
    m.AlertConditionType.occurrence_rate: ("Occurrence rate (N events in a window)", "count"),
    m.AlertConditionType.maintenance_due: ("Maintenance due", None),
}


def _rule_threshold_unit(rule: m.AlertRule) -> Optional[str]:
    labelled = _CONDITION_LABELS.get(rule.condition_type)
    return labelled[1] if labelled else None


def _fmt_rule_window(minutes) -> str:
    """Minutes as an operator says them (45m / 6h / 7d). Mirrors worker.jobs."""
    if not minutes:
        return ""
    minutes = int(minutes)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _parse_window_minutes(amount: str, unit: str) -> Optional[int]:
    """'12' + 'hours' -> 720. None when unparseable or out of range.

    Returned as None rather than clamped so the caller can refuse with a message.
    A window the operator did not choose is a rule firing on a period nobody
    picked, which is worse than being made to type it again.
    """
    try:
        value = int((amount or "").strip())
    except (TypeError, ValueError):
        return None
    per = {"minutes": 1, "hours": 60, "days": 1440}.get(unit, 60)
    minutes = value * per
    if minutes < 1 or minutes > _MAX_RULE_WINDOW_MINUTES:
        return None
    return minutes


@router.get("/alert-rules", response_class=HTMLResponse)
def alert_rules_home(request: Request, db: Session = Depends(get_db)):
    """List every alert rule, with the occurrence-rate ones fully spelled out."""
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    rules = list(db.scalars(
        select(m.AlertRule).order_by(m.AlertRule.condition_type, m.AlertRule.id)
    ))
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    sites = list(db.scalars(select(m.Site).order_by(m.Site.name)))
    printers = list(db.scalars(
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.ip)
    ))
    runtime = load_settings(db)
    return _tpl(
        request, "alert_rules.html", db,
        user=user, rules=rules, clients=clients, sites=sites, printers=printers,
        condition_labels={k.value: v[0] for k, v in _CONDITION_LABELS.items()},
        threshold_unit=_rule_threshold_unit,
        fmt_window=_fmt_rule_window,
        clear_margin_pct=float(runtime.get("alerts.occurrence_clear_margin_pct", 0) or 0),
        flap_cooldown_min=int(runtime.get("alerts.renotify_cooldown_min", 0) or 0),
        max_window_days=_MAX_RULE_WINDOW_MINUTES // 1440,
        flash=_pop_flash(request),
    )


@router.post("/alert-rules")
def alert_rules_create(
    request: Request,
    name: str = Form(...),
    condition_type: str = Form("supply_below"),
    scope: str = Form("global"),
    scope_id: str = Form(""),
    severity: str = Form("warning"),
    threshold: str = Form(""),
    window_amount: str = Form(""),
    window_unit: str = Form("hours"),
    match_code: str = Form(""),
    match_min_severity: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a rule. A half-specified one is refused, never stored.

    Same rule as the suppression form next door: an alert rule that quietly
    never matches is worse than an error message, because the operator believes
    they are covered. For occurrence_rate that means both the count and the
    window are mandatory -- neither has a defensible default.
    """
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    name = name.strip()
    if not name:
        _flash(request, "Rule name is required.")
        return _redirect("/manage/alert-rules")

    try:
        condition = m.AlertConditionType(condition_type)
    except ValueError:
        condition = None
    if condition not in _CONDITION_LABELS:
        _flash(request, "That condition type cannot be created here.")
        return _redirect("/manage/alert-rules")

    try:
        sev = m.EventSeverity(severity)
    except ValueError:
        sev = m.EventSeverity.warning

    scope_enum, target = _window_scope(scope, scope_id)
    if scope_enum is None:
        _flash(request, "Pick a target for a client/site/printer-scoped rule.")
        return _redirect("/manage/alert-rules")

    limit: Optional[float] = None
    raw_threshold = (threshold or "").strip()
    if raw_threshold:
        try:
            limit = float(raw_threshold)
        except ValueError:
            _flash(request, "The threshold must be a number.")
            return _redirect("/manage/alert-rules")

    window = None
    code = None
    floor = None
    if condition == m.AlertConditionType.occurrence_rate:
        if limit is None or limit < 1:
            _flash(request, "An occurrence-rate rule needs a count of 1 or more.")
            return _redirect("/manage/alert-rules")
        window = _parse_window_minutes(window_amount, window_unit)
        if window is None:
            _flash(request,
                   "An occurrence-rate rule needs a window between 1 minute and "
                   f"{_MAX_RULE_WINDOW_MINUTES // 1440} days.")
            return _redirect("/manage/alert-rules")
        # Free text from an operator. It reaches SQL only as a bound LIKE
        # parameter with the metacharacters escaped (worker.jobs._like_contains)
        # and the dashboard renders it through Jinja autoescaping, so the only
        # reason to bound the length here is to keep an accidental paste out of
        # a column that would truncate it silently.
        code = (match_code or "").strip()[:80] or None
        if match_min_severity:
            try:
                floor = m.EventSeverity(match_min_severity)
            except ValueError:
                floor = None
    elif condition in (
        m.AlertConditionType.supply_below,
        m.AlertConditionType.offline_minutes,
        m.AlertConditionType.printer_offline,
    ):
        if limit is None:
            _flash(request, "That condition type needs a threshold.")
            return _redirect("/manage/alert-rules")

    rule = m.AlertRule(
        name=name,
        scope=scope_enum,
        scope_id=target,
        condition_type=condition,
        threshold=limit,
        severity=sev,
        window_minutes=window,
        match_code=code,
        match_min_severity=floor,
        enabled=True,
    )
    db.add(rule)
    record(db, request, actor, "alert_rule.create",
           target=f"rule:{name}",
           detail=(f"condition={condition.value} scope={scope_enum.value}:{target or '-'} "
                   f"threshold={limit if limit is not None else '-'} "
                   f"severity={sev.value} window={window or '-'}min "
                   f"match_code={code or '*'} "
                   f"match_min_severity={floor.value if floor else '-'}"))
    db.commit()
    _flash(request, f"Rule '{name}' added.")
    return _redirect("/manage/alert-rules")


@router.post("/alert-rules/{rule_id}/toggle")
def alert_rules_toggle(rule_id: int, request: Request, db: Session = Depends(get_db)):
    """Enable/disable without deleting.

    A disabled rule stops contributing keys to the evaluator's active set, so
    its open alerts auto-resolve on the next cycle rather than being stranded --
    that is _resolve_stale's existing behaviour for an orphaned key, and it is
    what makes disabling safe to offer beside deleting.
    """
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    rule = db.get(m.AlertRule, rule_id)
    if rule is None:
        _flash(request, "That rule no longer exists.")
        return _redirect("/manage/alert-rules")
    rule.enabled = not rule.enabled
    record(db, request, actor, "alert_rule.update",
           target=f"rule:{rule.name}", detail=f"enabled={rule.enabled}")
    db.commit()
    _flash(request, f"Rule '{rule.name}' {'enabled' if rule.enabled else 'disabled'}.")
    return _redirect("/manage/alert-rules")


@router.post("/alert-rules/{rule_id}/delete")
def alert_rules_delete(rule_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    rule = db.get(m.AlertRule, rule_id)
    if rule is None:
        _flash(request, "That rule no longer exists.")
        return _redirect("/manage/alert-rules")
    record(db, request, actor, "alert_rule.delete",
           target=f"rule:{rule.name}",
           detail=f"condition={rule.condition_type.value} scope={rule.scope.value}")
    db.delete(rule)
    db.commit()
    _flash(request, f"Rule '{rule.name}' deleted.")
    return _redirect("/manage/alert-rules")
