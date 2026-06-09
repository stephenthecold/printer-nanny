"""Self-update diagnostics: install marker version, result write/read, heartbeat plumbing.

These tests exercise the path that lets an operator SEE whether 'Update' on
/manage/agents actually replaced the agent's package files (or why pip
failed) without ssh-ing into the agent host.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import generate_api_key, hash_api_key, hash_password
from printer_nanny_agent import __base_version__, __version__, _install_marker


def _seed_agent(db) -> tuple[m.Agent, str]:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    api_key = generate_api_key()
    agent = m.Agent(site_id=site.id, name="hq", api_key_hash=hash_api_key(api_key))
    db.add(agent)
    db.commit()
    return agent, api_key


def _login_admin(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("pw"),
        role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


# ---------- Version carries an install marker ----------

def test_version_string_includes_install_marker():
    """__version__ must embed a timestamp suffix from the package file mtime
    so an operator can see updates actually replaced the files."""
    assert __version__.startswith(__base_version__)
    marker = _install_marker()
    assert marker  # non-empty in any reasonable filesystem
    # Marker is YYYYMMDD-HHMMSS or similar fixed-shape.
    assert "+" in __version__ or __version__ == __base_version__
    if "+" in __version__:
        assert __version__.endswith(marker)


# ---------- Update-result marker file round-trips ----------

def test_update_result_marker_round_trip(tmp_path, monkeypatch):
    """The updater writes a JSON marker that's read on next start; that round
    trip is what makes the dashboard show 'last update: ok at X'."""
    from printer_nanny_agent import updater

    fake_marker = tmp_path / "marker.json"
    monkeypatch.setattr(updater, "_result_path", lambda: fake_marker)

    updater._write_result("ok", "pip install succeeded")
    result = updater.read_last_update_result()
    assert result["status"] == "ok"
    assert result["detail"] == "pip install succeeded"
    assert result["ts"].endswith("Z")  # ISO-8601 UTC marker


def test_update_result_returns_none_when_no_marker(tmp_path, monkeypatch):
    """No prior attempt -> no marker -> agent reports nothing."""
    from printer_nanny_agent import updater
    monkeypatch.setattr(updater, "_result_path", lambda: tmp_path / "missing.json")
    assert updater.read_last_update_result() is None


def test_update_result_returns_none_on_malformed_marker(tmp_path, monkeypatch):
    """A corrupt marker file shouldn't crash the agent on startup."""
    from printer_nanny_agent import updater
    bogus = tmp_path / "bad.json"
    bogus.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(updater, "_result_path", lambda: bogus)
    assert updater.read_last_update_result() is None


def test_update_result_caps_detail_length(tmp_path, monkeypatch):
    """A multi-MB pip stderr must not bloat heartbeat payloads."""
    from printer_nanny_agent import updater
    monkeypatch.setattr(updater, "_result_path", lambda: tmp_path / "m.json")
    updater._write_result("pip_failed", "x" * 5000)
    result = updater.read_last_update_result()
    assert len(result["detail"]) <= 1024


# ---------- Heartbeat carries diagnostic fields ----------

def test_heartbeat_persists_install_path_and_update_result(db):
    agent, api_key = _seed_agent(db)
    cli = TestClient(app)
    resp = cli.post(
        f"/api/v1/agents/{agent.id}/heartbeat",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "version": "0.1.0+20260609-180000",
            "install_path": "/opt/printer-nanny-agent/.venv/lib/python3.12/site-packages/printer_nanny_agent",
            "last_update_result": {
                "status": "ok",
                "detail": "pip install succeeded; restarting via service manager",
                "ts": "2026-06-09T17:59:55Z",
            },
        },
    )
    assert resp.status_code == 200
    db.refresh(agent)
    assert agent.version == "0.1.0+20260609-180000"
    assert "site-packages" in agent.install_path
    assert agent.last_update_result["status"] == "ok"


def test_heartbeat_legacy_payload_still_works(db):
    """An old agent that only sends {version} must still succeed."""
    agent, api_key = _seed_agent(db)
    cli = TestClient(app)
    resp = cli.post(
        f"/api/v1/agents/{agent.id}/heartbeat",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"version": "0.1.0"},
    )
    assert resp.status_code == 200
    db.refresh(agent)
    assert agent.version == "0.1.0"
    assert agent.install_path is None
    assert agent.last_update_result is None


def test_heartbeat_does_not_clear_install_path_when_field_omitted(db):
    """Once we have install_path stored, a heartbeat without the field must
    NOT null it out -- otherwise mixed-version fleets churn the row."""
    agent, api_key = _seed_agent(db)
    agent.install_path = "/old/path"
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        f"/api/v1/agents/{agent.id}/heartbeat",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"version": "0.1.0"},  # no install_path
    )
    assert resp.status_code == 200
    db.refresh(agent)
    assert agent.install_path == "/old/path"  # preserved


# ---------- /manage/agents UI surfaces the new fields ----------

def test_agents_page_shows_install_path_and_update_result(db):
    agent, _api_key = _seed_agent(db)
    agent.version = "0.1.0+20260609-180000"
    agent.install_path = "/opt/agent/site-packages/printer_nanny_agent"
    agent.last_update_result = {
        "status": "pip_failed",
        "detail": "ERROR: Could not find a version that satisfies the requirement",
        "ts": "2026-06-09T17:55:00Z",
    }
    db.commit()
    cli = _login_admin(db)
    resp = cli.get("/manage/agents", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "0.1.0+20260609-180000" in body
    assert "/opt/agent/site-packages/printer_nanny_agent" in body
    assert "pip_failed" in body
    # Failure detail surfaces so the operator can act on it
    assert "Could not find a version" in body


def test_agents_page_warns_on_placeholder_pip_source(db):
    """If pip source is the 'your-org' placeholder, Update will fail.
    Surface that loudly so operators don't waste a debugging cycle."""
    _seed_agent(db)
    db.add(m.AppSetting(
        key="agent.pip_source",
        value="git+https://github.com/your-org/printer-nanny.git#subdirectory=agent",
    ))
    db.commit()
    cli = _login_admin(db)
    resp = cli.get("/manage/agents", follow_redirects=False)
    assert resp.status_code == 200
    assert "placeholder pip source" in resp.text.lower()


def test_agents_page_shows_pip_source_when_configured(db):
    """When pip source is a real URL, show it (not a warning) so the operator
    can verify what self-update will install from."""
    _seed_agent(db)
    cli = _login_admin(db)
    resp = cli.get("/manage/agents", follow_redirects=False)
    assert resp.status_code == 200
    assert "Self-update will pip-install from" in resp.text
    assert "github.com" in resp.text


# ---------- Runner emits diagnostic fields on heartbeat ----------

async def test_runner_sends_install_path_on_first_heartbeat(monkeypatch, tmp_path):
    """run_once should pull install_path + update_result and pass them to
    client.heartbeat(). Ensures the wiring is plumbed end-to-end."""
    from printer_nanny_agent import runner

    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def heartbeat(self, version=None, install_path=None, last_update_result=None):
            captured["version"] = version
            captured["install_path"] = install_path
            captured["last_update_result"] = last_update_result
            return {}

        async def get_config(self):
            return {}

        async def get_commands(self):
            return []

        async def get_targets(self):
            return []

        async def post_readings(self, readings):
            return {"applied": 0}

        async def post_discovered(self, devices):
            return {"new_pending": 0}

        async def aclose(self):
            return None

    class FakeBackend:
        async def close(self):
            return None

    # Pre-seed an update marker that the runner should read.
    from printer_nanny_agent import updater
    marker = tmp_path / "m.json"
    marker.write_text(json.dumps({"status": "ok", "detail": "x", "ts": "2026-06-09T17:00:00Z"}))
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    monkeypatch.setattr(runner, "CentralClient", FakeClient)

    config = runner.AgentConfig(central_url="http://x", agent_id=1, api_key="k")
    await runner.run_once(config, backend=FakeBackend())

    assert captured["install_path"] is not None
    assert "printer_nanny_agent" in captured["install_path"]
    assert captured["last_update_result"]["status"] == "ok"
