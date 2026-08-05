"""CSRF protection, tested against a client that is NOT helped by conftest.

``tests/conftest.py`` patches ``TestClient`` so the suite's 345 existing POSTs
carry a real token (fetched over real HTTP from the real login page). That is
what makes the rest of the suite runnable, and it is also why this file cannot
use ``TestClient``: with every request transparently authenticated there is no
way to express "arrives without a token", which is the only interesting case.

So everything here goes through ``_raw()``, a client whose ``request`` is the
pre-patch one conftest stashed: nothing added, exactly the bytes a browser -- or
an attacker's page -- would send.

The invariants, in the order they matter:

* an unsafe request carrying the session cookie and no valid token is refused;
* the refusal is *legible* -- a page for a form post, a message for htmx, an
  audit row for the operator, and never the token itself;
* the bearer-authenticated agent/workstation/SCIM surface is untouched, because
  a request with no cookie has no ambient credential to abuse;
* the session-authenticated JSON API under the same ``/api/v1`` prefix *is*
  covered, which is the case a path allowlist would have got backwards;
* ``/admin/backup/download`` discloses nothing to a GET;
* every rendered POST form actually carries the field, and no GET form does.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import csrf as csrf_mod
from central import models as m
from central.main import app
from central.security import generate_api_key, hash_api_key, hash_password

TOKEN_INPUT = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


class _RawClient(TestClient):
    """A client with no CSRF help of any kind. Keeps cookies, like a browser.

    ``request_without_csrf`` is the pre-patch ``TestClient.request``, stashed by
    conftest for exactly this. Overriding ``request`` (rather than calling it at
    each site) means ``.get``/``.post``/``.put`` all route through the unhelped
    path, so nothing in this file can accidentally be handed a token.
    """

    def request(self, *args, **kwargs):
        return TestClient.request_without_csrf(self, *args, **kwargs)


def _raw() -> _RawClient:
    return _RawClient(app, follow_redirects=False)


def _token(client: TestClient) -> str:
    page = client.get("/login")
    found = TOKEN_INPUT.search(page.text)
    assert found, "login page carried no csrf_token field"
    return found.group(1)


def _admin(db, username="csrfadmin", role=m.UserRole.admin) -> _RawClient:
    db.add(m.User(username=username, password_hash=hash_password("pw12345678"), role=role))
    db.commit()
    client = _raw()
    resp = client.post(
        "/login",
        data={"username": username, "password": "pw12345678", "csrf_token": _token(client)},
    )
    assert resp.status_code == 303, resp.text[:300]
    return client


# --------------------------------------------------------------------------- #
# The mechanism
# --------------------------------------------------------------------------- #
def test_form_post_without_a_token_is_refused(db):
    client = _admin(db)
    resp = client.post("/manage/clients", data={"name": "Acme"})
    assert resp.status_code == 403
    assert db.scalar(select(m.Client)) is None, "the refused request still wrote"


def test_form_post_with_the_hidden_field_is_accepted(db):
    client = _admin(db)
    resp = client.post(
        "/manage/clients", data={"name": "Acme", "csrf_token": _token(client)}
    )
    assert resp.status_code == 303
    assert db.scalar(select(m.Client)).name == "Acme"


def test_htmx_header_is_accepted(db):
    """The other supply channel: an hx-post button has no body to carry a field."""
    client = _admin(db)
    resp = client.post(
        "/manage/clients",
        data={"name": "Acme"},
        headers={"X-CSRF-Token": _token(client)},
    )
    assert resp.status_code == 303
    assert db.scalar(select(m.Client)).name == "Acme"


def test_a_wrong_token_is_refused(db):
    client = _admin(db)
    _token(client)
    resp = client.post("/manage/clients", data={"name": "Acme", "csrf_token": "x" * 43})
    assert resp.status_code == 403
    assert db.scalar(select(m.Client)) is None


def test_another_sessions_token_is_refused(db):
    """The token is bound to a session, not merely to the deployment.

    A shared secret would be readable by anyone who can reach the login page --
    which is everyone -- and would then unlock every operator's session.
    """
    victim = _admin(db, "victim")
    attacker = _admin(db, "attacker", role=m.UserRole.tech)
    resp = victim.post(
        "/manage/clients",
        data={"name": "Acme"},
        headers={"X-CSRF-Token": _token(attacker)},
    )
    assert resp.status_code == 403
    assert db.scalar(select(m.Client)) is None


def test_token_rotates_on_login(db):
    """A token issued before authentication does not survive it."""
    db.add(m.User(username="rot", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin))
    db.commit()
    client = _raw()
    before = _token(client)
    assert client.post(
        "/login", data={"username": "rot", "password": "pw12345678", "csrf_token": before}
    ).status_code == 303
    after = _token(client)
    assert after != before
    # And the pre-login one is now dead, not merely superseded.
    assert client.post(
        "/manage/clients", data={"name": "Acme"}, headers={"X-CSRF-Token": before}
    ).status_code == 403


@pytest.mark.parametrize("page", ["/", "/manage", "/manage/agents", "/login"])
def test_safe_methods_are_never_challenged(db, page):
    client = _admin(db)
    assert client.get(page).status_code in (200, 303)


def test_login_is_challenged_even_with_no_session(db):
    """Login CSRF lands an operator inside an attacker's session.

    A browser that has never visited us has no session cookie, so the general
    "only when a session is present" trigger would skip this one -- hence the
    explicit exception in central/csrf.py.
    """
    db.add(m.User(username="fresh", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin))
    db.commit()
    client = _raw()
    resp = client.post("/login", data={"username": "fresh", "password": "pw12345678"})
    assert resp.status_code == 403
    assert not client.cookies.get("session"), "a refused login still opened a session"


# --------------------------------------------------------------------------- #
# Legibility -- a silent 403 is the failure this change had to avoid
# --------------------------------------------------------------------------- #
def test_a_refused_form_post_gets_an_explaining_page(db):
    client = _admin(db)
    resp = client.post("/manage/clients", data={"name": "Acme"})
    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "not accepted" in body.lower()
    assert "Nothing was changed" in body
    # Reachable from the dead end, not a cul-de-sac.
    assert 'href="/"' in body


def test_a_refused_htmx_request_gets_a_message_htmx_can_show(db):
    """htmx does not swap a non-2xx body, so the page must be told, not shown."""
    client = _admin(db)
    resp = client.post(
        "/manage/clients", data={"name": "Acme"}, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["HX-Reswap"] == "none"
    assert "security token" in resp.text
    assert "<" not in resp.text, "an HTML body would be shown as literal text"


def test_a_refusal_is_audited_without_the_token(db):
    client = _admin(db)
    client.post("/manage/clients", data={"name": "Acme"})
    client.post("/manage/clients", data={"name": "Acme", "csrf_token": "z" * 43})
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "csrf.rejected")
        .order_by(m.AuditLog.id)
    ))
    assert [r.detail for r in rows] == ["missing", "mismatch"]
    assert all(r.target == "POST /manage/clients" for r in rows)
    blob = " ".join(f"{r.target} {r.detail}" for r in rows)
    assert "z" * 43 not in blob, "the supplied token leaked into the audit trail"


# --------------------------------------------------------------------------- #
# Scope: which routes need this, and which must not have it
# --------------------------------------------------------------------------- #
def _agent(db) -> "tuple[int, str]":
    client_row = m.Client(name="AgentCo")
    db.add(client_row)
    db.flush()
    site = m.Site(client_id=client_row.id, name="HQ")
    db.add(site)
    db.flush()
    key = generate_api_key()
    agent = m.Agent(site_id=site.id, name="a1", api_key_hash=hash_api_key(key))
    db.add(agent)
    db.commit()
    return agent.id, key


def test_agent_ingest_is_untouched(db):
    """Bearer-authenticated, cookieless: nothing for a browser to forge."""
    agent_id, key = _agent(db)
    client = _raw()
    resp = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"version": "0.1.0"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text[:300]


def test_agent_self_registration_is_untouched(db):
    """Claim-code redemption carries no cookie, so it keeps its own 401."""
    resp = _raw().post(
        "/api/v1/agents/register", json={"claim_code": "nope", "hostname": "h"}
    )
    assert resp.status_code == 401


def test_workstation_enrollment_is_untouched(db):
    resp = _raw().post(
        "/api/v1/workstations/enroll",
        json={"enroll_key": "nope", "machine_uid": "u", "name": "PC"},
    )
    assert resp.status_code == 401


def test_scim_is_untouched(db):
    """SCIM authenticates with the IdP's bearer token and never sends a cookie.

    Disabled by default, so 404 is the whole surface answering -- which is
    exactly what it did before this change, and the point: it did not become a
    403.
    """
    resp = _raw().post(
        "/scim/v2/Users",
        json={"userName": "x"},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 404


def test_session_authed_json_api_is_covered(db):
    """The case a path allowlist would have got backwards.

    ``central/api/management.py`` mounts at ``/api/v1`` -- the same prefix as the
    agent ingest surface -- but authenticates with ``require_staff``, i.e. the
    session cookie. This route takes no body at all, so before this change a
    plain cross-site ``<form action=…/approve>`` submitted by a logged-in
    operator's browser worked.
    """
    client = _admin(db)
    client_row = m.Client(name="Tenant")
    db.add(client_row)
    db.flush()
    site = m.Site(client_id=client_row.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(client_id=client_row.id, site_id=site.id, ip="10.0.0.9",
                        discovery_state=m.DiscoveryState.pending)
    db.add(printer)
    db.commit()
    pid = printer.id

    assert client.post(f"/api/v1/printers/{pid}/approve").status_code == 403
    db.expire_all()
    assert db.get(m.Printer, pid).discovery_state == m.DiscoveryState.pending

    ok = client.post(
        f"/api/v1/printers/{pid}/approve", headers={"X-CSRF-Token": _token(client)}
    )
    assert ok.status_code == 200
    db.expire_all()
    assert db.get(m.Printer, pid).discovery_state == m.DiscoveryState.approved


# --------------------------------------------------------------------------- #
# The backup download's shape
# --------------------------------------------------------------------------- #
def test_backup_download_get_discloses_nothing(db):
    """The original defect: a GET that streamed the whole database.

    SameSite=lax permits top-level GET navigation, so a link or a redirect from
    any page a logged-in admin visited was enough to fire this.
    """
    client = _admin(db)
    resp = client.get("/admin/backup/download")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/backup"
    assert resp.content == b""
    assert "attachment" not in resp.headers.get("content-disposition", "")


def test_backup_download_post_needs_a_token(db, monkeypatch, tmp_path):
    import central.dashboard.backup_routes as br

    src = tmp_path / "pn.db"
    src.write_bytes(b"SQLITE-FAKE-CONTENTS")
    monkeypatch.setattr(br, "_is_sqlite", lambda: True)
    monkeypatch.setattr(br, "_sqlite_path", lambda: src)
    client = _admin(db)

    refused = client.post("/admin/backup/download")
    assert refused.status_code == 403
    assert b"SQLITE-FAKE-CONTENTS" not in refused.content

    ok = client.post("/admin/backup/download", data={"csrf_token": _token(client)})
    assert ok.status_code == 200
    assert ok.content == b"SQLITE-FAKE-CONTENTS"
    assert "attachment" in ok.headers["content-disposition"]


def test_restore_still_needs_the_typed_confirmation(db, monkeypatch, tmp_path):
    """The token is an addition to that gate, never a replacement for it."""
    import central.dashboard.backup_routes as br

    dest = tmp_path / "pn.db"
    dest.write_bytes(b"OLD")
    monkeypatch.setattr(br, "_is_sqlite", lambda: True)
    monkeypatch.setattr(br, "_sqlite_path", lambda: dest)
    client = _admin(db)

    # Valid token, wrong confirmation -> bounced, database untouched.
    resp = client.post(
        "/admin/backup/restore",
        data={"confirm": "yes please", "csrf_token": _token(client)},
        files={"backup_file": ("new.sqlite", b"NEW", "application/octet-stream")},
    )
    assert resp.status_code == 303
    assert dest.read_bytes() == b"OLD"

    # Right confirmation, no token -> refused, database untouched.
    resp = client.post(
        "/admin/backup/restore",
        data={"confirm": "RESTORE"},
        files={"backup_file": ("new.sqlite", b"NEW", "application/octet-stream")},
    )
    assert resp.status_code == 403
    assert dest.read_bytes() == b"OLD"

    # Both -> it runs. Also proves the token check does not eat a multipart
    # body: the uploaded file still reaches the handler.
    resp = client.post(
        "/admin/backup/restore",
        data={"confirm": "RESTORE", "csrf_token": _token(client)},
        files={"backup_file": ("new.sqlite", b"NEW", "application/octet-stream")},
    )
    assert resp.status_code == 303
    assert dest.read_bytes() == b"NEW"


# --------------------------------------------------------------------------- #
# Coverage: a form that forgot the field submits and 403s, which is silent
# --------------------------------------------------------------------------- #
TEMPLATE_DIR = __import__("pathlib").Path(
    __import__("central.dashboard.templating", fromlist=["TEMPLATE_DIR"]).TEMPLATE_DIR
)

_FORM_OPEN = re.compile(r"<form\b", re.I)
#: Jinja comments are stripped first: three of them talk *about* forms, and a
#: sentence mentioning <form> is not a form.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def _tag_end(src: str, start: int) -> int:
    """Index of the '>' closing the tag at ``start``, ignoring quotes and Jinja."""
    i, quote = start, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
        elif ch in "\"'":
            quote = ch
            i += 1
        elif src[i:i + 2] in ("{{", "{%", "{#"):
            close = {"{{": "}}", "{%": "%}", "{#": "#}"}[src[i:i + 2]]
            i = src.index(close, i) + 2
        elif ch == ">":
            return i
        else:
            i += 1
    raise AssertionError("unterminated <form> tag")


def _source_forms():
    """(template, method, tag, body-after-tag) for every <form> in the templates."""
    for path in sorted(TEMPLATE_DIR.glob("*.html")):
        # Blanked rather than removed so offsets stay meaningful.
        src = _JINJA_COMMENT.sub(lambda mo: " " * len(mo.group(0)), path.read_text(encoding="utf-8"))
        for mo in _FORM_OPEN.finditer(src):
            end = _tag_end(src, mo.start())
            tag = src[mo.start():end + 1]
            method = re.search(r"""method\s*=\s*["']?(\w+)""", tag, re.I)
            yield path.name, (method.group(1).lower() if method else "get"), tag, src[end:end + 400]


def test_every_post_form_in_the_templates_carries_the_field():
    """Source-level sweep: covers templates no rendered page in CI reaches.

    A form that forgot this does not break visibly -- it submits, 403s, and the
    operator sees a refusal for an action that was perfectly legitimate.
    """
    missing = [
        f"{name}: {tag[:110]}"
        for name, method, tag, after in _source_forms()
        if method == "post" and "csrf_field()" not in after
    ]
    assert not missing, (
        "POST form(s) with no {{ csrf_field() }}:\n  " + "\n  ".join(missing)
    )


def test_no_get_form_carries_the_field():
    """A token in a GET form ends up in the query string.

    From there it is in the browser history, the Referer header on every
    outbound link from the results page, and the reverse proxy's access log.
    """
    leaking = [
        f"{name}: {tag[:110]}"
        for name, method, tag, after in _source_forms()
        if method != "post" and "csrf_field()" in after
    ]
    assert not leaking, "GET form(s) leaking a token into the URL:\n  " + "\n  ".join(leaking)


class _Forms(HTMLParser):
    """Rendered POST forms and whether each holds a csrf_token input."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: "list[dict]" = []
        self._open: "list[dict]" = []
        self.hx_headers: "list[str]" = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("hx-headers"):
            self.hx_headers.append(a["hx-headers"])
        if tag == "form":
            entry = {"method": (a.get("method") or "get").lower(),
                     "action": a.get("action") or "", "token": False}
            self.forms.append(entry)
            self._open.append(entry)
        elif tag == "input" and a.get("name") == csrf_mod.FIELD_NAME:
            if self._open:
                self._open[-1]["token"] = bool(a.get("value"))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != "form":
            return
        self._open.pop()

    def handle_endtag(self, tag):
        if tag == "form" and self._open:
            self._open.pop()


# The same pages the a11y suite renders -- everything reachable with an admin
# and an otherwise empty database.
PAGES = [
    "/", "/printers", "/alerts", "/approvals", "/account",
    "/manage", "/manage/agents", "/manage/users", "/manage/maintenance",
    "/manage/audit", "/manage/events", "/manage/onboard", "/manage/alert-rules",
    "/manage/suppression", "/manage/billing", "/manage/people",
    "/manage/machines", "/manage/definitions",
    "/supplies/reorder", "/security/posture", "/admin/backup",
    "/settings?group=branding", "/settings?group=notifications",
    "/settings?group=alerts", "/settings?group=polling",
]


@pytest.mark.parametrize("page", PAGES)
def test_every_rendered_post_form_carries_a_token(db, page):
    """The rendered-DOM half of the guard.

    The source sweep above proves the call is written; only a render proves the
    global is actually installed on the environment that page was rendered
    through -- which is the failure that four separate ``Jinja2Templates``
    instances made possible.
    """
    client = _admin(db)
    resp = client.get(page)
    assert resp.status_code == 200, f"{page} did not render: {resp.text[:200]}"
    parsed = _Forms()
    parsed.feed(resp.text)
    bad = [f["action"] for f in parsed.forms if f["method"] == "post" and not f["token"]]
    assert not bad, f"{page} rendered POST form(s) with no token: {bad}"


def test_base_template_declares_the_htmx_header(db):
    """One hx-headers on <body> is what covers every hx-post in the app."""
    client = _admin(db)
    parsed = _Forms()
    parsed.feed(client.get("/alerts").text)
    assert len(parsed.hx_headers) == 1, parsed.hx_headers
    declared = parsed.hx_headers[0]
    assert csrf_mod.HEADER_NAME in declared
    assert _token(client) in declared
