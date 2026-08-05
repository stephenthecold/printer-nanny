"""Headers a CSRF token cannot substitute for, and the schema that was public.

Both findings are about what the dashboard hands a browser, and neither is
covered by anything the synchronizer token does.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from central.main import app


def test_the_dashboard_refuses_to_be_framed(db):
    """A synchronizer token does NOT stop clickjacking.

    The CSRF module's motivating case is `/admin/backup` -- a whole-database
    dump and a restore. Frame the real dashboard at opacity 0, bait an admin
    into clicking where the real button is, and the token travels with the
    request and is correct. Framing is the hole the token cannot cover.
    """
    r = TestClient(app).get("/login")
    assert r.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")


def test_the_other_headers_are_present(db):
    r = TestClient(app).get("/login")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_headers_reach_redirects_too(db):
    """A 303 to /login is still a framable page, so the middleware sits outside
    everything rather than decorating successful HTML responses only."""
    r = TestClient(app).get("/manage/users", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("x-frame-options") == "DENY"


def test_a_route_with_its_own_policy_keeps_it(db):
    """`remote.py` serves a captured device page under a much stricter,
    purpose-built CSP (sandbox; default-src 'none'). A blanket header that
    replaced it would be a downgrade, so anything already set wins."""
    from central.middleware import SecurityHeadersMiddleware

    sent = []

    async def app_stub(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-security-policy", b"sandbox; default-src 'none'")],
        })

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        SecurityHeadersMiddleware(app_stub)({"type": "http"}, None, send)
    )
    headers = dict(sent[0]["headers"])
    assert headers[b"content-security-policy"] == b"sandbox; default-src 'none'"
    assert headers[b"x-frame-options"] == b"DENY"  # the others still land


def test_the_openapi_schema_is_not_public(db):
    """198 KB describing every SCIM, agent-ingest, workstation-enroll and
    management route, served to anyone who could reach the dashboard -- which
    behind the shipped Caddyfile means the internet."""
    cli = TestClient(app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert cli.get(path).status_code == 404, path
