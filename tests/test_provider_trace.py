"""Provider trace: agent records what each provider did, central stores + renders."""

from __future__ import annotations

from fastapi.testclient import TestClient

from central import models as m
from central import schemas as s
from central import services
from central.main import app
from central.security import generate_api_key, hash_api_key, hash_password
from printer_nanny_agent.providers import (
    PrinterProvider,
    register,
    run_providers,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


# ---------- run_providers records traces ----------

class _SetsBlackToner(PrinterProvider):
    name = "test_sets_black"
    enterprise_prefixes = ("9999",)

    async def augment(self, backend, ip, params, reading, sys_object_id):
        for sup in reading.get("supplies", []):
            if sup.get("color") == "black":
                sup["level_pct"] = 73.0
                sup["status_note"] = None
        reading["_supply_precision"] = "test_native"
        return reading


class _RaisesProvider(PrinterProvider):
    name = "test_raises"
    enterprise_prefixes = ("9999",)

    async def augment(self, backend, ip, params, reading, sys_object_id):
        raise RuntimeError("simulated provider failure")


async def test_run_providers_emits_trace_with_changes():
    register(_SetsBlackToner())
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining"},
            {"type": "toner", "color": "cyan", "level_pct": 50.0},
        ],
        "events": [],
    }
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    out = await run_providers(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.9999.1",
    )
    traces = out.get("provider_trace") or []
    ours = [t for t in traces if t["name"] == "test_sets_black"]
    assert len(ours) == 1
    trace = ours[0]
    assert trace["ok"] is True
    assert trace["error"] is None
    # Provider changed black's level (None -> 73%); cyan was untouched.
    assert any("black: set to 73%" in c for c in trace["changed"])
    assert not any("cyan" in c for c in trace["changed"])
    assert "test_native" in trace["summary"]


async def test_run_providers_records_exception_in_trace():
    register(_RaisesProvider())
    reading = {"supplies": [], "events": []}
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.2"] = {"scalars": {}, "walks": {}}
    out = await run_providers(
        backend, "10.0.0.2", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.9999.1",
    )
    traces = out.get("provider_trace") or []
    ours = [t for t in traces if t["name"] == "test_raises"]
    assert len(ours) == 1
    trace = ours[0]
    assert trace["ok"] is False
    assert "RuntimeError" in trace["error"]
    assert "simulated provider failure" in trace["error"]


# ---------- Central persists the trace on Printer ----------

def _seed_printer(db) -> m.Printer:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    api_key = generate_api_key()
    agent = m.Agent(site_id=site.id, name="hq-agent", api_key_hash=hash_api_key(api_key))
    db.add(agent)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5",
        discovery_state=m.DiscoveryState.approved,
        brand="Brother", model="MFC-L8900CDW",
    )
    db.add(printer)
    db.commit()
    return printer


def test_apply_reading_stores_provider_trace_on_printer(db):
    printer = _seed_printer(db)
    reading = s.ReadingIn(
        ip="10.0.0.5",
        status=m.PrinterStatus.ok,
        provider_trace=[
            {"name": "brother", "ok": True, "error": None,
             "changed": ["black: status 'low'"], "summary": "precision=brother_buckets"},
            {"name": "brother_pjl", "ok": False, "error": "OSError: connect refused",
             "changed": [], "summary": ""},
        ],
    )
    services.apply_reading(db, printer.site_id, reading)
    db.commit()
    db.refresh(printer)
    assert printer.last_provider_trace is not None
    assert len(printer.last_provider_trace) == 2
    by_name = {t["name"]: t for t in printer.last_provider_trace}
    assert by_name["brother"]["ok"] is True
    assert by_name["brother_pjl"]["ok"] is False
    assert "connect refused" in by_name["brother_pjl"]["error"]


def test_apply_reading_without_trace_leaves_existing_trace_alone(db):
    """A reading from an older agent (no provider_trace field) shouldn't wipe
    the diagnostics we already collected on a previous poll."""
    printer = _seed_printer(db)
    printer.last_provider_trace = [{"name": "brother", "ok": True, "changed": []}]
    db.commit()
    reading = s.ReadingIn(ip="10.0.0.5", status=m.PrinterStatus.ok)  # no provider_trace
    services.apply_reading(db, printer.site_id, reading)
    db.commit()
    db.refresh(printer)
    assert printer.last_provider_trace is not None
    assert printer.last_provider_trace[0]["name"] == "brother"


def test_printer_detail_page_renders_provider_trace(db):
    printer = _seed_printer(db)
    printer.last_provider_trace = [
        {"name": "brother", "ok": True, "error": None,
         "changed": ["black: status 'low'"], "summary": "precision=brother_buckets"},
        {"name": "brother_pjl", "ok": False, "error": "OSError: refused",
         "changed": [], "summary": ""},
    ]
    db.add(m.User(
        username="admin", password_hash=hash_password("pw"),
        role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    resp = cli.get(f"/printers/{printer.id}", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "Provider diagnostics" in body
    assert "brother" in body
    assert "brother_pjl" in body
    assert "OSError: refused" in body
    # Jinja autoescapes single quotes -> &#39;
    assert "black: status" in body
    assert "low" in body


def test_provider_trace_section_hidden_when_none(db):
    """Printers polled by older agents (no trace) shouldn't show an empty card."""
    printer = _seed_printer(db)
    db.add(m.User(
        username="admin", password_hash=hash_password("pw"),
        role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    resp = cli.get(f"/printers/{printer.id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Provider diagnostics" not in resp.text
