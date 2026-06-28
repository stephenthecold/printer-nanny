"""Closed-loop FreeScout ticketing: alerts open a ticket and close it on resolve.

The FreeScout channel's HTTP is monkeypatched (no network): create returns a
conversation id, close records the call. We drive a real alert open/resolve
through the worker and assert ``Alert.external_ref`` is captured on open and the
matching ticket is closed exactly once on auto-resolve.
"""

from __future__ import annotations

import httpx

from central import models as m
from central.channels.base import ChannelResult
from central.channels.freescout import FreeScoutChannel
from central.runtime import default_settings
from central.worker import jobs

TICKET_ID = "4242"


def _approved_printer(db, ip="10.0.0.5"):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=ip,
        model="HP M404",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


def _low_supply_rule(db, printer, level_pct=5, threshold=10):
    supply = m.Supply(
        printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=level_pct
    )
    db.add(supply)
    db.add(
        m.AlertRule(
            name="low",
            condition_type=m.AlertConditionType.supply_below,
            threshold=threshold,
            severity=m.EventSeverity.warning,
        )
    )
    db.commit()
    return supply


class _FakeResp:
    def __init__(self, status_code, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_body or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


def _patch_freescout(monkeypatch, *, close_status=200):
    """Patch httpx.post so FreeScout create returns TICKET_ID and close is recorded.

    Returns the ``calls`` list: every close (threads) POST appends its URL +
    payload so the test can assert exactly-once.
    """
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/api/conversations"):
            # create
            return _FakeResp(201, json_body={"id": int(TICKET_ID)})
        if "/threads" in url:
            # close-and-note
            calls.append({"url": url, "payload": json})
            return _FakeResp(close_status)
        raise AssertionError(f"unexpected FreeScout URL: {url}")

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _enabled_runtime():
    rt = default_settings()
    rt.update(
        {
            "freescout.enabled": True,
            "freescout.base_url": "https://help.example.com",
            "freescout.api_key": "k-secret",
            "freescout.mailbox_id": 1,
        }
    )
    return rt


def test_open_persists_external_ref(db, monkeypatch):
    _patch_freescout(monkeypatch)
    printer = _approved_printer(db)
    _low_supply_rule(db, printer)

    # Drive the real worker, but with a FreeScout channel built from an enabled
    # runtime so a ticket is actually created (and external_ref captured).
    monkeypatch.setattr(jobs, "load_settings", lambda _db: _enabled_runtime())

    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1

    alert = db.query(m.Alert).filter_by(state=m.AlertState.open).one()
    assert alert.external_ref == TICKET_ID


def test_auto_resolve_closes_ticket_exactly_once(db, monkeypatch):
    calls = _patch_freescout(monkeypatch)
    printer = _approved_printer(db)
    supply = _low_supply_rule(db, printer)
    monkeypatch.setattr(jobs, "load_settings", lambda _db: _enabled_runtime())

    jobs.evaluate_alerts(db)
    alert = db.query(m.Alert).filter_by(state=m.AlertState.open).one()
    assert alert.external_ref == TICKET_ID
    assert calls == []  # nothing closed yet

    # Clear the condition → auto-resolve → exactly one close call for our ticket.
    supply.level_pct = 90
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_resolved"] == 1

    assert len(calls) == 1
    assert f"/api/conversations/{TICKET_ID}/threads" in calls[0]["url"]
    assert calls[0]["payload"]["status"] == "closed"
    assert calls[0]["payload"]["type"] == "note"

    # Re-running after resolve must not close again (alert is already resolved).
    res2 = jobs.evaluate_alerts(db)
    assert res2["alerts_resolved"] == 0
    assert len(calls) == 1


def test_no_external_ref_means_no_close(db, monkeypatch):
    # FreeScout NOT enabled → no ticket created → external_ref stays None →
    # resolve must not attempt any FreeScout close call.
    calls = _patch_freescout(monkeypatch)
    printer = _approved_printer(db)
    supply = _low_supply_rule(db, printer)
    # default runtime: freescout disabled (active_channels returns no FreeScout).

    jobs.evaluate_alerts(db)
    alert = db.query(m.Alert).filter_by(state=m.AlertState.open).one()
    assert alert.external_ref is None

    supply.level_pct = 90
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_resolved"] == 1
    assert calls == []  # no close attempted


def test_already_closed_ticket_handled(db, monkeypatch):
    # FreeScout answers 412 (status already closed) / 404 (gone) → treated as a
    # successful no-op so the resolve still proceeds and isn't retried-forever.
    calls = _patch_freescout(monkeypatch, close_status=412)
    printer = _approved_printer(db)
    supply = _low_supply_rule(db, printer)
    monkeypatch.setattr(jobs, "load_settings", lambda _db: _enabled_runtime())

    jobs.evaluate_alerts(db)
    supply.level_pct = 90
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_resolved"] == 1
    assert len(calls) == 1  # the close was attempted, gracefully no-op'd

    # And the channel method itself reports ok on a 412.
    ch = FreeScoutChannel("FreeScout", {}, _enabled_runtime())
    out = ch.close_ticket(TICKET_ID, "resolved")
    assert isinstance(out, ChannelResult)
    assert out.ok is True
    assert "already closed" in out.detail


def test_close_ticket_dry_run_without_creds():
    # No base_url/api_key → dry-run close, no HTTP call, still ok.
    res = FreeScoutChannel("FreeScout", {}).close_ticket(TICKET_ID, "resolved")
    assert res.ok is True
    assert "dry-run close" in res.detail


def test_close_ticket_empty_ref_is_error():
    res = FreeScoutChannel("FreeScout", {}, _enabled_runtime()).close_ticket("", "x")
    assert res.ok is False
    assert "no external_ref" in res.detail
