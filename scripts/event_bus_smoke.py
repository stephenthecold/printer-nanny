#!/usr/bin/env python3
"""End-to-end smoke for the outbound event bus, against a freshly seeded DB.

Not a unit test and deliberately not in ``tests/``: it stands up a **real HTTP
listener** on loopback, runs the **real worker job**, and verifies the signature
on the bytes that actually crossed a socket. Everything above the transport is
covered by ``tests/test_event_bus.py``; what this adds is the one thing a mocked
transport cannot -- proof that what leaves this process is what a subscriber can
verify with nothing but the secret and a copy of the scheme.

It also demonstrates the SSRF guard from the other side: the listener is on
127.0.0.1, which is refused by default, so the run has to switch
``events.allow_private_destinations`` on. If that guard ever stops working, this
script stops needing that line -- and it asserts the refusal first.

Run:  DATABASE_URL=sqlite:////tmp/pn-smoke.sqlite3 python scripts/event_bus_smoke.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

RECEIVED = []


class Handler(BaseHTTPRequestHandler):
    """The subscriber. Records exactly what arrived; answers 200."""

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        RECEIVED.append({
            "path": self.path,
            "body": self.rfile.read(length),
            "headers": dict(self.headers),
        })
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # keep the smoke output readable
        pass


def verify_like_a_subscriber(secret, body, header, tolerance=300):
    """Independent verification -- deliberately NOT central.events.signing.verify.

    A subscriber has our documentation, not our code. Re-implementing the check
    from the docs is the only way this proves the *scheme* rather than proving
    that one function agrees with itself.
    """
    parts = dict(
        p.strip().split("=", 1) for p in header.split(",") if "=" in p
    )
    timestamp = int(parts["t"])
    signed = str(timestamp).encode() + b"." + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    import time

    fresh = abs(int(time.time()) - timestamp) <= tolerance
    return hmac.compare_digest(expected, parts["v1"]), fresh, timestamp


def main() -> int:
    db_path = os.environ.get("PN_SMOKE_DB", "/tmp/pn-event-bus-smoke.sqlite3")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path
    os.environ.setdefault("SECRET_KEY", "smoke-secret-not-for-production")

    import central
    import inspect as _inspect

    print("central module:", _inspect.getfile(central))

    from central import models as m
    from central.db import SessionLocal
    from central.events import signing
    from central.events.delivery import deliver_due
    from central.events.destinations import DestinationError, validate_url
    from central.events.emit import emit_alert_opened
    from central.runtime import load_settings, save_settings
    from central.secrets import decrypt_value, encrypt_value
    from central.seed import seed

    print("\n== seeding a throwaway database ==")
    seed()

    db = SessionLocal()
    clients = db.query(m.Client).order_by(m.Client.id).all()
    assert len(clients) >= 2, "the seed must produce at least two tenants"
    tenant_a, tenant_b = clients[0], clients[1]
    print("tenant A =", tenant_a.name, "| tenant B =", tenant_b.name)

    # ---------------------------------------------------------------- SSRF --
    print("\n== SSRF guard ==")
    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8/hook"):
        try:
            validate_url(url, allow_private=False)
        except DestinationError as exc:
            print("  refused (default):", url, "->", exc)
        else:
            print("  FAIL: accepted", url)
            return 1
    try:
        validate_url("http://169.254.169.254/x", allow_private=True)
    except DestinationError as exc:
        print("  refused even with private allowed:", exc)
    else:
        print("  FAIL: the metadata address was reachable under a setting")
        return 1

    # The listener is on loopback, so the run must opt in -- which is the guard
    # working, demonstrated rather than asserted.
    #
    # The whole section is posted, not just the one key. `save_settings` scopes
    # its writes to the sections it is told about, and a bool absent from a
    # posted section is (correctly) read as unchecked -- so a partial form here
    # would silently switch `events.enabled` OFF and the smoke would "fail" for
    # a reason that has nothing to do with the event bus. Found by running it.
    save_settings(
        db,
        {
            "events.enabled": "on",
            "events.allow_private_destinations": "on",
            "events.max_attempts": "8",
            "events.retry_base_seconds": "60",
            "events.timeout_seconds": "15",
            "events.retention_days": "30",
        },
        sections={"Event bus"},
    )
    db.commit()
    assert load_settings(db)["events.enabled"] is True
    assert load_settings(db)["events.allow_private_destinations"] is True

    # ------------------------------------------------------------ listener --
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    print("\n== subscriber listening on %s ==" % base)

    # ------------------------------------------------------- subscriptions --
    secret_a = signing.generate_secret()
    secret_global = signing.generate_secret()
    sub_a = m.EventSubscription(
        name="%s partner" % tenant_a.name, client_id=tenant_a.id,
        url=base + "/tenant-a", secret=encrypt_value(secret_a), enabled=True,
    )
    sub_global = m.EventSubscription(
        name="MSP global", client_id=None,
        url=base + "/global", secret=encrypt_value(secret_global), enabled=True,
    )
    db.add_all([sub_a, sub_global])
    db.commit()
    print("  subscription %d scoped to client %d (%s)"
          % (sub_a.id, tenant_a.id, tenant_a.name))
    print("  subscription %d global" % sub_global.id)
    print("  stored secret starts with:", sub_a.secret[:12], "(ciphertext)")
    assert sub_a.secret.startswith("enc:v1:"), "the secret was not encrypted at rest"
    assert decrypt_value(sub_a.secret) == secret_a

    # ------------------------------------------------------- emit an event --
    printer_b = (
        db.query(m.Printer).filter(m.Printer.client_id == tenant_b.id).first()
    )
    assert printer_b is not None, "tenant B has no printer to alert on"
    # A hostile device string, so the escaping claim is exercised on real bytes.
    printer_b.display_name = 'Front Desk", "level_pct": 100, "x": "'
    alert = m.Alert(
        printer_id=printer_b.id,
        type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title="Toner low",
        detail="black at 6%",
        dedupe_key="smoke:tenant-b",
    )
    db.add(alert)
    db.flush()
    event = emit_alert_opened(db, alert, printer_b)
    db.commit()
    print("\n== emitted %s for client %d (%s) ==" % (event.type, event.client_id, tenant_b.name))
    print("  uid:", event.uid)
    print("  idempotency_key:", event.idempotency_key)

    queued = db.query(m.EventDelivery).all()
    print("  queued deliveries:", [(d.subscription_id, d.status.value) for d in queued])

    # -------------------------------------------------------- deliver it ----
    runtime = load_settings(db)
    out = deliver_due(db, runtime)
    print("\n== worker delivery pass ==")
    print(" ", out)

    # ------------------------------------------------------------ verdicts --
    print("\n== TENANCY ==")
    paths = [r["path"] for r in RECEIVED]
    print("  requests received at:", paths)
    if "/tenant-a" in paths:
        print("  FAIL: the %s-scoped subscription received %s's event"
              % (tenant_a.name, tenant_b.name))
        return 1
    if "/global" not in paths:
        print("  FAIL: the global subscription received nothing")
        return 1
    print("  PASS: only the global subscription was delivered to;")
    print("        the client-scoped one never received another tenant's event.")

    delivered = [d for d in db.query(m.EventDelivery).all()]
    for d in delivered:
        print("  delivery sub=%d status=%s http=%s"
              % (d.subscription_id, d.status.value, d.response_status))
    assert all(d.subscription_id == sub_global.id for d in delivered)

    print("\n== SIGNATURE ==")
    received = RECEIVED[0]
    body = received["body"]
    header = received["headers"][signing.SIGNATURE_HEADER]
    print("  ", signing.SIGNATURE_HEADER, "=", header)
    ok, fresh, ts = verify_like_a_subscriber(secret_global, body, header)
    print("   verified with the subscription secret:", ok)
    print("   timestamp fresh (<=300s):", fresh, "( t =", ts, ")")
    if not (ok and fresh):
        print("  FAIL: a subscriber could not verify what we sent")
        return 1

    wrong, _, _ = verify_like_a_subscriber(secret_a, body, header)
    print("   verified with the OTHER subscription's secret:", wrong, "(must be False)")
    if wrong:
        return 1

    moved = header.replace("t=%d" % ts, "t=%d" % (ts + 600))
    replayed, replay_fresh, _ = verify_like_a_subscriber(secret_global, body, moved)
    print("   replay with the timestamp moved forward:", replayed, "(must be False)")
    if replayed:
        print("  FAIL: the timestamp is not covered by the signature")
        return 1

    tampered = body.replace(b"Toner low", b"Toner OK!")
    bad, _, _ = verify_like_a_subscriber(secret_global, tampered, header)
    print("   verified after editing the body:", bad, "(must be False)")
    if bad:
        return 1

    print("\n== SIGNED PAYLOAD (the exact bytes on the wire) ==")
    print(body.decode("utf-8"))
    print("\n== headers ==")
    for k, v in sorted(received["headers"].items()):
        if k.lower().startswith("x-printernanny") or k.lower() == "content-type":
            print("  %s: %s" % (k, v))

    parsed = json.loads(body.decode("utf-8"))
    hostile = parsed["data"]["printer"]["name"]
    print("\n== DEVICE STRING ==")
    print("  stored :", repr(printer_b.display_name))
    print("  round-trip:", repr(hostile))
    if hostile != printer_b.display_name:
        print("  FAIL: the device string did not round-trip intact")
        return 1
    if "level_pct" in parsed["data"]["printer"]:
        print("  FAIL: a device string injected a sibling key")
        return 1
    print("  PASS: quoted by json.dumps, not double-escaped, no key injection.")

    # ------------------------------------------------------ idempotency -----
    print("\n== IDEMPOTENCY ==")
    again = emit_alert_opened(db, alert, printer_b)
    db.commit()
    print("  re-emitting the same occurrence returned:", again, "(must be None)")
    if again is not None:
        return 1
    before = len(RECEIVED)
    deliver_due(db, load_settings(db))
    print("  a second worker pass sent", len(RECEIVED) - before, "further requests (must be 0)")
    if len(RECEIVED) != before:
        return 1

    server.shutdown()
    db.close()
    print("\nALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
