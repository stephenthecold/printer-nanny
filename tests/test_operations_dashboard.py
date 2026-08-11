"""The staff dashboard is an ordered operations queue, not an activity feed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from central import models as m
from central import queries
from central.main import app
from central.security import hash_password


def _client_site(db, *, client_name="Acme", site_name="HQ"):
    client = m.Client(name=client_name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=site_name)
    db.add(site)
    db.flush()
    return client, site


def _printer(db, client, site, ip, *, status=m.PrinterStatus.ok, **values):
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=ip,
        status=status,
        discovery_state=m.DiscoveryState.approved,
        **values,
    )
    db.add(printer)
    db.flush()
    return printer


def _alert(db, printer, title, *, severity=m.EventSeverity.warning,
           condition=m.AlertConditionType.error_severity,
           state=m.AlertState.open):
    row = m.Alert(
        printer_id=printer.id,
        type=condition,
        severity=severity,
        state=state,
        title=title,
        dedupe_key=f"dashboard-test:{printer.id}:{title}",
    )
    db.add(row)
    return row


def _login_admin(db):
    db.add(
        m.User(
            username="dashboard-admin",
            password_hash=hash_password("pw"),
            role=m.UserRole.admin,
        )
    )
    db.commit()
    client = TestClient(app)
    client.post(
        "/login",
        data={"username": "dashboard-admin", "password": "pw"},
        follow_redirects=False,
    )
    return client


def test_subnet_health_requires_agent_and_recent_printer_reply(db):
    now = datetime.now(timezone.utc)
    client, site = _client_site(db)
    agent = m.Agent(
        site_id=site.id,
        name="HQ collector",
        api_key_hash="hash",
        status=m.AgentStatus.online,
        last_heartbeat=now - timedelta(seconds=20),
    )
    db.add(agent)
    db.flush()
    broad = m.Subnet(
        site_id=site.id,
        agent_id=agent.id,
        cidr="10.0.0.0/16",
        label="Campus",
        snmp_community="do-not-render",
    )
    specific = m.Subnet(
        site_id=site.id,
        agent_id=agent.id,
        cidr="10.0.1.0/24",
        label="North building",
    )
    empty = m.Subnet(
        site_id=site.id,
        agent_id=agent.id,
        cidr="10.9.0.0/24",
        label="New building",
    )
    db.add_all([broad, specific, empty])
    db.flush()
    _printer(
        db,
        client,
        site,
        "10.0.1.25",
        display_name="Reception",
        last_seen=now - timedelta(minutes=2),
    )
    _printer(
        db,
        client,
        site,
        "10.0.2.25",
        display_name="Warehouse",
        last_seen=now - timedelta(hours=2),
    )
    db.commit()

    health = queries.subnet_health(db, now=now)
    by_label = {row["subnet_label"]: row for row in health["rows"]}

    # Longest-prefix matching assigns Reception only to the /24. Its recent
    # successful poll proves that subnet end to end.
    assert by_label["North building"]["state"] == "verified"
    assert by_label["North building"]["printer_count"] == 1
    assert by_label["North building"]["last_printer_name"] == "Reception"
    # The /16 owns only the printer outside the /24, whose reply is overdue.
    assert by_label["Campus"]["state"] == "down"
    assert by_label["Campus"]["label"] == "Poll overdue"
    assert by_label["Campus"]["printer_count"] == 1
    # A live agent alone is not enough to call an empty path green.
    assert by_label["New building"]["state"] == "attention"
    assert by_label["New building"]["label"] == "Not verified"
    assert health["verified"] == 1
    assert health["down"] == 1
    assert health["attention"] == 1
    assert all("snmp_community" not in row and "snmp_v3" not in row
               for row in health["rows"])


def test_subnet_health_offline_agent_overrides_recent_printer(db):
    now = datetime.now(timezone.utc)
    client, site = _client_site(db)
    agent = m.Agent(
        site_id=site.id,
        name="Stopped collector",
        api_key_hash="hash",
        status=m.AgentStatus.offline,
        last_heartbeat=now - timedelta(hours=1),
    )
    db.add(agent)
    db.flush()
    db.add(m.Subnet(site_id=site.id, agent_id=agent.id, cidr="192.0.2.0/24"))
    _printer(
        db,
        client,
        site,
        "192.0.2.10",
        last_seen=now - timedelta(minutes=1),
    )
    db.commit()

    row = queries.subnet_health(db, now=now)["rows"][0]
    assert row["state"] == "down"
    assert row["label"] == "Agent offline"
    assert queries.subnet_health(db, now=now)["agent_issues"] == []


def test_subnet_health_surfaces_unmapped_offline_agent(db):
    now = datetime.now(timezone.utc)
    _client, site = _client_site(db)
    db.add(
        m.Agent(
            site_id=site.id,
            name="Spare collector",
            api_key_hash="hash",
            status=m.AgentStatus.offline,
            last_heartbeat=now - timedelta(hours=3),
        )
    )
    db.commit()

    health = queries.subnet_health(db, now=now)
    assert health["agent_attention"] == 1
    assert health["agent_issues"][0]["agent_name"] == "Spare collector"
    assert health["agent_issues"][0]["heartbeat_age"] == "3h"


def test_printer_issue_queue_groups_and_orders_by_impact(db):
    client, site = _client_site(db)
    stopped = _printer(
        db,
        client,
        site,
        "10.0.0.10",
        status=m.PrinterStatus.offline,
        display_name="Front desk",
        location="Lobby",
    )
    critical = _printer(db, client, site, "10.0.0.20", display_name="Accounting")
    warning = _printer(db, client, site, "10.0.0.30", display_name="Shipping")
    _alert(db, stopped, "Black toner low")
    _alert(db, critical, "Fuser failure", severity=m.EventSeverity.critical)
    _alert(db, warning, "Paper tray empty")
    _alert(db, warning, "Door open")
    _alert(db, warning, "Old resolved issue", state=m.AlertState.resolved)
    db.commit()

    queue = queries.printer_issue_queue(db)
    assert [row["printer_name"] for row in queue["rows"]] == [
        "Front desk",
        "Accounting",
        "Shipping",
    ]
    assert queue["total"] == 3
    first = queue["rows"][0]
    assert first["blocking"] is True
    assert first["impact_label"] == "Printing stopped"
    assert first["title"] == "Printer is offline"
    assert first["detail"] == "Black toner low"
    assert first["location"] == "Lobby"
    shipping = queue["rows"][2]
    assert shipping["issue_count"] == 2
    assert shipping["more_count"] == 1


def test_manual_printing_stopped_issue_is_immediately_blocking(db):
    client, site = _client_site(db)
    printer = _printer(
        db,
        client,
        site,
        "10.0.0.40",
        status=m.PrinterStatus.ok,
        display_name="Records",
    )
    _alert(
        db,
        printer,
        "Printer will not print",
        severity=m.EventSeverity.critical,
        condition=m.AlertConditionType.manual_issue,
    )
    db.commit()

    row = queries.printer_issue_queue(db)["rows"][0]
    assert row["blocking"] is True
    assert row["impact_label"] == "Printing stopped"
    assert row["title"] == "Printer will not print"


def test_dashboard_renders_three_priorities_and_escapes_device_text(db):
    now = datetime.now(timezone.utc)
    client, site = _client_site(db, client_name="LCR", site_name="Downtown")
    agent = m.Agent(
        site_id=site.id,
        name="Downtown collector",
        api_key_hash="hash",
        status=m.AgentStatus.online,
        last_heartbeat=now,
    )
    db.add(agent)
    db.flush()
    db.add(
        m.Subnet(
            site_id=site.id,
            agent_id=agent.id,
            cidr="10.20.0.0/24",
            label="Main tunnel",
        )
    )
    printer = _printer(
        db,
        client,
        site,
        "10.20.0.10",
        display_name="Reception <script>alert(1)</script>",
        model="HP M404",
        location='<img src=x onerror="alert(2)">',
        last_seen=now,
    )
    _alert(db, printer, "Tray <script>alert(3)</script> empty")
    db.add(
        m.Supply(
            printer_id=printer.id,
            type=m.SupplyType.toner,
            color="black",
            description="Black toner",
            level_pct=8,
            days_to_empty=2,
            pages_to_empty=120,
            forecast_at=now,
        )
    )
    db.commit()

    response = _login_admin(db).get("/")
    assert response.status_code == 200
    body = response.text
    first = body.index("1. System and agent status")
    second = body.index("2. Printer issues")
    third = body.index("3. Low supplies")
    assert first < second < third
    assert "Main tunnel" in body
    assert "A printer on this subnet replied successfully." in body
    assert "Printing-stopped devices appear first" in body
    assert "~2.0 days" in body
    assert "LCR · Downtown" in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert '<img src=x onerror="alert(2)">' not in body
    assert "&lt;img src=x onerror=&#34;alert(2)&#34;&gt;" in body


def test_issues_tab_orders_severity_before_recency(db):
    now = datetime.now(timezone.utc)
    client, site = _client_site(db)
    printer = _printer(db, client, site, "10.0.0.10")
    older_critical = _alert(
        db,
        printer,
        "Older critical fault",
        severity=m.EventSeverity.critical,
    )
    newer_warning = _alert(db, printer, "Newer warning")
    older_critical.created_at = now - timedelta(hours=1)
    newer_warning.created_at = now
    db.commit()

    body = _login_admin(db).get("/alerts").text
    assert body.index("Older critical fault") < body.index("Newer warning")
