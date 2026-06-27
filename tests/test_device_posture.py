"""Device security-posture reporting: insecure-SNMP flagging, firmware capture,
tenant scoping, fleet summary, and the operator-facing dashboard view."""

from __future__ import annotations

from fastapi.testclient import TestClient

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

    # Subnets carry the SNMP creds. Acme = v2c (insecure), Beta = v3 (secure).
    db.add(m.Subnet(site_id=acme_hq.id, cidr="10.0.0.0/24", label="Acme LAN",
                    snmp_version="2c"))
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
def test_insecure_snmp_flagged_from_subnet_config(db):
    _seed_two_clients(db)
    rollup = queries.security_posture_rollup(db)
    by_ip = {r["printer"].ip: r for r in rollup["rows"]}

    # v2c subnet -> insecure, flagged.
    assert by_ip["10.0.0.10"]["snmp_version"] == "2c"
    assert by_ip["10.0.0.10"]["snmp_secure"] is False
    assert "insecure-snmp" in by_ip["10.0.0.10"]["flags"]
    assert by_ip["10.0.0.10"]["snmp_source"] == "Acme LAN"
    assert by_ip["10.0.0.11"]["snmp_secure"] is False

    # v3 subnet -> secure, NOT flagged insecure.
    assert by_ip["10.0.1.10"]["snmp_version"] == "3"
    assert by_ip["10.0.1.10"]["snmp_secure"] is True
    assert "insecure-snmp" not in by_ip["10.0.1.10"]["flags"]

    # Pending device is excluded entirely.
    assert "10.0.1.99" not in by_ip


def test_firmware_unknown_surfaced_honestly(db):
    _seed_two_clients(db)
    rollup = queries.security_posture_rollup(db)
    by_ip = {r["printer"].ip: r for r in rollup["rows"]}

    assert by_ip["10.0.0.10"]["firmware"] == "FW3.21"
    assert by_ip["10.0.0.10"]["firmware_known"] is True
    # No firmware -> None + flag, never a fabricated value.
    assert by_ip["10.0.0.11"]["firmware"] is None
    assert by_ip["10.0.0.11"]["firmware_known"] is False
    assert "firmware-unknown" in by_ip["10.0.0.11"]["flags"]


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
    assert row["snmp_secure"] is True


# --------------------------------------------------------------------------- #
# Tenant scoping
# --------------------------------------------------------------------------- #
def test_rollup_scoped_to_client(db):
    acme, beta = _seed_two_clients(db)
    acme_only = queries.security_posture_rollup(db, client_id=acme.id)
    ips = {r["printer"].ip for r in acme_only["rows"]}
    assert ips == {"10.0.0.10", "10.0.0.11"}
    assert acme_only["summary"]["total"] == 2
    assert acme_only["summary"]["insecure_snmp"] == 2

    beta_only = queries.security_posture_rollup(db, client_id=beta.id)
    ips = {r["printer"].ip for r in beta_only["rows"]}
    assert ips == {"10.0.1.10"}
    assert beta_only["summary"]["insecure_snmp"] == 0
    assert beta_only["summary"]["secure_snmp"] == 1


# --------------------------------------------------------------------------- #
# Fleet summary counts
# --------------------------------------------------------------------------- #
def test_fleet_summary_counts(db):
    _seed_two_clients(db)
    summary = queries.security_posture_rollup(db)["summary"]
    assert summary["total"] == 3           # two Acme + one Beta (pending excluded)
    assert summary["insecure_snmp"] == 2   # both Acme v2c devices
    assert summary["secure_snmp"] == 1     # the Beta v3 device
    assert summary["firmware_unknown"] == 1
    assert summary["firmware_known"] == 2
    # Acme a1 has firmware (insecure only), a2 insecure+fw-unknown, Beta clean.
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
    assert "Insecure SNMP" in body
    # An insecure-SNMP flag appears for the v2c devices.
    assert "insecure-snmp" in body
    # The known firmware string for the v2c device is rendered.
    assert "FW3.21" in body
    # The honest "unknown" firmware label renders for the device without one.
    assert "unknown" in body


def test_security_posture_redirects_client_readonly(db):
    acme, _ = _seed_two_clients(db)
    db.add(m.User(
        username="reader", password_hash=hash_password("pw"),
        role=m.UserRole.client_readonly, client_id=acme.id,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "reader", "password": "pw"},
             follow_redirects=False)
    resp = cli.get("/security/posture", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


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
