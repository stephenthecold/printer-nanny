"""Seed the database with realistic demo data so the whole system is demoable
without a live agent. Run: ``python -m central.seed`` (drops & recreates tables).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from central import models as m
from central.db import Base, SessionLocal, engine
from central.security import generate_api_key, hash_api_key, hash_password

RNG = random.Random(42)  # deterministic demo data


def _now() -> datetime:
    return datetime.now(timezone.utc)


BRAND_MODELS = [
    ("HP", "LaserJet Pro M404dn"),
    ("HP", "Color LaserJet M553"),
    ("Brother", "MFC-L8900CDW"),
    ("Canon", "imageRUNNER 1643i"),
    ("Xerox", "VersaLink C405"),
    ("Lexmark", "MX431adw"),
    ("Konica Minolta", "bizhub C300i"),
]

LOCATIONS = ["Front desk", "Copy room", "Finance", "Nurses station", "Warehouse", "2nd floor"]


def _supplies_for(model: str, low: bool) -> list[dict]:
    """Color devices get CMYK toners; mono devices get black + drum."""
    is_color = "Color" in model or model.endswith(("CDW", "C405", "C300i"))
    out = []
    if is_color:
        for color in ("black", "cyan", "magenta", "yellow"):
            lvl = 4.0 if (low and color == "magenta") else float(RNG.randint(25, 95))
            out.append({"type": m.SupplyType.toner, "color": color, "level_pct": lvl})
    else:
        lvl = 6.0 if low else float(RNG.randint(20, 95))
        out.append({"type": m.SupplyType.toner, "color": "black", "level_pct": lvl})
        out.append({"type": m.SupplyType.drum, "color": None, "level_pct": float(RNG.randint(40, 100))})
    return out


def seed() -> None:
    print("Dropping and recreating tables…")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    now = _now()

    # Admin + a tech user.
    db.add(m.User(username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin))
    db.add(m.User(username="tech", password_hash=hash_password("tech"), role=m.UserRole.tech))

    # Channels are configured in Settings; enable email to a demo address so the
    # Docker stack's MailHog catches alert mail. FreeScout stays off until creds
    # are entered. (See central/runtime.py.)
    from central import runtime

    runtime.save_settings(db, {
        "email.enabled": "on",
        "email.default_recipients": "ops@example.com",
        "smtp.host": "mailhog",
        "smtp.port": "1025",
    })

    clients_spec = [
        ("Northwind Health", ["Main Clinic", "Westside Annex"]),
        ("Cascade Legal", ["Downtown Office"]),
        ("Summit Logistics", ["HQ", "Distribution Center"]),
    ]

    printer_id_low = None
    octet = 10
    for cname, site_names in clients_spec:
        client = m.Client(name=cname)
        db.add(client)
        db.flush()
        for sname in site_names:
            site = m.Site(client_id=client.id, name=sname, address=f"{RNG.randint(100,999)} Main St")
            db.add(site)
            db.flush()

            # One agent per site, owning 1–2 subnets.
            api_key = generate_api_key()
            agent = m.Agent(
                site_id=site.id,
                name=f"{sname} agent",
                api_key_hash=hash_api_key(api_key),
                version="0.1.0",
                status=m.AgentStatus.online,
                last_heartbeat=now - timedelta(minutes=RNG.randint(0, 3)),
            )
            db.add(agent)
            db.flush()
            n_subnets = RNG.choice([1, 1, 2])
            for sub in range(n_subnets):
                db.add(
                    m.Subnet(
                        site_id=site.id,
                        agent_id=agent.id,
                        cidr=f"10.{octet}.{sub}.0/24",
                        label=f"VLAN {10 + sub}",
                    )
                )

            # Approved printers with history.
            n_printers = RNG.randint(2, 4)
            for i in range(n_printers):
                brand, model = RNG.choice(BRAND_MODELS)
                low = printer_id_low is None and cname == "Northwind Health" and i == 0
                printer = m.Printer(
                    client_id=client.id,
                    site_id=site.id,
                    discovered_by_agent_id=agent.id,
                    ip=f"10.{octet}.0.{20 + i}",
                    hostname=f"{brand[:3].lower()}-{octet}{i}",
                    brand=brand,
                    model=model,
                    serial=f"SN{RNG.randint(100000, 999999)}",
                    location=RNG.choice(LOCATIONS),
                    status=m.PrinterStatus.error if low else m.PrinterStatus.ok,
                    discovery_state=m.DiscoveryState.approved,
                    page_count=RNG.randint(40000, 250000),
                    last_seen=now - timedelta(minutes=RNG.randint(1, 30)),
                )
                db.add(printer)
                db.flush()
                if low:
                    printer_id_low = printer.id

                supplies = _supplies_for(model, low)
                for sp in supplies:
                    db.add(
                        m.Supply(
                            printer_id=printer.id,
                            type=sp["type"],
                            color=sp["color"],
                            level_pct=sp["level_pct"],
                            description=f"{sp['color'] or sp['type'].value} cartridge",
                        )
                    )

                # 14 days of readings: page count climbs, toner declines (for forecast).
                base_pages = printer.page_count - 14 * 80
                for d in range(14):
                    ts = now - timedelta(days=13 - d)
                    snap = []
                    for sp in supplies:
                        decline = sp["level_pct"] + (13 - d) * 1.5
                        snap.append(
                            {
                                "type": sp["type"].value,
                                "color": sp["color"],
                                "level_pct": round(min(100.0, decline), 1),
                            }
                        )
                    db.add(
                        m.Reading(
                            printer_id=printer.id,
                            ts=ts,
                            page_count=base_pages + d * 80 + RNG.randint(0, 20),
                            status=m.PrinterStatus.ok,
                            supply_snapshot=snap,
                        )
                    )

                if low:
                    db.add(
                        m.PrinterEvent(
                            printer_id=printer.id,
                            ts=now - timedelta(hours=2),
                            code="low-toner",
                            severity=m.EventSeverity.warning,
                            source=m.EventSource.snmp_alert,
                            message="Magenta toner low (4%)",
                        )
                    )
                    db.add(
                        m.PrinterEvent(
                            printer_id=printer.id,
                            ts=now - timedelta(minutes=20),
                            code="paper-jam",
                            severity=m.EventSeverity.critical,
                            source=m.EventSource.snmp_alert,
                            message="Paper jam in tray 2",
                        )
                    )

                # A maintenance record on the first printer of each site.
                if i == 0:
                    db.add(
                        m.MaintenanceRecord(
                            printer_id=printer.id,
                            type=m.MaintenanceType.scheduled,
                            performed_by="tech",
                            performed_at=now - timedelta(days=45),
                            notes="Quarterly PM: cleaned rollers, firmware update.",
                            next_due=now + timedelta(days=45),
                        )
                    )

            # A couple of pending-discovery devices per site.
            for j in range(RNG.randint(1, 2)):
                db.add(
                    m.Printer(
                        client_id=client.id,
                        site_id=site.id,
                        discovered_by_agent_id=agent.id,
                        ip=f"10.{octet}.0.{80 + j}",
                        hostname=f"unknown-{octet}{j}",
                        brand=RNG.choice(BRAND_MODELS)[0],
                        mac=f"00:1B:{RNG.randint(10,99)}:{RNG.randint(10,99)}:{RNG.randint(10,99)}:{RNG.randint(10,99)}",
                        discovery_state=m.DiscoveryState.pending,
                        status=m.PrinterStatus.unknown,
                    )
                )
            octet += 10

    # One offline agent to exercise the offline alert path.
    stray = m.Agent(
        site_id=1,
        name="Stale remote agent",
        api_key_hash=hash_api_key(generate_api_key()),
        version="0.1.0",
        status=m.AgentStatus.online,
        last_heartbeat=now - timedelta(hours=3),
    )
    db.add(stray)

    # Alert rules: low supply <10%, any error, agent offline >30 min.
    # Channels come from Settings (active_channels), so no per-rule channel_ids.
    db.add_all(
        [
            m.AlertRule(
                name="Low supply (<10%)",
                scope=m.AlertScope.global_,
                condition_type=m.AlertConditionType.supply_below,
                threshold=10,
                severity=m.EventSeverity.warning,
            ),
            m.AlertRule(
                name="Printer errors",
                scope=m.AlertScope.global_,
                condition_type=m.AlertConditionType.error_severity,
                severity=m.EventSeverity.critical,
            ),
            m.AlertRule(
                name="Agent offline (>30 min)",
                scope=m.AlertScope.global_,
                condition_type=m.AlertConditionType.offline_minutes,
                threshold=30,
                severity=m.EventSeverity.warning,
            ),
        ]
    )

    db.commit()
    counts = {
        "clients": db.query(m.Client).count(),
        "sites": db.query(m.Site).count(),
        "printers": db.query(m.Printer).count(),
        "readings": db.query(m.Reading).count(),
        "pending": db.query(m.Printer).filter_by(discovery_state=m.DiscoveryState.pending).count(),
    }
    db.close()
    print(f"Seeded: {counts}")
    print("Log in as admin / admin (local dev: http://localhost:8000, Docker: http://localhost:8080)")


if __name__ == "__main__":
    seed()
