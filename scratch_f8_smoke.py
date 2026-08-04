"""F8 end-to-end smoke against a freshly seeded throwaway DB.

Not part of the suite -- run by hand, deleted before commit.

  1. seed a throwaway SQLite DB (central.seed)
  2. synthesise a REAL multi-cartridge reading series with a KNOWN yield:
       - a healthy Brother:  4 cartridges x 3,000 pages
       - a short Brother:    4 cartridges x   900 pages  (same model)
       - a control HP:       4 cartridges x 5,000 pages  (different model)
  3. run the real worker job (not the library) so the whole path executes
  4. read back the computed yield and the verdicts
  5. render the operator page over HTTP
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "f8-smoke.sqlite3")
os.environ["SECRET_KEY"] = "f8-smoke-secret"

from datetime import datetime, timedelta, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import central  # noqa: E402
from central import models as m  # noqa: E402
from central import runtime  # noqa: E402
from central import supply_yield as sy  # noqa: E402
from central.db import SessionLocal  # noqa: E402
from central.main import app  # noqa: E402
from central.seed import seed  # noqa: E402
from central.security import hash_password  # noqa: E402
from central.worker import jobs  # noqa: E402

print("central     :", inspect.getfile(central))
print("supply_yield:", inspect.getfile(sy))
print("jobs        :", inspect.getfile(jobs))
print()

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

seed()
db = SessionLocal()
client = db.query(m.Client).first()
site = db.query(m.Site).filter(m.Site.client_id == client.id).first()
print("seeded client/site:", client.name, "/", site.name)


def make_printer(ip, model):
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip, model=model,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


def cartridges(printer, *, count, pages_each, steps=20, start_pages=10_000):
    """N full cartridges: level 100 -> 0 while the meter climbs pages_each."""
    ts = NOW - timedelta(days=120)
    pages = start_pages
    per_step = pages_each // steps
    for _cart in range(count):
        for step in range(steps + 1):
            db.add(m.Reading(
                printer_id=printer.id, ts=ts, page_count=pages,
                supply_snapshot=[{"type": "toner", "color": "black",
                                  "level_pct": 100.0 - step * (100.0 / steps)}],
            ))
            ts += timedelta(hours=12)
            if step < steps:
                pages += per_step


healthy = make_printer("10.9.0.1", "Brother MFC-L8900CDW series")
short = make_printer("10.9.0.2", "Brother MFC-L8900CDW series")
control = make_printer("10.9.0.3", "HP LaserJet M404dn")
cartridges(healthy, count=4, pages_each=3000)
cartridges(short, count=4, pages_each=900)
cartridges(control, count=4, pages_each=5000)
db.commit()
print("synthesised: healthy=3000 pages/cartridge, short=900, control(HP)=5000")
print()

# ---- 3. the REAL worker job ------------------------------------------------ #
print("worker  :", jobs.scan_supply_cycles(db, now=NOW))
print()

# ---- 4. what was measured -------------------------------------------------- #
print("=== measured cartridge cycles ===")
for cycle in db.query(m.SupplyCycle).order_by(m.SupplyCycle.printer_id,
                                              m.SupplyCycle.id).all():
    printer = db.get(m.Printer, cycle.printer_id)
    print("  %-10s %s complete=%-5s pages=%-6s consumed=%3.0f%% observed_yield=%s"
          % (printer.ip, cycle.supply_type, cycle.complete, cycle.pages,
             sy.consumed_pct(cycle), sy.observed_yield(cycle)))

print()
print("=== verdicts, no expected yield entered (fleet baseline only) ===")
for row in sy.assessments(db, thresholds=sy.YieldThresholds.load(db)):
    print("  %-10s %-14s %-18s observed=%-8s expected=%-8s source=%-9s"
          % (row.printer_label.split(" @ ")[1], row.supply_label, row.verdict,
             row.observed_pages, row.expected.pages, row.expected.source or "-"))
    for reason in row.reasons:
        print("        - %s" % reason)

print()
print("=== verdicts with the datasheet figure entered (3,000 pages) ===")
db.add(m.SupplyYieldExpectation(
    model_tag="MFC-L8900", supply_type="toner", color="", expected_pages=3000,
    note="TN-421 standard yield",
))
db.commit()
for row in sy.assessments(db, thresholds=sy.YieldThresholds.load(db)):
    print("  %-10s %-14s %-18s observed=%-8s expected=%-8s source=%-9s shortfall=%s"
          % (row.printer_label.split(" @ ")[1], row.supply_label, row.verdict,
             row.observed_pages, row.expected.pages, row.expected.source or "-",
             ("%.0f%%" % row.shortfall_pct) if row.shortfall_pct is not None else "-"))

print()
print("summary:", sy.summarize(sy.assessments(db, thresholds=sy.YieldThresholds.load(db))))

# ---- 5. the operator page over HTTP ---------------------------------------- #
db.add(m.User(username="smoke", password_hash=hash_password("pw"),
              role=m.UserRole.admin))
db.commit()
http = TestClient(app)
# Outside pytest there is no conftest patch supplying CSRF tokens, so the token
# is read out of the real login form -- which is also what a browser does.
form = http.get("/login").text
token = re.search(r'name="csrf_token"\s+value="([^"]+)"', form).group(1)
login = http.post("/login",
                  data={"username": "smoke", "password": "pw", "csrf_token": token},
                  follow_redirects=False)
print()
print("login ->", login.status_code, login.headers.get("location"))
page = http.get("/supplies/yield")
print()
print("GET /supplies/yield ->", page.status_code, len(page.text), "bytes")
for needle in ("below expected", "10.9.0.2", "insufficient data",
               "Recent cartridge replacements", "not a verdict"):
    print("   %-32s %s" % (needle, "present" if needle in page.text else "MISSING"))

print()
print("events default:", runtime.load_settings(db).get("yield.emit_events"),
      "-> outbound events:", db.query(m.OutboundEvent).count())
