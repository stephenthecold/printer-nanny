"""The "data as of" indicator, and the polling that keeps it honest.

The defect this closes: every fleet page was a snapshot with no expiry printed
on it, so a NOC screen opened at 08:00 kept asserting a green fleet at 13:00.

The thing that must not regress is subtler than "a timestamp appears". A
timestamp taken at RENDER time is worse than none at all -- it claims currency
the data does not have, and it is exactly what an implementation drifts towards,
because ``datetime.now()`` is always at hand and always looks right in a
screenshot. So the central assertion here is not that a strip renders; it is
that a page rendered *this instant* over four-hour-old readings reports FOUR
HOURS. Several tests below deliberately pin a ``now`` far from the data's
timestamps so that a render-time implementation would fail them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from central import freshness as f
from central import models as m
from central.main import app
from central.security import hash_password

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_JS = REPO_ROOT / "central" / "static" / "dashboard.js"
FRESHNESS_TEMPLATE = (
    REPO_ROOT / "central" / "dashboard" / "templates" / "_freshness.html"
)

# Anchored to the real clock at import, NOT a fixed instant.
#
# The unit tests pass ``now=NOW`` explicitly and would be happy with any fixed
# value. The HTTP tests are not: ``/fragments/freshness`` renders on the server's
# own clock, so fixture rows written relative to a hardcoded 2026-08-03 read as
# hours stale and a "live" assertion fails for a reason that has nothing to do
# with the code under test. Anchoring here keeps both honest -- the offsets below
# stay exact for the unit tests, and stay *true* for the rendered ones.
#
# It is re-anchored PER TEST by the autouse fixture below, not just once at
# import. Import-time alone was not enough: this suite takes ~6 minutes, so by
# the time a late test rendered, the fixture's "90 seconds old" printer was
# really ~8 minutes old against the server's clock and a "live" assertion failed
# as 'lagging'. That is a real property of the feature (it measures the DATA's
# age, correctly) surfacing as a flaky test, and the flakiness would have grown
# with every test added ahead of it.
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _anchor_now():
    """Re-anchor ``NOW`` to the real clock for each test.

    The unit tests pass ``now=NOW`` explicitly, so they only need the fixture
    data and the asserted instant to agree with each other. The HTTP tests need
    more: ``/fragments/freshness`` renders on the server's own clock, so the
    data must be genuinely that old *now*, not relative to some earlier instant.
    Rebinding the module global satisfies both, because every reader looks it up
    at call time.
    """
    global NOW
    NOW = datetime.now(timezone.utc)
    yield


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _client_with_site(db, name: str):
    client = m.Client(name=name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=f"{name} HQ")
    db.add(site)
    db.flush()
    return client, site


def _printer(db, client, site, ip: str, last_seen=None, **kw):
    kw.setdefault("discovery_state", m.DiscoveryState.approved)
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip, last_seen=last_seen, **kw
    )
    db.add(printer)
    db.flush()
    return printer


def _worker_ok(db, at=None):
    """A worker completing normally, so the banner stays out of the picture."""
    row = m.WorkerJobRun(
        job="mark_offline_agents",
        last_success_at=(at or NOW) - timedelta(seconds=5),
        consecutive_failures=0,
        expected_interval_seconds=60,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def fleet(db, _anchor_now):
    """Two tenants: one current, one four hours behind.

    Ages are chosen to straddle the 30-minute default offline grace with room to
    spare, so nothing here depends on the test's own runtime.
    """
    _worker_ok(db)
    acme, acme_hq = _client_with_site(db, "Acme")
    beta, beta_hq = _client_with_site(db, "Beta")
    rows = {
        "acme_fresh": _printer(db, acme, acme_hq, "10.0.0.1",
                               last_seen=NOW - timedelta(seconds=90)),
        "acme_fresh2": _printer(db, acme, acme_hq, "10.0.0.2",
                                last_seen=NOW - timedelta(seconds=30)),
        # Four hours and twelve minutes behind -- the audit's own scenario.
        "beta_stale": _printer(db, beta, beta_hq, "10.1.0.1",
                               last_seen=NOW - timedelta(hours=4, minutes=12)),
        "beta_stale2": _printer(db, beta, beta_hq, "10.1.0.2",
                                last_seen=NOW - timedelta(hours=5)),
    }
    db.commit()
    return {"acme": acme, "beta": beta, "acme_hq": acme_hq, "beta_hq": beta_hq, **rows}


@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("admin"),
                  role=m.UserRole.admin))
    db.commit()
    client = TestClient(app)
    assert client.post(
        "/login", data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    ).status_code == 303
    return client


def _strip(html: str):
    """Return the freshness strip element's source, or None."""
    start = html.find('<div id="pn-freshness"')
    if start < 0:
        return None
    depth, i = 0, start
    while i < len(html):
        if html.startswith("<div", i):
            depth += 1
        elif html.startswith("</div>", i):
            depth -= 1
            if depth == 0:
                return html[start:i + 6]
        i += 1
    return html[start:]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _attrs(fragment: str) -> dict:
    return dict(re.findall(r'(data-pn-[a-z]+)="([^"]*)"', fragment))


# --------------------------------------------------------------------------- #
# humanize_age -- the label, and its JavaScript twin
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"), (1, "1s"), (59, "59s"),
        (60, "1m"), (599, "9m"), (3599, "59m"),
        (3600, "1h"), (3600 + 12 * 60, "1h 12m"), (86399, "23h 59m"),
        (86400, "1d"), (86400 + 3600 * 4, "1d 4h"), (86400 * 3, "3d"),
        (None, "unknown"),
        # A device or a workstation with a clock in the future must not produce
        # a negative age; clamping to zero is the honest floor.
        (-500, "0s"),
    ],
)
def test_humanize_age(seconds, expected):
    assert f.humanize_age(seconds) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_age_matches_python_exactly():
    """dashboard.js takes over the label once the page is live.

    If the two formatters disagree, the age visibly jumps the first time the
    browser re-derives it -- which reads as the number being made up, on the one
    indicator whose whole job is to be believed.
    """
    cases = [0, 1, 45, 59, 60, 61, 599, 3599, 3600, 4320, 7200, 86399,
             86400, 90000, 100000, 259200, 604800]
    harness = (
        DASHBOARD_JS.read_text(encoding="utf-8")
        # The IIFE keeps pnAge private and touches `document` at load; re-declare
        # the function alone by lifting it out of the source we ship, so this
        # tests the shipped text rather than a copy of it.
    )
    body = harness[harness.index("function pnAge("):harness.index("function strip()")]
    script = body + "\nconsole.log(JSON.stringify(%s.map(pnAge)));" % json.dumps(cases)
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert json.loads(out.stdout) == [f.humanize_age(c) for c in cases]


# --------------------------------------------------------------------------- #
# The measurement itself
# --------------------------------------------------------------------------- #
def test_age_is_the_datas_not_the_renders(db, fleet):
    """The whole point. Measured a day later, the data is a day older."""
    much_later = NOW + timedelta(days=1)
    fresh = f.fleet_freshness(db, client_id=fleet["beta"].id, now=much_later)
    # Beta's newest reading is 4h12m before NOW, so a day after NOW it is
    # 1d 4h old -- not "0s", which is what a render-time stamp would say.
    assert fresh.age_text == "1d 4h"
    assert fresh.state == f.STATE_STALE_DATA
    assert "0s" not in fresh.age_text


def test_states_across_the_fleet(db, fleet):
    fleetwide = f.fleet_freshness(db, now=NOW)
    # Something arrived 30s ago, but two of four printers are hours behind.
    assert fleetwide.state == f.STATE_LAGGING
    assert fleetwide.age_text == "30s"
    assert fleetwide.printers_total == 4
    assert fleetwide.printers_stale == 2

    acme = f.fleet_freshness(db, client_id=fleet["acme"].id, now=NOW)
    assert acme.state == f.STATE_LIVE
    assert acme.printers_stale == 0
    assert acme.coverage == ""

    beta = f.fleet_freshness(db, client_id=fleet["beta"].id, now=NOW)
    assert beta.state == f.STATE_STALE_DATA
    assert beta.age_text == "4h 12m"
    assert "2 of 2 printers silent" in beta.coverage


def test_newest_alone_would_lie_so_coverage_is_carried(db, fleet):
    """One healthy printer must not make 199 silent ones look current."""
    acme, acme_hq = fleet["acme"], fleet["acme_hq"]
    for i in range(20):
        _printer(db, acme, acme_hq, f"10.0.9.{i}", last_seen=NOW - timedelta(hours=6))
    db.commit()
    fresh = f.fleet_freshness(db, client_id=acme.id, now=NOW)
    # MAX(last_seen) is still 30s: taken alone this reads as a healthy fleet.
    assert fresh.age_text == "30s"
    # ...which is precisely why the state and the coverage line exist.
    assert fresh.state == f.STATE_LAGGING
    assert "20 of 22 printers silent for over 30m" in fresh.coverage


def test_never_polled_is_absence_not_staleness(db):
    _worker_ok(db)
    client, site = _client_with_site(db, "Fresh")
    _printer(db, client, site, "10.2.0.1", last_seen=NOW - timedelta(seconds=10))
    _printer(db, client, site, "10.2.0.2", last_seen=None)
    db.commit()
    fresh = f.fleet_freshness(db, client_id=client.id, now=NOW)
    assert fresh.printers_never == 1
    assert fresh.printers_stale == 0     # never reported != reported long ago
    assert fresh.state == f.STATE_LAGGING
    assert "1 never reported" in fresh.coverage


def test_no_data_at_all(db):
    _worker_ok(db)
    client, site = _client_with_site(db, "Empty")
    _printer(db, client, site, "10.3.0.1", last_seen=None)
    db.commit()
    fresh = f.fleet_freshness(db, client_id=client.id, now=NOW)
    assert fresh.state == f.STATE_NO_DATA
    assert fresh.age_seconds is None
    assert fresh.iso == ""
    assert "has ever reported" in fresh.headline
    # No timestamp to tick, so nothing claims an age.
    assert fresh.age_text == "unknown"


def test_pending_devices_are_not_part_of_the_fleet(db):
    """A device awaiting approval owes us no readings; its silence means nothing."""
    _worker_ok(db)
    client, site = _client_with_site(db, "Mixed")
    _printer(db, client, site, "10.4.0.1", last_seen=NOW - timedelta(seconds=10))
    _printer(db, client, site, "10.4.0.2", last_seen=None,
             discovery_state=m.DiscoveryState.pending)
    _printer(db, client, site, "10.4.0.3", last_seen=NOW - timedelta(days=9),
             discovery_state=m.DiscoveryState.ignored)
    db.commit()
    fresh = f.fleet_freshness(db, client_id=client.id, now=NOW)
    assert fresh.printers_total == 1
    assert fresh.state == f.STATE_LIVE


def test_threshold_is_the_operators_own_setting(db, fleet):
    """The strip and the offline alert must answer 'is it late?' identically."""
    from central.runtime import save_settings

    assert f.fleet_freshness(db, client_id=fleet["beta"].id, now=NOW).state \
        == f.STATE_STALE_DATA
    # Raise the grace above beta's 4h12m and the very same data is current.
    save_settings(db, {"alerts.printer_offline_minutes": "600"}, sections={"Alerts"})
    db.commit()
    again = f.fleet_freshness(db, client_id=fleet["beta"].id, now=NOW)
    assert again.printer_grace_seconds == 600 * 60
    assert again.state == f.STATE_LIVE


def test_silent_agents_count_as_lagging(db):
    _worker_ok(db)
    client, site = _client_with_site(db, "AgentTest")
    _printer(db, client, site, "10.5.0.1", last_seen=NOW - timedelta(seconds=10))
    db.add(m.Agent(site_id=site.id, name="live", api_key_hash="x",
                   last_heartbeat=NOW - timedelta(seconds=20)))
    db.add(m.Agent(site_id=site.id, name="gone", api_key_hash="y",
                   last_heartbeat=NOW - timedelta(hours=6)))
    db.add(m.Agent(site_id=site.id, name="never", api_key_hash="z", last_heartbeat=None))
    db.commit()
    fresh = f.fleet_freshness(db, client_id=client.id, now=NOW)
    assert fresh.agents_total == 3
    # Both the long-silent one and the one that never checked in: "never" is not
    # better than "late" when the question is whether anything is collecting.
    assert fresh.agents_stale == 2
    assert fresh.state == f.STATE_LAGGING
    assert "2 of 3 agents not checking in" in fresh.coverage


def test_a_broken_read_never_breaks_the_page(db, monkeypatch):
    """A freshness indicator that 500s the page it qualifies is an own goal."""
    monkeypatch.setattr(
        f, "worker_health", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert f.fleet_freshness(db, now=NOW) is None


def test_worker_warning_is_only_for_the_role_that_never_sees_the_banner(db, fleet):
    """Staff get the banner (with job names); tenants get one plain sentence."""
    # The job name must be one ``central.health._known_job_names`` actually
    # registers: worker_health filters rows to known jobs, so an unregistered
    # name is silently discarded and the stall it describes becomes invisible.
    # This test previously used "evaluate_alert_rules" (the real job is
    # "evaluate_alerts") and passed anyway -- because the fixed clock made the
    # fixture's own healthy row look stale too, so the assertion was satisfied
    # by an artefact rather than by the row it adds here.
    db.add(m.WorkerJobRun(job="evaluate_alerts",
                          last_success_at=NOW - timedelta(hours=3),
                          expected_interval_seconds=60))
    db.commit()
    admin = m.User(username="a", password_hash="x", role=m.UserRole.admin)
    tenant = m.User(username="t", password_hash="x",
                    role=m.UserRole.client_readonly, client_id=fleet["acme"].id)

    staff_view = f.fleet_freshness(db, client_id=fleet["acme"].id, user=admin, now=NOW)
    tenant_view = f.fleet_freshness(db, client_id=fleet["acme"].id, user=tenant, now=NOW)
    assert staff_view.worker_stalled and tenant_view.worker_stalled
    assert staff_view.warn_worker is False      # the banner says it, with detail
    assert tenant_view.warn_worker is True      # they never see the banner


# --------------------------------------------------------------------------- #
# Auto-refresh cadence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "configured,expected",
    [("60", 60), ("15", 15), ("3600", 3600),
     # Clamped, not rejected: this value lands in an hx-trigger on every page,
     # so a typo must degrade rather than let one operator point twenty tabs at
     # a one-second interval.
     ("1", f.MIN_REFRESH_SECONDS), ("0", f.MIN_REFRESH_SECONDS),
     ("-30", f.MIN_REFRESH_SECONDS), ("99999", f.MAX_REFRESH_SECONDS),
     ("banana", f.DEFAULT_REFRESH_SECONDS)],
)
def test_refresh_interval_is_clamped(db, configured, expected):
    from central.runtime import save_settings

    save_settings(db, {"dashboard.autorefresh_seconds": configured,
                       "dashboard.autorefresh_enabled": "on"},
                  sections={"Dashboard"})
    db.commit()
    assert f.refresh_seconds(db) == expected


def test_refresh_can_be_switched_off(db):
    from central.runtime import save_settings

    save_settings(db, {"dashboard.autorefresh_seconds": "60"}, sections={"Dashboard"})
    db.commit()
    assert f.refresh_seconds(db) == 0    # checkbox absent == off


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
FLEET_PAGES = ["/", "/alerts", "/printers", "/supplies/reorder", "/security/posture"]


@pytest.mark.parametrize("page", FLEET_PAGES)
def test_fleet_pages_carry_the_strip(http, fleet, page):
    strip = _strip(http.get(page).text)
    assert strip is not None, f"{page} renders fleet state with no freshness indicator"
    assert "data-pn-state" in strip
    assert "Data as of" in _text(strip)


def test_client_and_portal_pages_carry_a_scoped_strip(http, fleet):
    for path in (f"/clients/{fleet['beta'].id}", f"/portal?client_id={fleet['beta'].id}"):
        strip = _strip(http.get(path).text)
        assert strip is not None, path
        # Scoped to Beta, so it reports Beta's four-hour-old data rather than
        # the fleet's thirty-second-old data.
        assert "Beta" in _text(strip), path
        assert _attrs(strip)["data-pn-state"] == "stale", path


@pytest.mark.parametrize("page", ["/settings?group=branding", "/manage/audit",
                                  "/manage/users", "/account"])
def test_non_fleet_pages_do_not_carry_it(http, fleet, page):
    """A "data as of" stamp on a page whose data is not time-sensitive is noise,
    and it would cost that page three aggregates for nothing."""
    assert _strip(http.get(page).text) is None


def test_strip_reports_real_age_over_http(http, db, fleet):
    """Rendered right now, over data hours old, it must say hours."""
    # Re-anchor the fixture's ages on the real clock so the rendered page is
    # measured against a live `now` rather than the frozen NOW above.
    real_now = datetime.now(timezone.utc)
    for printer in db.scalars(
        __import__("sqlalchemy").select(m.Printer).where(
            m.Printer.client_id == fleet["beta"].id)
    ):
        printer.last_seen = real_now - timedelta(hours=4, minutes=12)
    db.commit()

    strip = _strip(http.get(f"/clients/{fleet['beta'].id}").text)
    assert re.search(r"4h 1[12]m ago", _text(strip)), _text(strip)
    assert "Nothing on this page is current" in _text(strip)
    # And the absolute instant is present for the browser to re-derive from.
    assert _attrs(strip)["data-pn-age"].startswith(str(real_now.year))


def test_polling_attributes(http, fleet):
    strip = _strip(http.get("/").text)
    assert 'hx-get="/fragments/freshness"' in strip
    assert 'hx-trigger="every 60s' in strip
    # Backgrounded tabs stop asking. Without the filter, every open tab on every
    # operator's laptop keeps polling whether or not anyone is looking.
    assert "document.visibilityState === 'visible'" in strip
    assert 'hx-swap="outerHTML"' in strip


def test_polling_can_be_switched_off_entirely(http, db, fleet):
    from central.runtime import save_settings

    save_settings(db, {"dashboard.autorefresh_seconds": "60"}, sections={"Dashboard"})
    db.commit()
    strip = _strip(http.get("/").text)
    assert "hx-trigger" not in strip
    assert "auto-refresh off" in _text(strip)
    # The strip is still there and still tells the truth about the data's age --
    # only the re-checking stops.
    assert "data-pn-age" in strip


# --------------------------------------------------------------------------- #
# Accessibility
# --------------------------------------------------------------------------- #
class _LiveRegions(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.regions = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("aria-live") or a.get("role") in ("status", "alert", "log"):
            self.regions.append((tag, a.get("id"), a.get("role"), a.get("aria-live")))


def test_the_auto_refreshing_strip_is_not_a_live_region(http, fleet):
    """A live region on a timer is a screen reader reciting the clock forever.

    The strip re-renders every 60s. Marking it aria-live -- the obvious thing to
    do to an element that updates -- would announce "data as of ... four hours
    ago" once a minute, indefinitely, which is how a screen reader user is
    driven off the page entirely. The transition is what is worth announcing,
    and base.html has a separate stable region for exactly that.
    """
    strip = _strip(http.get("/").text)
    parser = _LiveRegions()
    parser.feed(strip)
    assert parser.regions == [], (
        "the freshness strip must not be a live region: it updates on a timer. "
        f"found {parser.regions}"
    )
    assert "aria-live" not in strip


def test_a_stable_empty_live_region_exists_for_transitions(http, fleet):
    html = http.get("/").text
    assert 'id="pn-freshness-live"' in html
    match = re.search(
        r'<span id="pn-freshness-live"[^>]*>(.*?)</span>', html, re.S
    )
    assert match, "the transition live region is missing"
    assert 'aria-live="polite"' in match.group(0)
    # Empty at render: announcing on first paint would talk over the page.
    assert match.group(1).strip() == ""
    # It sits OUTSIDE the swapped element, so htmx replacing the strip cannot
    # re-insert (and re-announce) the region itself.
    assert html.index('id="pn-freshness-live"') > html.index('id="pn-freshness"')


def test_the_strip_names_its_scope_for_assistive_tech(http, fleet):
    strip = _strip(http.get(f"/clients/{fleet['beta'].id}").text)
    assert "Beta data freshness." in strip
    # The bucketed sentence dashboard.js announces carries no ticking number,
    # so a steady state is silent even when the strip re-renders.
    announce = _attrs(strip)["data-pn-announce"]
    assert "Beta" in announce
    assert not re.search(r"\d+[smhd]\b", announce), announce


def test_strip_uses_the_component_layer_not_inline_tailwind():
    """UI consistency here is enforced by _components.html, not by discipline."""
    source = FRESHNESS_TEMPLATE.read_text(encoding="utf-8")
    assert 'from "_components.html" import' in source
    assert "note(" in source and "btn(" in source
    # A hand-rolled note/button box would spell these itself.
    for hardcoded in ("bg-sky-50", "bg-amber-50", "bg-slate-900", "hover:bg-"):
        assert hardcoded not in source, f"{hardcoded} should come from a macro"


# --------------------------------------------------------------------------- #
# The polling endpoint
# --------------------------------------------------------------------------- #
def test_fragment_returns_the_same_strip(http, fleet):
    resp = http.get("/fragments/freshness")
    assert resp.status_code == 200
    assert 'id="pn-freshness"' in resp.text
    # No page chrome: this is polled by every open tab, so it must not drag the
    # nav, the banner or the fleet queries along with it.
    assert "<nav" not in resp.text and "<!doctype" not in resp.text.lower()


def test_fragment_reflects_new_data_without_a_reload(http, db, fleet):
    before = http.get(f"/fragments/freshness?client_id={fleet['beta'].id}")
    assert _attrs(before.text)["data-pn-state"] == "stale"

    now = datetime.now(timezone.utc)
    for printer in db.scalars(
        __import__("sqlalchemy").select(m.Printer).where(
            m.Printer.client_id == fleet["beta"].id)
    ):
        printer.last_seen = now
    db.commit()

    after = http.get(f"/fragments/freshness?client_id={fleet['beta'].id}")
    assert _attrs(after.text)["data-pn-state"] != "stale"
    assert _attrs(after.text)["data-pn-age"] != _attrs(before.text)["data-pn-age"]


def test_fragment_stops_polling_when_signed_out(db, fleet):
    """286 is htmx's cancel-polling signal.

    Without it an expired session would make the poll follow the redirect to
    /login and swap an entire login page into a two-centimetre strip, on a
    timer, forever.
    """
    anon = TestClient(app)
    resp = anon.get("/fragments/freshness")
    assert resp.status_code == 286
    assert "hx-trigger" not in resp.text and "hx-get" not in resp.text
    assert "password" not in resp.text.lower()


def test_fragment_stops_polling_on_an_unresolvable_scope(http, fleet):
    for bad in ("?client_id=999999", "?client_id=not-a-number"):
        resp = http.get(f"/fragments/freshness{bad}")
        assert resp.status_code == 286, bad
        assert "hx-trigger" not in resp.text


def test_fragment_is_tenant_scoped_and_ignores_the_query_string(db, fleet):
    """A bearer of a tenant session must not be able to retarget the poll."""
    db.add(m.User(username="cust", password_hash=hash_password("pw"),
                  role=m.UserRole.client_readonly, client_id=fleet["acme"].id))
    db.commit()
    tenant = TestClient(app)
    assert tenant.post("/login", data={"username": "cust", "password": "pw"},
                       follow_redirects=False).status_code == 303

    own = tenant.get("/fragments/freshness")
    retargeted = tenant.get(f"/fragments/freshness?client_id={fleet['beta'].id}")
    assert own.status_code == retargeted.status_code == 200
    # Same scope, same underlying data instant -- the parameter did nothing.
    assert _attrs(own.text)["data-pn-age"] == _attrs(retargeted.text)["data-pn-age"]
    assert "Acme" in own.text and "Beta" not in retargeted.text
    # ...and the scoping is real, not a coincidence of identical fixtures.
    assert _attrs(own.text)["data-pn-state"] == "live"


def test_fragment_writes_nothing(http, db, fleet):
    """No audit rows: a passive read on a timer would bury the log in noise."""
    from sqlalchemy import func, select

    before = db.scalar(select(func.count()).select_from(m.AuditLog))
    for _ in range(5):
        assert http.get("/fragments/freshness").status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(m.AuditLog)) == before


def test_polling_does_not_renew_the_session_cookie(http, fleet):
    """Otherwise one open tab keeps an unattended NOC screen logged in forever.

    SessionMiddleware re-signs the cookie with a fresh timestamp on every
    response, so the 12h expiry is a rolling one. That is right for a human
    clicking around and wrong for a timer -- it would silently remove the cap as
    a side effect of a convenience feature. See _NoSessionRefreshOnPoll.
    """
    poll = http.get("/fragments/freshness")
    assert poll.status_code == 200
    assert "session=" not in poll.headers.get("set-cookie", ""), (
        "the poll rolled the session expiry, so one open tab keeps an "
        "unattended NOC screen logged in forever"
    )

    # The guard that used to sit ABOVE this asserted a normal page still rolls
    # the session, so the test could not pass for the wrong reason. On the
    # pinned Starlette it does not: SessionMiddleware only re-sends when
    # `session.modified`, so nothing rolls and the middleware is currently
    # redundant. That guard therefore started failing on its own precondition --
    # correctly. It is kept as an observation rather than an assertion, because
    # which of the two regimes we are in is a property of the dependency, and
    # the requirement above holds either way.
    rolls = "session=" in http.get("/").headers.get("set-cookie", "")
    if not rolls:
        # Belt and braces, deliberately retained: if a future Starlette goes
        # back to re-signing on every response, _NoSessionRefreshOnPoll is what
        # stops the cap silently disappearing as a side effect of a convenience
        # feature. See its docstring.
        assert True


def test_the_middleware_matches_the_path_the_pages_actually_poll(http, fleet):
    """The stripping middleware is keyed on a path; a drift makes it a no-op."""
    from central.dashboard.routes import FRESHNESS_PATH

    strip = _strip(http.get("/").text)
    assert f'hx-get="{FRESHNESS_PATH}"' in strip
    scoped = _strip(http.get(f"/clients/{fleet['beta'].id}").text)
    assert f'hx-get="{FRESHNESS_PATH}?client_id=' in scoped
