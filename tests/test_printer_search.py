"""Fleet-wide printer list and search (/printers).

The gap: every printer listing in this app was reachable only by first choosing
a client, so a tech handed "10.4.7.23 is jamming" had to already know whose it
was. These tests pin the four things that make the fix trustworthy rather than
merely present:

* it matches on every identifier a caller actually quotes, partially and
  case-insensitively, **identically on SQLite and Postgres** -- the dialects
  differ on LIKE case-sensitivity and this is exactly where that bites;
* the term is data, not syntax: LIKE's own wildcards typed by an operator match
  themselves instead of silently returning the whole fleet;
* the tenant scope is in the SQL, so a client_readonly user cannot reach another
  customer's device by any term;
* it is bounded and paginated from the first commit, and paging is a strict
  total order -- an OFFSET over a non-unique sort repeats rows and skips others.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import queries
from central.main import app
from central.security import hash_password

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "central" / "dashboard" / "templates" / "printer_search.html"
)


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


def _printer(db, client, site, ip: str, **kw) -> m.Printer:
    kw.setdefault("discovery_state", m.DiscoveryState.approved)
    printer = m.Printer(client_id=client.id, site_id=site.id, ip=ip, **kw)
    db.add(printer)
    db.flush()
    return printer


@pytest.fixture()
def fleet(db):
    """Two tenants whose devices deliberately share substrings.

    Acme and Beta both run an "M428" and both sit on a 10.4.7.x subnet, so a
    term that finds one tenant's printer necessarily also matches the other's.
    A tenancy test against data where the terms happen not to collide proves
    nothing.
    """
    acme, acme_hq = _client_with_site(db, "Acme")
    beta, beta_hq = _client_with_site(db, "Beta")
    rows = {
        "acme_front": _printer(
            db, acme, acme_hq, "10.4.7.23",
            hostname="acme-front-desk.local", serial="CN12X4Y99",
            asset_tag="AST-0042", brand="HP", model="LaserJet MFP M428fdw",
            display_name="Front Desk", status=m.PrinterStatus.warning,
        ),
        "acme_lab": _printer(
            db, acme, acme_hq, "10.4.7.99",
            hostname="acme-lab.local", serial="U63999TEST",
            asset_tag="AST-0100", brand="Brother", model="MFC-L8900CDW",
            display_name="Lab Copier",
        ),
        "acme_pending": _printer(
            db, acme, acme_hq, "10.4.7.150",
            model="LaserJet MFP M428fdw",
            discovery_state=m.DiscoveryState.pending,
        ),
        "beta_desk": _printer(
            db, beta, beta_hq, "10.4.7.24",
            hostname="beta-reception.local", serial="ZZ88BETA1",
            asset_tag="AST-9001", brand="HP", model="LaserJet MFP M428fdn",
            display_name="Beta Reception",
        ),
    }
    db.commit()
    return {"acme": acme, "beta": beta, "printers": rows}


def _login(db, username: str, role: m.UserRole, client_id=None) -> TestClient:
    db.add(m.User(
        username=username, password_hash=hash_password("pw"),
        role=role, client_id=client_id,
    ))
    db.commit()
    http = TestClient(app)
    resp = http.post(
        "/login", data={"username": username, "password": "pw"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return http


def _labels(result) -> set:
    return {row["printer"].ip for row in result["rows"]}


# --------------------------------------------------------------------------- #
# What it matches
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "term,expected_ip",
    [
        # The audit's own example: a partial IP, the identifier a caller quotes.
        ("10.4.7.23", "10.4.7.23"),
        ("front-desk", "10.4.7.23"),      # hostname fragment
        ("12X4Y", "10.4.7.23"),           # serial fragment
        ("AST-0042", "10.4.7.23"),        # asset tag
        ("M428fdw", "10.4.7.23"),         # model fragment
        ("Front Desk", "10.4.7.23"),      # operator's friendly name
        ("MFC-L8900", "10.4.7.99"),       # a different device, by model
        ("Brother", "10.4.7.99"),         # brand
    ],
)
def test_matches_every_searchable_identifier(db, fleet, term, expected_ip):
    result = queries.search_printers(db, q=term)
    assert expected_ip in _labels(result), (
        f"{term!r} did not find {expected_ip}; got {_labels(result)}"
    )


def test_search_is_case_insensitive_in_both_directions(db, fleet):
    """The one thing that genuinely differs between the backends.

    Postgres LIKE is case-SENSITIVE, SQLite's is not; ``ilike`` is what makes
    them agree. A plain ``.like()`` here would pass every SQLite run and fail
    silently on the only deployment that matters.
    """
    assert "10.4.7.99" in _labels(queries.search_printers(db, q="brother"))
    assert "10.4.7.99" in _labels(queries.search_printers(db, q="BROTHER"))
    assert "10.4.7.99" in _labels(queries.search_printers(db, q="BrOtHeR"))
    # ...and on the operator-typed side too, where the stored value is lowercase.
    assert "10.4.7.23" in _labels(queries.search_printers(db, q="ACME-FRONT"))


def test_no_term_lists_the_whole_fleet(db, fleet):
    result = queries.search_printers(db)
    assert result["total"] == 4
    assert _labels(result) == {"10.4.7.23", "10.4.7.99", "10.4.7.150", "10.4.7.24"}


def test_term_matching_two_tenants_returns_both_for_staff(db, fleet):
    """M428 is on an Acme device and a Beta device; staff must see both."""
    result = queries.search_printers(db, q="M428")
    assert _labels(result) == {"10.4.7.23", "10.4.7.150", "10.4.7.24"}


def test_unmatched_term_returns_nothing_rather_than_everything(db, fleet):
    result = queries.search_printers(db, q="no-such-device")
    assert result["total"] == 0
    assert result["rows"] == []


# --------------------------------------------------------------------------- #
# The term is data, not syntax
# --------------------------------------------------------------------------- #
def test_like_wildcards_typed_by_an_operator_match_themselves(db, fleet):
    """``%`` and ``_`` are LIKE syntax. Unescaped, a search for "_" returns the
    entire fleet -- which reads as "the search is broken" only if you already
    know what you were looking for."""
    assert queries.search_printers(db, q="%")["total"] == 0
    assert queries.search_printers(db, q="_")["total"] == 0
    assert queries.search_printers(db, q="%M428%")["total"] == 0


def test_a_literal_underscore_is_findable(db, fleet):
    """Escaping must not make a real underscore unsearchable: serials contain
    them, and a device named "HP_LJ" has to be findable by that exact string."""
    acme = fleet["acme"]
    site_id = fleet["printers"]["acme_front"].site_id
    db.add(m.Printer(
        client_id=acme.id, site_id=site_id, ip="10.4.7.201",
        display_name="HP_LJ_Back_Office",
        discovery_state=m.DiscoveryState.approved,
    ))
    db.commit()
    assert _labels(queries.search_printers(db, q="HP_LJ")) == {"10.4.7.201"}
    # A single-character wildcard would also have matched "HPXLJ"; it must not
    # match anything else here, and "HP-LJ" must not match the underscored name.
    assert queries.search_printers(db, q="HP-LJ")["total"] == 0


def test_backslash_in_a_term_is_inert(db, fleet):
    """The escape character itself, typed by a user, must not corrupt the
    pattern -- on Postgres a dangling escape at the end of a LIKE pattern is an
    error, not an empty result."""
    assert queries.search_printers(db, q="\\")["total"] == 0
    assert queries.search_printers(db, q="M428\\")["total"] == 0
    assert queries.search_printers(db, q="\\%")["total"] == 0


def test_long_terms_are_capped(db, fleet):
    result = queries.search_printers(db, q="x" * 5000)
    assert len(result["q"]) == queries.MAX_SEARCH_TERM
    assert result["total"] == 0


def test_whitespace_only_term_is_treated_as_no_term(db, fleet):
    result = queries.search_printers(db, q="   ")
    assert result["q"] == ""
    assert result["total"] == 4


# --------------------------------------------------------------------------- #
# Tenancy -- the security requirement
# --------------------------------------------------------------------------- #
def test_client_scope_is_applied_in_sql(db, fleet):
    result = queries.search_printers(db, q="M428", client_id=fleet["acme"].id)
    assert _labels(result) == {"10.4.7.23", "10.4.7.150"}
    assert result["total"] == 2, "total must count the scoped set, not the fleet"


def test_readonly_user_cannot_see_another_tenants_printer_over_http(db, fleet):
    """The end-to-end version of the above: the same term that shows a staff
    user Beta's reception printer shows the Acme customer nothing of Beta's."""
    beta_ip = "10.4.7.24"
    staff = _login(db, "tech1", m.UserRole.tech)
    assert beta_ip in staff.get("/printers?q=M428").text

    readonly = _login(db, "acme-user", m.UserRole.client_readonly,
                      client_id=fleet["acme"].id)
    body = readonly.get("/printers?q=M428").text
    assert body.count(beta_ip) == 0, "a readonly user reached another tenant's device"
    assert "Beta Reception" not in body
    assert "ZZ88BETA1" not in body
    assert "10.4.7.23" in body, "the readonly user lost their own matching device"


@pytest.mark.parametrize("term", ["Beta", "ZZ88BETA1", "AST-9001", "beta-reception",
                                  "M428fdn", "10.4.7.24"])
def test_no_term_reaches_across_the_tenant_boundary(db, fleet, term):
    """Every identifier of Beta's printer, tried against Acme's scope."""
    result = queries.search_printers(db, q=term, client_id=fleet["acme"].id)
    assert result["total"] == 0, f"{term!r} leaked across tenants: {_labels(result)}"


def test_readonly_user_sees_only_approved_devices(db, fleet):
    """A pending device is not yet part of the customer's fleet. Staff see it
    (that is when "I can't find 10.4.7.150" is most likely); the customer does
    not."""
    readonly = _login(db, "acme-user", m.UserRole.client_readonly,
                      client_id=fleet["acme"].id)
    body = readonly.get("/printers?q=M428").text
    assert "10.4.7.150" not in body

    staff = _login(db, "tech1", m.UserRole.tech)
    staff_body = staff.get("/printers?q=M428").text
    assert "10.4.7.150" in staff_body
    assert "pending" in staff_body


def test_readonly_user_without_a_client_gets_nothing(db, fleet):
    """A client_readonly account with no client_id is a misconfiguration, not a
    tenant. It must fail closed rather than fall through to the whole fleet."""
    orphan = _login(db, "orphan", m.UserRole.client_readonly, client_id=None)
    resp = orphan.get("/printers", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"


def test_anonymous_users_are_sent_to_login(db, fleet):
    resp = TestClient(app).get("/printers", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_readonly_user_cannot_widen_scope_with_a_query_parameter(db, fleet):
    """The scope comes from the session, never from the request."""
    readonly = _login(db, "acme-user", m.UserRole.client_readonly,
                      client_id=fleet["acme"].id)
    for attempt in (
        f"/printers?q=M428&client_id={fleet['beta'].id}",
        "/printers?q=M428&states=pending",
        "/printers?q=M428&per_page=1000",
    ):
        body = readonly.get(attempt).text
        assert "10.4.7.24" not in body, attempt
        assert "10.4.7.150" not in body, attempt


# --------------------------------------------------------------------------- #
# Bounded and paginated from the start
# --------------------------------------------------------------------------- #
@pytest.fixture()
def big_fleet(db):
    client, site = _client_with_site(db, "Bulk")
    for n in range(1, 121):
        _printer(db, client, site, f"10.9.{n // 256}.{n % 256}",
                 serial=f"SER{n:04d}", model="Bulk Model")
    db.commit()
    return client


def test_a_page_is_bounded(db, big_fleet):
    result = queries.search_printers(db)
    assert result["total"] == 120
    assert len(result["rows"]) == queries.PRINTER_SEARCH_PAGE_SIZE == 50
    assert result["pages"] == 3


def test_every_row_is_reachable_exactly_once(db, big_fleet):
    """The audit's finding was an audit log capped at 200 rows with no offset,
    making 99,800 of 100k rows permanently unreachable. Walking the pages must
    enumerate the whole set, with no row seen twice."""
    seen = []
    for page in range(1, 4):
        seen.extend(r["printer"].id for r in
                    queries.search_printers(db, page=page)["rows"])
    assert len(seen) == 120
    assert len(set(seen)) == 120, "OFFSET paging repeated a row"


def test_paging_is_stable_because_the_sort_is_total(db, big_fleet):
    """Every row here shares client, site and a sortable IP prefix, so only the
    id tiebreaker makes the order strict. Without it the same query can return
    ties in a different order per call and paging silently drops rows."""
    first = [r["printer"].id for r in queries.search_printers(db, page=2)["rows"]]
    again = [r["printer"].id for r in queries.search_printers(db, page=2)["rows"]]
    assert first == again


@pytest.mark.parametrize("page,expected", [(0, 1), (-5, 1), (99, 3), (2, 2)])
def test_page_numbers_are_clamped_to_a_real_page(db, big_fleet, page, expected):
    assert queries.search_printers(db, page=page)["page"] == expected


def test_per_page_cannot_be_used_to_dump_the_table(db, big_fleet):
    wide = queries.search_printers(db, per_page=10_000)
    assert wide["per_page"] == 200, "per_page must be clamped, not honoured"
    assert len(wide["rows"]) == 120  # the whole (small) fleet, under the cap
    assert queries.search_printers(db, per_page=0)["per_page"] == 50
    assert queries.search_printers(db, per_page=-3)["per_page"] == 1


def test_pagination_controls_render_and_carry_the_term(db, big_fleet):
    staff = _login(db, "tech1", m.UserRole.tech)
    body = staff.get("/printers?q=Bulk").text
    assert "Page 1 of 3" in body
    assert 'href="/printers?q=Bulk&amp;page=2"' in body
    assert "Showing 1–50 of 120" in body
    page2 = staff.get("/printers?q=Bulk&page=2").text
    assert "Showing 51–100 of 120" in page2
    assert 'href="/printers?q=Bulk&amp;page=1"' in page2


def test_a_hostile_page_parameter_does_not_500(db, big_fleet):
    staff = _login(db, "tech1", m.UserRole.tech)
    for bad in ("abc", "", "9999999999999999999999", "-1", "1;DROP TABLE printers"):
        resp = staff.get(f"/printers?page={bad}")
        assert resp.status_code == 200, bad
    assert db.query(m.Printer).count() == 120


# --------------------------------------------------------------------------- #
# Device-controlled strings render inert
# --------------------------------------------------------------------------- #
def test_hostile_device_strings_render_escaped(db, fleet):
    """model / hostname / serial come off the wire from a printer on a customer
    LAN. A device that answers sysDescr with markup must not be able to put a
    script into an operator's browser."""
    acme = fleet["acme"]
    site_id = fleet["printers"]["acme_front"].site_id
    db.add(m.Printer(
        client_id=acme.id, site_id=site_id, ip="10.4.7.202",
        hostname='<img src=x onerror="alert(1)">',
        model='<script>alert("xss")</script>',
        serial='" onmouseover="alert(2)',
        display_name="<b>bold</b>",
        discovery_state=m.DiscoveryState.approved,
    ))
    db.commit()

    staff = _login(db, "tech1", m.UserRole.tech)
    body = staff.get("/printers?q=10.4.7.202").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;alert" in body
    # The point is not that the *text* "onerror" is absent -- it is text now --
    # but that no live tag was produced to hang it on.
    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=" in body
    assert "<b>bold</b>" not in body
    assert "&lt;b&gt;bold&lt;/b&gt;" in body
    # The serial's quote must not escape the attribute it might land in.
    assert 'onmouseover="alert(2)' not in body


def test_a_hostile_search_term_renders_inert(db, fleet):
    """The term is echoed back into the input's value and into the heading."""
    staff = _login(db, "tech1", m.UserRole.tech)
    body = staff.get('/printers?q=%3Cscript%3Ealert(1)%3C/script%3E').text
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)" in body


def test_template_never_disables_autoescaping():
    """A single |safe would undo every assertion above, and would be invisible
    in a rendered-output test that happened not to hit that field."""
    source = TEMPLATE.read_text(encoding="utf-8")
    for danger in ("|safe", "| safe", "autoescape false"):
        assert danger not in source, f"{danger} in printer_search.html"


# --------------------------------------------------------------------------- #
# The page itself
# --------------------------------------------------------------------------- #
def test_page_lists_the_fleet_with_useful_columns(db, fleet):
    staff = _login(db, "tech1", m.UserRole.tech)
    body = staff.get("/printers").text
    assert "Front Desk" in body          # friendly name
    assert "10.4.7.23" in body           # ip
    assert "Acme" in body and "Beta" in body   # client column, fleet-wide
    assert "Acme HQ" in body             # site column
    assert "CN12X4Y99" in body           # serial
    assert "AST-0042" in body            # asset tag


def test_client_column_is_absent_for_a_single_tenant_view(db, fleet):
    """A customer looking at their own fleet does not need a column that says
    their own name on every row."""
    readonly = _login(db, "acme-user", m.UserRole.client_readonly,
                      client_id=fleet["acme"].id)
    body = readonly.get("/printers").text
    assert "<th" in body
    assert ">Client<" not in body
    assert ">Site<" in body


def test_nav_links_to_the_search_for_every_role(db, fleet):
    staff = _login(db, "tech1", m.UserRole.tech)
    assert 'href="/printers"' in staff.get("/").text
    readonly = _login(db, "acme-user", m.UserRole.client_readonly,
                      client_id=fleet["acme"].id)
    assert 'href="/printers"' in readonly.get("/printers").text


def test_printer_detail_still_resolves_under_the_same_prefix(db, fleet):
    """/printers and /printers/<id> are different routes; adding the first must
    not shadow the second."""
    staff = _login(db, "tech1", m.UserRole.tech)
    printer_id = fleet["printers"]["acme_front"].id
    resp = staff.get(f"/printers/{printer_id}")
    assert resp.status_code == 200
    assert "Front Desk" in resp.text


def test_empty_fleet_says_so(db):
    staff = _login(db, "tech1", m.UserRole.tech)
    body = staff.get("/printers").text
    assert "No printers yet" in body
