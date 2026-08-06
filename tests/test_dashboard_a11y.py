"""Accessibility invariants for the dashboard, checked against rendered HTML.

These assert on the real DOM rather than on template source, because template
source cannot answer the question that matters. A `<label>` that *wraps* its
control is correctly associated and needs no `for=`; a `<label>` that merely
sits next to one is not associated at all, even though both look identical in
a grep. Only the rendered tree distinguishes them.

The invariants:

1. Every form control has an accessible name. Before this, 85 controls had only
   a `placeholder` -- which assistive tech does not treat as a name, and which
   disappears from view on the first keystroke, leaving (for instance) three
   visually identical password boxes on /account.
2. Interactive controls carry a visible focus ring. Keyboard operation otherwise
   depends on whatever the browser draws by default, which is routinely
   invisible on this app's dark and coloured buttons. Enforced on the component
   layer outright and on pages as a ratchet -- see the section comment, which
   states exactly how much of the dashboard is still outside it.
3. The nav marks the current page with aria-current. Thirteen identical links
   with nothing indicating which one you are on was the single most common
   complaint about the old dashboard.

**The pages are rendered against a populated database, and that is the whole
point.** This suite previously rendered every page with one admin user and
nothing else, so no table on any of them had a single row. Per-row controls --
exactly the ones most likely to be unlabelled, because a visible label there
would just repeat the column header -- were never rendered and therefore never
checked. Seeding one row per table turned up eight of them immediately: seven
unnamed controls across /portal, /manage/people and /manage/maintenance, plus a
bare multiplication sign on /manage/people whose only name was a `title=`. A
fixture that seeds no rows does not check a table; it checks an empty-state
message.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import services
from central.main import app
from central.security import (
    generate_api_key,
    generate_enroll_key,
    hash_api_key,
    hash_enroll_key,
    hash_password,
)

# Controls that take no accessible name in the usual sense.
EXEMPT_TYPES = {"hidden", "submit", "button", "image", "reset"}
CONTROL_TAGS = {"input", "select", "textarea"}

#: Ids of the rows the fixture seeds, so a page addressed by id can be named in
#: PAGES (a parametrize list is built at collection time, long before any
#: database exists). The fixture asserts it actually got these, so reordering
#: the seed fails loudly here rather than turning every drill-down page into a
#: redirect that quietly checks nothing.
PRINTER_ID = 1
CLIENT_ID = 1

# Pages rendered against the seeded fixture below. Every one of these renders at
# least one populated table or form; that is the entry criterion, because a page
# whose tables are empty is not being checked, it is being visited.
PAGES = [
    "/",
    "/printers",
    f"/printers/{PRINTER_ID}",
    f"/clients/{CLIENT_ID}",
    "/alerts",
    "/approvals",
    "/account",
    "/portal",
    "/manage",
    "/manage/agents",
    "/manage/users",
    "/manage/people",
    "/manage/machines",
    "/manage/maintenance",
    "/manage/audit",
    "/manage/events",
    "/manage/onboard",
    "/manage/alert-rules",
    "/manage/suppression",
    "/manage/billing",
    "/supplies/reorder",
    # Carries the "add an expected yield" form -- five controls, one of them a
    # select whose only visible context is the column it sits in.
    "/supplies/yield",
    "/manage/definitions",
    f"/manage/printers/{PRINTER_ID}/edit",
    "/security/posture",
    "/admin/backup",
    "/settings?group=branding",
    "/settings?group=notifications",
    # Carries the reorder thresholds. Previously no Alerts-tab render was
    # checked at all, so a spec added there was unlabelled-until-noticed.
    "/settings?group=alerts",
    "/settings?group=polling",
    "/settings?group=auth",
    "/settings?group=agents",
]


class _Controls(HTMLParser):
    """Collect form controls and how each one is labelled."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._label_depth = 0
        self.label_for: set[str] = set()
        self.controls: list[dict] = []
        self.aria_current: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
            return
        if a.get("aria-current"):
            self.aria_current.append(a.get("href", "?"))
        if tag in CONTROL_TAGS:
            if (a.get("type") or "text").lower() in EXEMPT_TYPES:
                return
            self.controls.append(
                {
                    "tag": tag,
                    "name": a.get("name") or "(unnamed)",
                    "id": a.get("id"),
                    "wrapped": self._label_depth > 0,
                    "aria": bool(a.get("aria-label") or a.get("aria-labelledby")),
                }
            )

    def handle_endtag(self, tag):
        if tag == "label" and self._label_depth:
            self._label_depth -= 1


def _parse(html: str) -> _Controls:
    p = _Controls()
    p.feed(html)
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed(db, admin: m.User) -> None:
    """Populate every table these pages render, with one realistic row each.

    Deliberately one row, not many: the invariants are per-control, so a second
    row proves nothing the first did not and costs a schema rebuild's worth of
    time on every one of the parametrized pages. What matters is that each table
    has *some* row, because a table body that renders `empty_row()` renders no
    controls at all.
    """
    now = _now()

    client = m.Client(name="Northwind Health", timezone="America/New_York")
    db.add(client)
    db.flush()
    assert client.id == CLIENT_ID, "seed order changed; update CLIENT_ID"

    site = m.Site(client_id=client.id, name="Main Clinic", address="1 Main St")
    db.add(site)
    db.flush()

    agent = m.Agent(
        site_id=site.id,
        name="Main Clinic agent",
        api_key_hash=hash_api_key(generate_api_key()),
        version="0.1.0+20260101-000000",
        status=m.AgentStatus.online,
        last_heartbeat=now - timedelta(minutes=2),
        install_path="/opt/pn/agent",
    )
    db.add(agent)
    db.flush()

    # An assigned subnet renders the per-row SNMP editor on /manage/agents --
    # community, version, bind interface and the `trusted` checkbox, none of
    # which can carry a visible label without repeating its column header.
    db.add(
        m.Subnet(
            site_id=site.id,
            agent_id=agent.id,
            cidr="10.10.0.0/24",
            label="VLAN 10",
            snmp_community="public",
            snmp_version="2c",
        )
    )

    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        discovered_by_agent_id=agent.id,
        ip="10.10.0.20",
        hostname="brt-100",
        display_name="Front desk MFP",
        brand="Brother",
        model="MFC-L8900CDW",
        serial="SN123456",
        firmware="FW1.20",
        location="Front desk",
        status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
        driver_tier=m.DriverTier.driverless,
        driver_tier_reason="IPP Everywhere advertised",
        ipp_endpoint="ipp://10.10.0.20/ipp/print",
        page_count=120_000,
        last_seen=now - timedelta(minutes=3),
    )
    db.add(printer)
    db.flush()
    assert printer.id == PRINTER_ID, "seed order changed; update PRINTER_ID"

    # Low enough to appear in the reorder list and the portal's "running out"
    # panel, which is where those pages get their rows from.
    db.add(
        m.Supply(
            printer_id=printer.id,
            type=m.SupplyType.toner,
            color="black",
            description="black cartridge",
            level_pct=6.0,
            days_to_empty=4.0,
            forecast_at=now,
        )
    )
    for day in range(3):
        db.add(
            m.Reading(
                printer_id=printer.id,
                ts=now - timedelta(days=2 - day),
                page_count=119_800 + day * 100,
                mono_count=100_000 + day * 80,
                color_count=19_800 + day * 20,
                status=m.PrinterStatus.ok,
                supply_snapshot=[
                    {"type": "toner", "color": "black", "level_pct": 9.0 - day}
                ],
            )
        )
    db.add(
        m.PrinterEvent(
            printer_id=printer.id,
            ts=now - timedelta(hours=1),
            code="low-toner",
            severity=m.EventSeverity.warning,
            source=m.EventSource.snmp_alert,
            message="Black toner low (6%)",
        )
    )

    # Pending discovery: the only thing that puts rows on /approvals, and the
    # per-row approve/ignore controls there are checkbox-selected.
    db.add(
        m.Printer(
            client_id=client.id,
            site_id=site.id,
            discovered_by_agent_id=agent.id,
            ip="10.10.0.80",
            hostname="unknown-1",
            brand="HP",
            mac="00:1B:44:11:3A:B7",
            discovery_state=m.DiscoveryState.pending,
            status=m.PrinterStatus.unknown,
        )
    )

    rule = m.AlertRule(
        name="Low supply (<10%)",
        scope=m.AlertScope.global_,
        condition_type=m.AlertConditionType.supply_below,
        threshold=10,
        severity=m.EventSeverity.warning,
    )
    db.add(rule)
    db.flush()
    db.add(
        m.Alert(
            rule_id=rule.id,
            printer_id=printer.id,
            type=m.AlertConditionType.supply_below,
            severity=m.EventSeverity.warning,
            state=m.AlertState.open,
            title="Front desk MFP: black toner at 6%",
            detail="Below the 10% threshold.",
            dedupe_key=f"supply:{printer.id}:toner:black",
        )
    )

    db.add(
        m.SuppressionWindow(
            name="Overnight quiet hours",
            kind=m.SuppressionKind.quiet_hours,
            action=m.SuppressionAction.defer,
            scope=m.AlertScope.client,
            scope_id=client.id,
            start_minute=18 * 60,
            end_minute=7 * 60,
            weekdays=[0, 1, 2, 3, 4],
        )
    )

    db.add(
        m.MaintenanceSchedule(
            printer_id=printer.id,
            name="Quarterly PM",
            interval_days=90,
            page_threshold=50_000,
            next_due=now + timedelta(days=30),
        )
    )
    db.add(
        m.MaintenanceRecord(
            printer_id=printer.id,
            type=m.MaintenanceType.scheduled,
            performed_by="tech",
            performed_at=now - timedelta(days=60),
            notes="Cleaned rollers.",
            next_due=now + timedelta(days=30),
        )
    )

    db.add(
        m.SupplyYieldExpectation(
            model_tag="MFC-L8900CDW",
            supply_type="toner",
            color="black",
            expected_pages=3000,
            note="Vendor datasheet",
        )
    )

    db.add(
        m.DeviceDefinition(
            key="brother-mfc",
            name="Brother MFC family",
            client_id=client.id,
            spec={"match": {"model_contains": "MFC"}},
            notes="Seeded for the definitions table.",
        )
    )

    card = m.BillingRateCard(
        client_id=client.id,
        name="Standard MPS",
        currency="USD",
        mono_rate="0.0120",
        color_rate="0.0890",
        minimum_charge="50.00",
    )
    db.add(card)
    db.flush()
    db.add(
        m.BillingRateTier(
            rate_card_id=card.id, kind=m.MeterClass.mono, up_to=10_000, rate="0.0100"
        )
    )

    db.add(
        m.EventSubscription(
            name="Ticketing bridge",
            client_id=client.id,
            url="https://example.test/hook",
            secret="enc:v1:seeded",
            event_types=["alert.opened"],
        )
    )

    # People: one person with a direct assignment, one group with a member, so
    # both per-row unassign controls render.
    dana = m.EndUser(
        client_id=client.id, display_name="Dana Reyes", email="dana@example.test"
    )
    sam = m.EndUser(
        client_id=client.id, display_name="Sam Okafor", email="sam@example.test"
    )
    db.add_all([dana, sam])
    group = m.EndUserGroup(client_id=client.id, name="Accounting")
    db.add(group)
    db.flush()
    services.assign_printer(db, printer=printer, end_user=dana, is_default=True)
    services.assign_printer(db, printer=printer, group=group)
    services.sync_group_members(db, group=group, end_users=[dana, sam])

    db.add(
        m.DirectoryConnection(
            client_id=client.id,
            provider=m.DirectorySource.entra,
            enabled=True,
            config={"tenant_id": "t", "client_id": "c"},
            secret="enc:v1:seeded",
            last_sync_at=now - timedelta(hours=6),
            last_ok=True,
        )
    )

    # Machines: a workstation plus a live enroll key, so the machines table and
    # the key table both have rows.
    machine = m.Machine(
        client_id=client.id,
        machine_uid="0d5f8a1c-0000-4000-8000-000000000001",
        name="RECEPTION-01",
        api_key_hash=hash_api_key(generate_api_key()),
        platform="windows",
        last_seen_at=now - timedelta(minutes=10),
    )
    db.add(machine)
    db.flush()
    services.assign_printer(db, printer=printer, machine=machine, is_default=True)
    db.add(
        m.WorkstationEnrollKey(
            client_id=client.id,
            key_hash=hash_enroll_key(generate_enroll_key()),
            label="Reception rollout",
            created_by_user_id=admin.id,
        )
    )
    # No bytes on disk, which the Machines page renders as "missing" -- the
    # documented state after a database restore, and the one that also renders
    # the driver table's per-row delete control.
    db.add(
        m.DriverPackage(
            client_id=client.id,
            name="Brother universal",
            driver_name="Brother MFC-L8900CDW series",
            inf_relpath="brother/BROHL17A.INF",
            model="MFC-L8900",
            platform="windows",
            sha256="0" * 64,
            size=1024,
            stored_at="driver_packages/seeded.zip",
            created_by_user_id=admin.id,
        )
    )

    db.add(
        m.AuditLog(
            ts=now - timedelta(minutes=5),
            user_id=admin.id,
            username=admin.username,
            ip="10.0.0.5",
            action="printer.approve",
            target=f"printer:{printer.id}",
            detail="Seeded audit row.",
        )
    )

    # A second operator, so /manage/users renders a row whose per-row controls
    # (reset password, delete) are not the signed-in admin's own.
    db.add(
        m.User(
            username="tech",
            password_hash=hash_password("tech-password"),
            role=m.UserRole.tech,
        )
    )


@pytest.fixture()
def http(db) -> TestClient:
    admin = m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin
    )
    db.add(admin)
    db.flush()
    _seed(db, admin)
    db.commit()
    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": "admin", "password": "admin"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return client


#: Characters that carry no meaning when spoken. A button whose visible text is
#: made only of these needs an aria-label to say what it does: "×" announces as
#: "times", and an emoji announces as whatever that emoji is called.
_GLYPHS = re.compile(r"^[\W\d_]*$")


class _Buttons(HTMLParser):
    """Collect <button>s with their visible text and aria-label.

    A <button> takes its name from its content rather than from a <label>, so it
    is exempt from the form-control check and needs this one instead. `title=`
    does not close the gap: it is advisory, and several screen reader / browser
    pairings never speak it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[dict] = []
        self.buttons: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag != "button":
            return
        a = dict(attrs)
        self._stack.append(
            {
                "text": "",
                "aria": bool(a.get("aria-label") or a.get("aria-labelledby")),
                "title": a.get("title") or "",
            }
        )

    def handle_data(self, data):
        if self._stack:
            self._stack[-1]["text"] += data

    def handle_endtag(self, tag):
        if tag == "button" and self._stack:
            self.buttons.append(self._stack.pop())


@pytest.mark.parametrize("page", PAGES)
def test_every_control_has_an_accessible_name(http, page):
    """Form controls and buttons alike -- one question, two element families.

    Deliberately one test rather than two, because it is one invariant and each
    parametrized case costs a full schema rebuild plus a login: splitting it
    would double 32 of the slowest tests in this file to say the same thing.
    """
    resp = http.get(page)
    assert resp.status_code == 200, f"{page} did not render: {resp.text[:300]}"

    parsed = _parse(resp.text)
    unlabelled = [
        c
        for c in parsed.controls
        if not c["wrapped"]
        and not c["aria"]
        and not (c["id"] and c["id"] in parsed.label_for)
    ]
    assert not unlabelled, (
        f"{page} has form control(s) with no accessible name:\n  "
        + "\n  ".join(f"<{c['tag']} name={c['name']}>" for c in unlabelled)
        + "\n\nGive it a wrapping <label> (use the `field` macro in "
        "_components.html), a label[for] pointing at its id, or -- for controls "
        "inside a table row, where a visible label would duplicate the column "
        "header -- an aria-label. A placeholder is not an accessible name."
    )

    buttons = _Buttons()
    buttons.feed(resp.text)
    nameless = [
        b for b in buttons.buttons if not b["aria"] and _GLYPHS.match(b["text"].strip())
    ]
    assert not nameless, (
        f"{page} has button(s) whose visible text says nothing when spoken:\n  "
        + "\n  ".join(
            f"<button title={b['title']!r}>{b['text'].strip()!r}</button>"
            for b in nameless
        )
        + "\n\nAdd an aria-label naming the action AND its row "
        '("Remove Dana Reyes from Accounting"). title= is advisory and several '
        "screen readers never speak it."
    )


def test_login_page_controls_are_labelled(db):
    """The login form renders before any session exists, so it needs its own check."""
    client = TestClient(app)
    parsed = _parse(client.get("/login").text)
    unlabelled = [
        c for c in parsed.controls if not c["wrapped"] and not c["aria"] and not c["id"]
    ]
    assert not unlabelled, [c["name"] for c in unlabelled]


# --------------------------------------------------------------------------
# Focus visibility.
#
# "Every interactive control carries a visible focus ring" is stated in CLAUDE.md
# as an enforced convention, and until now nothing enforced it -- the word
# "focus" did not appear anywhere in this file. Keyboard operation therefore
# depended on whatever the browser drew by default, which on this app's
# slate-900 and coloured buttons is routinely invisible.
#
# Two assertions, because the convention has two halves and neither implies the
# other:
#
#   * The component layer emits the ring. `_components.html` builds it once into
#     a `_focus` variable and interpolates it into every interactive macro, so
#     deleting that one interpolation strips the ring from several hundred
#     controls at once and nothing else in the suite would notice.
#   * Pages do not hand-roll focus-less controls. Asserted per page, because a
#     control that skips the macros is exactly the one that loses the ring.
#
# The second is a ratchet over the pages that are clean TODAY, not over all of
# them, and that is a deliberate, stated limitation rather than an oversight:
# the remaining pages carry 318 keyboard-reachable elements with no focus style
# -- 124 of them a single hand-rolled input class string repeated across the
# settings tabs. Fixing those is a restyle of a dozen templates, which is a
# different change from adding the missing assertion; making the whole list
# strict now would just mean 24 red tests nobody can land. Move a page into this
# list as it is cleaned up; never move one out.
# --------------------------------------------------------------------------

#: Either spelling counts: the macros use `focus-visible` so a mouse click leaves
#: no ring behind, while the skip link uses plain `focus:` on purpose -- it has
#: to appear for a programmatic focus too, not only a keyboard-initiated one.
_FOCUS_STYLE = re.compile(r"focus(-visible)?:(ring|outline|border|not-sr-only|shadow)")

#: What the *macros* must emit, which is stricter than what a page-level element
#: is checked for -- and the difference is not pedantry. `input_cls` carries its
#: own `focus:border-sky-500` alongside the shared `_focus`, so under the loose
#: pattern above, deleting `{{ _focus }}` from it leaves the assertion passing on
#: the strength of a border-colour change: no ring, and four of the thirteen
#: macro cases below silently stop testing anything. Measured, by mutation, not
#: assumed. The component layer is the one place the ring is defined, so that is
#: the one place it is checked by name.
_SHARED_RING = re.compile(r"focus-visible:ring")

#: Types that are not keyboard-reachable, so nothing is ever focused on them.
_UNFOCUSABLE = {"hidden"}

FOCUS_CLEAN_PAGES = [
    f"/printers/{PRINTER_ID}",
    "/alerts",
    "/account",
    "/manage/audit",
    "/manage/events",
    "/manage/definitions",
    "/security/posture",
    "/admin/backup",
]


class _Focusable(HTMLParser):
    """Collect keyboard-reachable elements and whether they style their focus."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in CONTROL_TAGS or tag == "button":
            if (a.get("type") or "").lower() in _UNFOCUSABLE:
                return
        elif not (tag == "a" and a.get("href")):
            return
        if "disabled" in a:
            return
        self.items.append(
            {
                "tag": tag,
                "cls": a.get("class") or "",
                "what": a.get("name") or a.get("href") or a.get("type") or "",
            }
        )


#: Every macro in `_components.html` that renders something a keyboard can land
#: on, with arguments that reach each distinct branch. `btn`'s variants are
#: enumerated because the ring is interpolated per variant: dropping it from one
#: is a one-token edit that leaves the other six looking fine.
_INTERACTIVE_MACROS = [
    ("btn", ("primary",)),
    ("btn", ("accent",)),
    ("btn", ("danger",)),
    ("btn", ("warning",)),
    ("btn", ("success",)),
    ("btn", ("neutral",)),
    ("btn", ("subtle",)),
    ("btn_link", ()),
    ("input_cls", ()),
    ("input_sm_cls", ()),
    ("field", ("q", "Search")),
    ("checkbox_field", ("on", "Enabled")),
    ("textarea_field", ("notes", "Notes")),
]


@pytest.mark.parametrize("macro,args", _INTERACTIVE_MACROS)
def test_component_macros_emit_a_focus_ring(macro, args):
    """The one place the ring is defined must keep defining it.

    Rendered through the app's real Jinja environment rather than grepped: the
    ring reaches `btn` as `{{ _focus }}` inside an interpolated class string, and
    only the rendered output says whether it survived the trip.
    """
    from central.dashboard.templating import templates

    components = templates.env.get_template("_components.html").make_module()
    out = str(getattr(components, macro)(*args))
    assert _SHARED_RING.search(out), (
        f"_components.html: {macro}{args} renders no focus ring.\n{out.strip()}\n\n"
        "Every interactive macro interpolates the shared `_focus` variable. "
        "Without it the control falls back to the browser's default ring, which "
        "is invisible on this app's dark and coloured buttons. A `focus:border-*` "
        "recolour is not a substitute and does not satisfy this assertion."
    )


@pytest.mark.parametrize("page", FOCUS_CLEAN_PAGES)
def test_interactive_controls_have_a_focus_ring(http, page):
    """No keyboard-reachable control on these pages falls back to the default ring.

    Asserted on the rendered DOM for the same reason everything else here is:
    the classes arrive from `_components.html` through several layers of macro
    interpolation, and only the output says whether they actually landed on the
    element.
    """
    parsed = _Focusable()
    parsed.feed(http.get(page).text)
    assert parsed.items, f"{page} rendered nothing focusable; the selector needs updating"
    missing = [i for i in parsed.items if not _FOCUS_STYLE.search(i["cls"])]
    assert not missing, (
        f"{page} has keyboard-reachable element(s) with no focus style:\n  "
        + "\n  ".join(f"<{i['tag']} {i['what']}> class={i['cls'].strip()!r}" for i in missing)
        + "\n\nUse the macros in _components.html (btn / btn_link / input_cls / "
        "field), which all emit the shared focus-visible ring. A control that "
        "hand-rolls its classes gets whatever the browser draws by default, "
        "which is invisible on this app's dark and coloured buttons."
    )


@pytest.mark.parametrize(
    "page,expected",
    [
        ("/", "/"),
        ("/printers", "/printers"),
        ("/alerts", "/alerts"),
        ("/manage", "/manage"),
        # Longest-prefix wins: /manage/agents must mark Agents, not Manage.
        ("/manage/agents", "/manage/agents"),
        ("/manage/users", "/manage/users"),
        ("/manage/audit", "/manage/audit"),
    ],
)
def test_nav_marks_the_current_page(http, page, expected):
    parsed = _parse(http.get(page).text)
    assert expected in parsed.aria_current, (
        f"{page} should mark {expected} with aria-current; "
        f"got {parsed.aria_current!r}. Without it there is no indication of "
        "which page you are on."
    )


def test_nav_marks_exactly_one_destination(http):
    """Two highlighted links is as uninformative as none."""
    parsed = _parse(http.get("/manage/agents").text)
    assert len(parsed.aria_current) == 1, parsed.aria_current


def test_skip_link_and_landmarks_present(http):
    """Keyboard users need a way past 13 nav links to the content."""
    html = http.get("/").text
    assert 'href="#main"' in html, "skip-to-content link missing"
    assert 'id="main"' in html, "main landmark missing"
    assert '<nav aria-label="Primary"' in html, "nav landmark is unnamed"


# --------------------------------------------------------------------------
# Layout containment.
#
# Two class names carry load-bearing behaviour that is invisible in review and
# only shows up as a sideways-scrolling page on a narrow viewport. Both were
# real defects found by driving Chromium at 375px, and both are one token away
# from silently coming back.
# --------------------------------------------------------------------------

#: Elements that never wrap anything, so they never open a scope on the stack.
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _Scrollers(HTMLParser):
    """Horizontal scroll containers, and whether each one actually wraps a table.

    A regex cannot answer the second half, which is why the previous version of
    this check could not be pointed at the pages that needed it most. Its
    selector was any class containing `overflow-x-auto`, which on /manage/agents
    matches five <pre> diagnostic blocks: they scroll sideways on purpose,
    contain no absolutely-positioned descendants, and have no business being
    containing blocks. Adding the page produced a failure that was real-looking
    and wrong -- and an assertion that cries wolf is one somebody deletes.

    Nesting is why this tracks a stack rather than "is a <table> anywhere after
    the tag": the scrollers here are cards and inner divs, and a table inside one
    is inside every scroller enclosing it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[dict] = []
        self.wrappers: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            for frame in self._stack:
                frame["table"] = True
        if tag in _VOID_TAGS:
            return
        cls = dict(attrs).get("class") or ""
        self._stack.append(
            {"tag": tag, "cls": cls, "scroll": "overflow-x-auto" in cls.split(),
             "table": False}
        )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag):
        # Pop back to the matching open tag rather than assuming balance: an
        # unclosed <p> or <li> anywhere in a template must not shift every
        # scroller after it onto the wrong frame.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                for frame in self._stack[i:]:
                    if frame["scroll"] and frame["table"]:
                        self.wrappers.append(frame["cls"])
                del self._stack[i:]
                return


def test_scroller_selector_ignores_code_blocks():
    """The selector must tell a table scroller from a scrolling <pre>.

    Checked on a literal fragment rather than on a page, because the five <pre>
    install commands on /manage/agents render only in the moment after a key or
    claim code is minted -- so no fixture reaches them, and the false positive
    they caused would come back unobserved. This is the shape they have.
    """
    parsed = _Scrollers()
    parsed.feed(
        '<pre class="rounded p-3 text-xs overflow-x-auto select-all">curl …</pre>'
        '<div class="relative overflow-x-auto"><table><tr><td>x</td></tr></table></div>'
        '<div class="overflow-x-auto"><p>prose, no table</p></div>'
    )
    assert parsed.wrappers == ["relative overflow-x-auto"]


@pytest.mark.parametrize(
    "page",
    ["/manage/users", "/manage/audit", "/manage/agents", "/manage/people",
     "/manage/maintenance", "/manage/suppression", "/manage/machines",
     "/alerts", "/printers"],
)
def test_table_scrollers_are_positioned(http, page):
    """Every table scroll container must also be a containing block.

    Tailwind's `sr-only` is `position: absolute`. Absolutely-positioned elements
    resolve against the nearest *positioned* ancestor -- with none, that is the
    viewport, so they are not clipped by an `overflow-x-auto` wrapper and their
    offset lands in the page's scroll width. A 1px hidden <span> naming an
    actions column pushed /manage/users 60px wide at phone width while the table
    itself was correctly contained.
    """
    resp = http.get(page)
    assert resp.status_code == 200
    parsed = _Scrollers()
    parsed.feed(resp.text)
    assert parsed.wrappers, f"{page} rendered no table scroller; the selector needs updating"
    for wrapper in parsed.wrappers:
        assert "relative" in wrapper.split(), (
            f"{page}: table wrapper '{wrapper.strip()}' is not a containing block. "
            "Absolutely-positioned descendants (sr-only text) will escape it and "
            "widen the page. Use the table_wrap() macro."
        )


@pytest.mark.parametrize("page", ["/manage/users", "/", "/manage/people", "/printers"])
def test_cards_can_shrink_below_their_content(http, page):
    """Cards must carry min-w-0 so a grid can shrink them.

    Grid and flex items default to `min-width: auto` and refuse to shrink below
    their content's intrinsic width. A card holding a wide table therefore grows
    to the table's full width and takes the page with it -- and any
    `overflow-x-auto` inside it never scrolls, because it is handed all the
    width it asked for. /manage/agents overflowed 401px this way.
    """
    html = http.get(page).text
    cards = re.findall(r'class="([^"]*bg-white rounded-lg shadow[^"]*)"', html)
    assert cards, f"{page} rendered no cards; the selector needs updating"
    for card in cards:
        assert "min-w-0" in card, (
            f"{page}: card '{card.strip()}' lacks min-w-0 and cannot shrink "
            "inside a grid. Use the card_cls() macro."
        )
