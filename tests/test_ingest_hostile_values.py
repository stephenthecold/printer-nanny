"""One printer's bad value must never cost the batch -- or the spool.

Central validates a readings push as a whole, so a single field it refuses 422s
every reading in the request, including the ones from printers that reported
perfectly. That alone would be survivable. What makes it terminal is the other
half of the system: ``push_readings`` persists a failed batch to the agent's
store-and-forward spool and ``drain_spool`` replays it **verbatim** on every
cycle. A value central will always refuse is therefore a permanently blocked
spool -- nothing drains, the backlog climbs to ``max_readings``, and the cap
then drops the OLDEST entries, destroying the good readings while faithfully
retaining the poisoned one.

So these tests drive the real ingest route and the real ``ReadingSpool`` against
the real app: the property under test is the interaction between them, and a
mock on either side would assert only that the mock behaves.

The values used are the ones printers actually emit -- 255 as a vendor
"unknown", a negative sentinel, a percentage over 100, an EWS-scraped cartridge
name far longer than its column.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central.main import app
from central.security import hash_api_key, hash_password
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.runner import drain_spool, push_readings
from printer_nanny_agent.spool import ReadingSpool

API_KEY = "pn_ingest_test_key"


def _seed(db, printers=("10.0.0.10", "10.0.0.11", "10.0.0.12")):
    """One client/site/agent plus N approved printers ready to receive readings."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="HQ-Collector",
                    api_key_hash=hash_api_key(API_KEY))
    db.add(agent)
    for ip in printers:
        db.add(m.Printer(
            client_id=client.id, site_id=site.id, ip=ip,
            brand="Brother", model="MFC-L8900CDW",
            status=m.PrinterStatus.ok,
            discovery_state=m.DiscoveryState.approved,
        ))
    db.commit()
    return agent


def _post(agent_id, readings):
    cli = TestClient(app)
    return cli.post(
        f"/api/v1/agents/{agent_id}/readings",
        json={"readings": readings},
        headers={"Authorization": f"Bearer {API_KEY}"},
    )


def _reading(ip, **kw):
    base = {"ip": ip, "status": "ok", "page_count": 1000, "supplies": [], "events": []}
    base.update(kw)
    return base


def _supply(**kw):
    base = {"type": "toner", "color": "black", "description": "Black Toner"}
    base.update(kw)
    return base


def _admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"),
                  role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


# --------------------------------------------------------------------------- #
# The batch survives
# --------------------------------------------------------------------------- #
def test_one_hostile_level_does_not_reject_the_other_printers(db):
    """The headline: 255 on one cartridge must not cost two other printers."""
    agent = _seed(db)
    resp = _post(agent.id, [
        _reading("10.0.0.10", supplies=[_supply(level_pct=42)]),
        _reading("10.0.0.11", supplies=[_supply(level_pct=255)]),   # the poison
        _reading("10.0.0.12", supplies=[_supply(level_pct=7)]),
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] == 3

    levels = {
        p.ip: (p.supplies[0].level_pct if p.supplies else None)
        for p in db.scalars(select(m.Printer))
    }
    assert levels == {"10.0.0.10": 42.0, "10.0.0.11": None, "10.0.0.12": 7.0}
    # And every printer got a reading row -- the batch was applied, not merely
    # accepted with the bad one dropped.
    assert db.scalar(select(m.Reading).where(m.Reading.printer_id.is_not(None))) is not None
    assert len(list(db.scalars(select(m.Reading)))) == 3


def test_a_refused_level_is_coerced_not_clamped(db):
    """Clamping would fabricate a measurement, which is worse than none.

    255 -> 100 reports a full cartridge and suppresses the low-supply alert;
    -1 -> 0 raises a false empty. Both are lies about a device that said
    "unknown". None is the only true answer, and it is what ``status_note``
    exists to caption.
    """
    agent = _seed(db, printers=("10.0.0.10", "10.0.0.11"))
    _post(agent.id, [
        _reading("10.0.0.10", supplies=[_supply(level_pct=255)]),
        _reading("10.0.0.11", supplies=[_supply(color="cyan", level_pct=-1)]),
    ])
    supplies = {s.color: s for s in db.scalars(select(m.Supply))}
    assert supplies["black"].level_pct is None      # not 100.0
    assert supplies["cyan"].level_pct is None       # not 0.0
    assert "255" in supplies["black"].status_note
    assert "-1" in supplies["cyan"].status_note
    # The row itself survives with its identity intact -- dropping it would read
    # as "this printer stopped reporting a black cartridge".
    assert supplies["black"].description == "Black Toner"


def test_the_operator_can_see_why_the_percentage_is_missing(db):
    """A silent coercion is its own defect. The note renders on the printer page
    (where the level bar would be) and in the supplies CSV export."""
    agent = _seed(db, printers=("10.0.0.10",))
    _post(agent.id, [_reading("10.0.0.10", supplies=[_supply(level_pct=255)])])
    printer = db.scalar(select(m.Printer))

    http = _admin(db)
    body = http.get(f"/printers/{printer.id}").text
    assert "level out of range: 255" in body

    csv = http.get("/api/v1/reports/export/supplies.csv").text
    assert "level out of range: 255" in csv


def test_a_device_status_note_survives_the_marker(db):
    """The marker goes FIRST so clipping to the 60-char column can only ever eat
    the device's own wording, never the explanation."""
    agent = _seed(db, printers=("10.0.0.10",))
    _post(agent.id, [_reading("10.0.0.10", supplies=[
        _supply(level_pct=255, status_note="some remaining"),
    ])])
    note = db.scalar(select(m.Supply)).status_note
    assert note.startswith("level out of range: 255")
    assert "some remaining" in note
    assert len(note) <= 60


def test_out_of_range_meters_and_raw_counts_do_not_reject_the_batch(db):
    """Same shape as the level, and on Postgres these 500 rather than 422 --
    an INT4 column will not take them. Either way the batch dies and the spool
    blocks, so both are coerced to "not reported"."""
    agent = _seed(db, printers=("10.0.0.10", "10.0.0.11"))
    resp = _post(agent.id, [
        _reading("10.0.0.10", page_count=2 ** 40, mono_count=-3,
                 supplies=[_supply(level_pct=50, current=2 ** 40, max_capacity=-9)]),
        _reading("10.0.0.11", page_count=12.5),   # a float pydantic would refuse
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] == 2
    by_ip = {p.ip: p for p in db.scalars(select(m.Printer))}
    assert by_ip["10.0.0.10"].page_count is None
    assert by_ip["10.0.0.10"].mono_count is None
    assert by_ip["10.0.0.11"].page_count == 12
    supply = db.scalar(select(m.Supply))
    assert supply.level_pct == 50.0          # the good field in the same row
    assert supply.current is None
    assert supply.max_capacity is None


def test_over_length_device_strings_are_clipped_to_their_columns(db):
    """SQLite ignores a VARCHAR width; Postgres raises StringDataRightTruncation
    and 500s the request. So this assertion is about the value's LENGTH, which
    is the part that protects the backend the suite does not run on."""
    agent = _seed(db, printers=("10.0.0.10",))
    resp = _post(agent.id, [_reading(
        "10.0.0.10",
        model="M" * 500, serial="S" * 500, firmware="F" * 500, hostname="H" * 500,
        supplies=[_supply(description="D" * 500, unit="U" * 200, level_pct=10)],
    )])
    assert resp.status_code == 200, resp.text
    printer = db.scalar(select(m.Printer))
    assert (len(printer.model), len(printer.serial)) == (200, 120)
    assert (len(printer.firmware), len(printer.hostname)) == (200, 200)
    supply = db.scalar(select(m.Supply))
    assert (len(supply.description), len(supply.unit)) == (200, 40)


def test_unknown_enum_members_do_not_reject_the_batch(db):
    """An agent NEWER than central (agents self-update; central does not) is the
    realistic source. Each unknown value degrades to the least-informative
    member, never to a refusal and never to a fabricated severity."""
    agent = _seed(db, printers=("10.0.0.10",))
    resp = _post(agent.id, [_reading(
        "10.0.0.10",
        status="quantum-jam",
        driver_tier="tier_from_the_future",
        supplies=[_supply(type="ribbon", level_pct=30)],
        events=[{"code": "E-1", "message": "odd", "severity": "apocalyptic",
                 "source": "telepathy"}],
    )])
    assert resp.status_code == 200, resp.text
    printer = db.scalar(select(m.Printer))
    assert printer.status == m.PrinterStatus.unknown
    assert printer.driver_tier is None          # "no new information", not a guess
    assert db.scalar(select(m.Supply)).type == m.SupplyType.other
    event = db.scalar(select(m.PrinterEvent))
    assert event.severity == m.EventSeverity.warning   # not info, not critical
    assert event.source == m.EventSource.agent         # append-only, not reconciled


# --------------------------------------------------------------------------- #
# The spool drains -- the part that made this permanent
# --------------------------------------------------------------------------- #
def _agent_client(agent_id):
    """A real ``CentralClient`` (real ``raise_for_status``) wired to the real app.

    Only the transport is substituted, so the failure path under test is the
    agent's actual one: a non-2xx raises, ``drain`` stops, the remainder stays
    on disk.
    """
    client = CentralClient("http://testserver", agent_id, API_KEY)
    client._client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {API_KEY}"},
        transport=httpx.ASGITransport(app=app),
    )
    return client


async def test_spool_drains_after_a_hostile_reading(db, tmp_path):
    """The real bug: a 422 that clears on retry is survivable, one that never
    clears is not. Buffer a batch containing a poisoned value as an outage
    would, then replay it against a live central and require the spool to reach
    zero. On the old schema this drained 0 and stayed at 3 forever."""
    agent = _seed(db)
    spool = ReadingSpool(str(tmp_path / "readings-spool.jsonl"), max_readings=100)
    spool.append([
        _reading("10.0.0.10", ts="2026-08-01T10:00:00+00:00",
                 supplies=[_supply(level_pct=42)]),
        _reading("10.0.0.11", ts="2026-08-01T10:00:00+00:00",
                 supplies=[_supply(level_pct=255)]),
        _reading("10.0.0.12", ts="2026-08-01T10:00:00+00:00",
                 supplies=[_supply(level_pct=7)]),
    ])
    assert spool.count() == 3

    client = _agent_client(agent.id)
    try:
        assert await drain_spool(client, spool) == 3
        assert spool.count() == 0
        # A second pass has nothing left to do -- the backlog is gone, not merely
        # smaller (a partial drain would re-offer the same head forever).
        assert await drain_spool(client, spool) == 0
    finally:
        await client._client.aclose()

    assert len(list(db.scalars(select(m.Reading)))) == 3
    levels = {p.ip: (p.supplies[0].level_pct if p.supplies else None)
              for p in db.scalars(select(m.Printer))}
    assert levels == {"10.0.0.10": 42.0, "10.0.0.11": None, "10.0.0.12": 7.0}


async def test_a_live_push_containing_a_hostile_reading_is_not_spooled(db, tmp_path):
    """The other end of the same path: a fresh cycle carrying a bad value must
    be accepted outright, so nothing is buffered to replay in the first place."""
    agent = _seed(db)
    spool = ReadingSpool(str(tmp_path / "readings-spool.jsonl"), max_readings=100)
    client = _agent_client(agent.id)
    try:
        applied = await push_readings(client, spool, [
            _reading("10.0.0.10", supplies=[_supply(level_pct=42)]),
            _reading("10.0.0.11", supplies=[_supply(level_pct=-7)]),
            _reading("10.0.0.12", supplies=[_supply(level_pct="n/a")]),
        ])
    finally:
        await client._client.aclose()
    assert applied == 3
    assert spool.count() == 0
