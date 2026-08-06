"""The one-shot message channel, and the severity it could not previously carry.

The channel used to be a bare string in ``session["flash"]`` rendered into a
hardcoded emerald *success* box by ``base.html``. ~224 call sites wrote to it and
a large minority are refusals, so failures were announced in the colour this
interface uses for "that worked" -- the UI committing exactly the sin
``ChannelResult.sent`` forbids the delivery layer.

Ten page templates also rendered the same variable a second time inside their own
body block, in four different colours, so on nine pages one sentence appeared
twice. The portal had a third copy under its own key, which is how a customer
whose support request FAILED was told so in green.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central.dashboard import flash as fl
from central.main import app
from central.security import hash_password


class _Req:
    """Minimal stand-in: the module only ever touches ``request.session``."""

    def __init__(self, session=None):
        self.session = {} if session is None else session


# --------------------------------------------------------------------------- #
# The channel itself
# --------------------------------------------------------------------------- #
def test_a_message_carries_its_level():
    req = _Req()
    fl.flash(req, "Printer updated.")
    assert req.session[fl.FLASH_KEY] == {"level": "success", "text": "Printer updated."}


def test_error_is_a_distinct_level():
    req = _Req()
    fl.error(req, "No changes saved.")
    assert req.session[fl.FLASH_KEY]["level"] == "error"


@pytest.mark.parametrize("level", fl.LEVELS)
def test_every_declared_level_is_writable_and_has_a_tone(level):
    req = _Req()
    fl.flash(req, "x", level=level)
    assert fl.pop(req)["level"] == level
    assert fl.tone_for(level)


def test_an_unknown_level_raises_at_write_time():
    """Writing is our own code, reached by tests, so a typo must not resolve
    silently to info-blue and look deliberate -- the silent-fallback pattern
    this codebase keeps getting bitten by."""
    with pytest.raises(fl.FlashLevelError):
        fl.flash(_Req(), "x", level="succes")


def test_reading_is_permissive_where_writing_is_strict():
    """The value arrives from a signed cookie that an older build may have
    minted; a page that 500s on a stale cookie is worse than a neutral box."""
    req = _Req({fl.FLASH_KEY: {"level": "banana", "text": "hello"}})
    # Asserted per key rather than as a whole dict: the payload later grew
    # ``fields`` and ``errors`` for form repopulation, and a whole-dict equality
    # turns every future addition into three unrelated test failures that say
    # nothing about what broke.
    got = fl.pop(req)
    assert got["level"] == fl.FALLBACK_LEVEL
    assert got["text"] == "hello"


def test_a_legacy_plain_string_survives_the_upgrade():
    """A session written by the previous version holds a bare string. Dropping
    it would silently eat the one message an operator was waiting for."""
    req = _Req({fl.FLASH_KEY: "Client created."})
    got = fl.pop(req)
    assert got["level"] == fl.FALLBACK_LEVEL
    assert got["text"] == "Client created."


def test_popping_is_one_shot():
    req = _Req()
    fl.flash(req, "once")
    assert fl.pop(req)["text"] == "once"
    assert fl.pop(req) is None


def test_empty_and_missing_values_are_none():
    assert fl.pop(_Req()) is None
    assert fl.pop(_Req({fl.FLASH_KEY: ""})) is None
    assert fl.pop(_Req({fl.FLASH_KEY: {"level": "error", "text": ""}})) is None


def test_pop_never_raises_on_a_hostile_session():
    class Broken:
        session = property(lambda self: (_ for _ in ()).throw(RuntimeError("no store")))

    assert fl.pop(Broken()) is None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin))
    db.commit()
    client = TestClient(app)
    resp = client.post("/login", data={"username": "admin", "password": "pw12345678"},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def test_the_manage_helper_delegates_to_the_shared_channel():
    """~224 call sites keep calling ``_flash``; it must be the same channel and
    must accept a level, or the migration is cosmetic."""
    from central.dashboard import manage as mg

    req = _Req()
    mg._flash(req, "refused", level="error")
    got = fl.pop(req)
    assert got["level"] == "error"
    assert got["text"] == "refused"
    mg._flash_error(req, "also refused")
    assert fl.pop(req)["level"] == "error"


@pytest.mark.parametrize(
    "level,expect_role,expect_class",
    [
        ("success", "status", "emerald"),
        ("error", "alert", "red"),
        ("warning", "alert", "amber"),
        ("info", "status", "sky"),
    ],
)
def test_each_level_renders_its_own_tone_and_role(level, expect_role, expect_class):
    """Rendered straight through the Jinja environment: the template is the unit
    under test, not the route that populates it."""
    from central.dashboard.templating import templates

    env = templates.env
    tmpl = env.from_string(
        '{% import "_components.html" as c %}'
        '<div class="{{ c.note(flash.level) }}" '
        'role="{{ \'alert\' if flash.level in (\'error\', \'warning\') else \'status\' }}">'
        "{{ flash.text }}</div>"
    )
    html = tmpl.render(flash={"level": level, "text": "MARKER"})
    assert f'role="{expect_role}"' in html
    assert expect_class in html
    assert "MARKER" in html


def test_note_has_a_success_tone():
    """Its absence is why base.html hand-rolled an emerald box outside the
    component layer -- the missing tone and the lying channel were one defect."""
    from central.dashboard.templating import templates

    tmpl = templates.env.from_string(
        '{% import "_components.html" as c %}{{ c.note("success") }}'
    )
    rendered = tmpl.render()
    assert "emerald" in rendered
    # And it must not silently resolve to the info tone.
    info = templates.env.from_string(
        '{% import "_components.html" as c %}{{ c.note("info") }}'
    ).render()
    assert rendered.strip() != info.strip()


def test_a_refusal_renders_once_and_in_the_error_tone(http, db):
    """End to end through real plumbing: a refusal must appear exactly ONCE (nine
    pages used to render the same sentence a second time in their own colour) and
    must not arrive in the success box (which is what it did for every refusal
    this channel carried).

    Counting the message TEXT rather than role= attributes on purpose: the page
    also carries a worker banner and an htmx error toast, both legitimately
    role="alert", and an earlier version of this test counted those and failed.
    """
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(client_id=client.id, site_id=site.id, ip="10.0.0.7",
                        snmp_version="2c", discovery_state=m.DiscoveryState.approved)
    db.add(printer)
    db.commit()

    resp = http.post(
        f"/manage/printers/{printer.id}",
        data={"site_id": str(site.id), "ip": "10.0.0.7", "snmp_version": "nonsense"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    page = resp.text
    assert page.count("is not an SNMP version") == 1, "flash rendered more than once"
    # The severity has to survive to the markup, or the refactor is decorative.
    box = re.search(r'<div class="([^"]*)"[^>]*role="alert"[^>]*>[^<]*is not an SNMP version',
                    page)
    assert box, "the refusal did not render in an alert box"
    assert "red" in box.group(1), f"refusal rendered in a non-error tone: {box.group(1)}"


def test_no_template_except_base_renders_the_flash():
    """The structural guarantee. If a page re-adds its own block, the duplicate
    is back and no behavioural test would necessarily catch it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "central" / "dashboard" / "templates"
    offenders = []
    for path in sorted(root.glob("*.html")):
        if path.name == "base.html":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\{\{\s*flash\b|\{%[-\s]*if\s+flash\b|portal_flash", text):
            offenders.append(path.name)
    assert not offenders, f"templates rendering flash outside base.html: {offenders}"


def test_the_portal_uses_the_shared_channel():
    """The portal's private ``portal_flash`` key is gone; its failure message was
    the clearest instance of the defect -- a customer told in green that their
    support request had not been delivered."""
    import pathlib

    routes = (pathlib.Path(__file__).resolve().parents[1]
              / "central" / "dashboard" / "routes.py").read_text(encoding="utf-8")
    assert "portal_flash" not in routes
    # And the undelivered branch must be an error, not a success.
    assert re.search(r"_flash_mod\.error\(\s*request,\s*\n?\s*\"Could not deliver", routes), (
        "the undelivered-report branch is not raised as an error"
    )
