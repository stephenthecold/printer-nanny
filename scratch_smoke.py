import inspect
import os
import re
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.gettempdir(), "pn_csrf_smoke.sqlite3")
os.environ["SECRET_KEY"] = "smoke-secret"

import central  # noqa: E402
print("central from:", inspect.getfile(central))

from fastapi.testclient import TestClient  # noqa: E402

from central import models as m  # noqa: E402
from central.db import Base, SessionLocal, engine  # noqa: E402
from central.main import app  # noqa: E402
from central.security import hash_password  # noqa: E402

Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    db.add(m.User(username="admin", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin, active=True))
    db.commit()

c = TestClient(app, base_url="https://testserver")

r = c.get("/login")
print("GET /login ->", r.status_code)
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
print("token in login form:", tok[:12], "...")

# (b) POST without a token is refused
r = c.post("/login", data={"username": "admin", "password": "pw12345678"},
           follow_redirects=False)
print("POST /login  no token ->", r.status_code, "|", r.text[:70].replace("\n", " "))

# (a) legitimate POST works
r = c.post("/login", data={"username": "admin", "password": "pw12345678",
                           "csrf_token": tok}, follow_redirects=False)
print("POST /login with token ->", r.status_code, r.headers.get("location"))

page = c.get("/manage/agents")
print("GET /manage/agents ->", page.status_code)
tok2 = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
print("token rotated on login:", tok2 != tok)
print("hx-headers present:", 'hx-headers=' in page.text)

# a real form POST
r = c.post("/manage/clients", data={"name": "Acme", "csrf_token": tok2},
           follow_redirects=False)
print("POST /manage/clients with token ->", r.status_code)
r = c.post("/manage/clients", data={"name": "Evil"}, follow_redirects=False)
print("POST /manage/clients no token  ->", r.status_code)
r = c.post("/manage/clients", data={"name": "Evil", "csrf_token": "x" * 43},
           follow_redirects=False)
print("POST /manage/clients bad token ->", r.status_code)

# header channel (htmx)
r = c.post("/manage/clients", data={"name": "Beta"},
           headers={"X-CSRF-Token": tok2}, follow_redirects=False)
print("POST /manage/clients hdr token ->", r.status_code)

# (c) backup download
r = c.get("/admin/backup/download", follow_redirects=False)
print("GET  /admin/backup/download ->", r.status_code,
      r.headers.get("location"), "| body bytes:", len(r.content))
r = c.post("/admin/backup/download", follow_redirects=False)
print("POST /admin/backup/download no token ->", r.status_code, "| bytes:", len(r.content))
r = c.post("/admin/backup/download", data={"csrf_token": tok2}, follow_redirects=False)
print("POST /admin/backup/download w/ token ->", r.status_code, "| bytes:", len(r.content),
      "|", r.headers.get("content-disposition"))

# the api/v1 session-authed CSRF hole
with SessionLocal() as db:
    cl = db.scalar(__import__("sqlalchemy").select(m.Client))
    site = m.Site(client_id=cl.id, name="HQ")
    db.add(site)
    db.flush()
    p = m.Printer(client_id=cl.id, site_id=site.id, ip="10.0.0.5",
                  discovery_state=m.DiscoveryState.pending)
    db.add(p)
    db.commit()
    pid = p.id
r = c.post("/api/v1/printers/%d/approve" % pid, follow_redirects=False)
print("POST /api/v1/printers/N/approve no token ->", r.status_code)
r = c.post("/api/v1/printers/%d/approve" % pid, headers={"X-CSRF-Token": tok2})
print("POST /api/v1/printers/N/approve w/ token ->", r.status_code)

# agent bearer surface unaffected
r = c.post("/api/v1/agents/register", json={"claim_code": "nope", "hostname": "h"})
print("POST /api/v1/agents/register (no cookie client would be 401) ->", r.status_code)

fresh = TestClient(app, base_url="https://testserver")
r = fresh.post("/api/v1/agents/register", json={"claim_code": "nope", "hostname": "h"})
print("fresh client, no session, agent register ->", r.status_code)
r = fresh.post("/api/v1/agents/1/heartbeat", json={}, headers={"Authorization": "Bearer x"})
print("fresh client, agent heartbeat        ->", r.status_code)

with SessionLocal() as db:
    rows = [(a.action, a.detail, a.target) for a in db.scalars(
        __import__("sqlalchemy").select(m.AuditLog).where(m.AuditLog.action == "csrf.rejected"))]
print("audit csrf.rejected rows:", len(rows))
for row in rows:
    print("   ", row)
