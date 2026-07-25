"""Accessibility invariants for the dashboard, checked against rendered HTML.

These assert on the real DOM rather than on template source, because template
source cannot answer the question that matters. A `<label>` that *wraps* its
control is correctly associated and needs no `for=`; a `<label>` that merely
sits next to one is not associated at all, even though both look identical in
a grep. Only the rendered tree distinguishes them.

The two invariants:

1. Every form control has an accessible name. Before this, 85 controls had only
   a `placeholder` -- which assistive tech does not treat as a name, and which
   disappears from view on the first keystroke, leaving (for instance) three
   visually identical password boxes on /account.
2. The nav marks the current page with aria-current. Thirteen identical links
   with nothing indicating which one you are on was the single most common
   complaint about the old dashboard.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password

# Controls that take no accessible name in the usual sense.
EXEMPT_TYPES = {"hidden", "submit", "button", "image", "reset"}
CONTROL_TAGS = {"input", "select", "textarea"}

# Pages rendered with only an admin + seeded-empty database. Anything needing
# fixture data beyond this is covered by its own feature test.
PAGES = [
    "/",
    "/alerts",
    "/approvals",
    "/account",
    "/manage",
    "/manage/agents",
    "/manage/users",
    "/manage/maintenance",
    "/manage/audit",
    "/manage/onboard",
    "/manage/suppression",
    "/security/posture",
    "/admin/backup",
    "/settings?group=branding",
    "/settings?group=notifications",
    "/settings?group=polling",
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


@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin))
    db.commit()
    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": "admin", "password": "admin"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return client


@pytest.mark.parametrize("page", PAGES)
def test_every_form_control_has_an_accessible_name(http, page):
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


def test_login_page_controls_are_labelled(db):
    """The login form renders before any session exists, so it needs its own check."""
    client = TestClient(app)
    parsed = _parse(client.get("/login").text)
    unlabelled = [
        c for c in parsed.controls if not c["wrapped"] and not c["aria"] and not c["id"]
    ]
    assert not unlabelled, [c["name"] for c in unlabelled]


@pytest.mark.parametrize(
    "page,expected",
    [
        ("/", "/"),
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
