"""Windows MSI builder: WiX generation, config injection, capability gating, and
the dashboard build/download route.

Three layers:
  1. Pure unit tests of the builder library (no toolchain, no DB) -- WXS shape,
     config.toml rendering, ._pth patching, local-embeddable resolution.
  2. A real end-to-end ``wixl`` build, SKIPPED when msitools isn't installed (so
     CI without the toolchain stays green while a dev box with it gets full
     coverage). It fabricates the embeddable + NSSM so the only thing exercised
     is our own build, not python.org / nssm.cc.
  3. Route tests of POST /manage/agents/msi: manager-gating, audit, graceful
     degradation when the toolchain is missing or the build fails, and that the
     baked-in API key never lands in an audit row. build_msi is monkeypatched so
     these run regardless of toolchain.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from xml.dom import minidom

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import msi_builder as M
from central.main import app
from central.security import generate_api_key, hash_api_key, hash_password

_HAVE_WIXL = bool(shutil.which("wixl") and shutil.which("msiinfo"))


# --------------------------------------------------------------------------- #
# 1. Pure unit tests
# --------------------------------------------------------------------------- #
def test_render_config_toml_injects_enrollment():
    toml = M.render_config_toml(
        central_url="https://central.example.com/", agent_id=12, api_key="pn_secret"
    )
    # Trailing slash stripped; ints unquoted; strings quoted; tls defaulted true.
    assert 'central_url = "https://central.example.com"' in toml
    assert "agent_id = 12" in toml
    assert 'api_key = "pn_secret"' in toml
    assert "verify_tls = true" in toml


def test_render_config_toml_verify_tls_false():
    toml = M.render_config_toml(
        central_url="https://c", agent_id=1, api_key="k", verify_tls=False
    )
    assert "verify_tls = false" in toml


@pytest.mark.skipif(__import__("sys").version_info < (3, 11), reason="tomllib is 3.11+")
def test_render_config_toml_escapes_quotes_and_backslashes():
    """A quote/backslash-bearing key must still yield parseable TOML (the route
    accepts an arbitrary api_key form value)."""
    import tomllib

    key = 'pn_has"quote\\and\\back'
    toml = M.render_config_toml(central_url="https://c/", agent_id=3, api_key=key)
    data = tomllib.loads(toml)  # would raise on unescaped quote/backslash
    assert data["api_key"] == key
    assert data["agent_id"] == 3
    assert data["central_url"] == "https://c"


def test_concurrent_cold_builds_do_not_collide(tmp_path, monkeypatch):
    """Two builds racing on a COLD cache must not delete each other's staging or
    publish a half-built tree -- they both end up at the same valid runtime.

    Targets the unique-staging-dir + atomic-publish fix. The heavy/networked
    steps are faked so this is fast and runs without wixl or PyPI; the race is
    purely in the staging/rename logic.
    """
    import threading
    import time

    cache = tmp_path / "cache"
    embed = tmp_path / "python-3.12.10-embed-amd64.zip"
    with zipfile.ZipFile(embed, "w") as zf:
        zf.writestr("python.exe", b"MZ")
        zf.writestr("python312._pth", "python312.zip\n.\n")

    monkeypatch.setattr(M, "_ensure_python_embed", lambda url, c: embed)

    def fake_nssm(c):
        c.mkdir(parents=True, exist_ok=True)
        p = c / "nssm.exe"
        p.write_bytes(b"MZ")
        return p

    monkeypatch.setattr(M, "_ensure_nssm", fake_nssm)

    def fake_pip(site_packages, src, py):
        site_packages.mkdir(parents=True, exist_ok=True)
        (site_packages / "printer_nanny_agent").mkdir()
        time.sleep(0.2)  # widen the window so the two builds genuinely overlap

    monkeypatch.setattr(M, "_pip_install_agent", fake_pip)

    results: dict = {}

    def build(i):
        results[i] = M._ensure_runtime_tree(
            cache=cache, embed_url=str(embed), agent_src=tmp_path,
            agent_version="0.4.0", python_exe="x",
        )

    threads = [threading.Thread(target=build, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rt = M._runtime_cache_dir(cache, "0.4.0", "3.12.10")
    assert results.get(1) == rt and results.get(2) == rt
    assert (rt / "python" / "python.exe").exists() and (rt / "nssm.exe").exists()
    # No abandoned half-built staging dirs left behind.
    assert list((cache / "msi").glob("rt-*.building-*")) == []


def _tiny_payload(tmp_path: Path) -> Path:
    payload = tmp_path / "payload"
    (payload / "python" / "Lib" / "site-packages" / "printer_nanny_agent").mkdir(parents=True)
    (payload / "python" / "python.exe").write_bytes(b"MZ")
    (payload / "python" / "python312.dll").write_bytes(b"MZ")
    (payload / "python" / "python312._pth").write_text("python312.zip\n.\n")
    (payload / "python" / "Lib" / "site-packages" / "printer_nanny_agent" / "__init__.py").write_text("x=1\n")
    (payload / "nssm.exe").write_bytes(b"MZ")
    (payload / "config.toml").write_text("central_url = \"https://c\"\nagent_id = 1\napi_key = \"k\"\n")
    return payload


def test_generate_wxs_is_well_formed_xml(tmp_path):
    wxs = M.generate_wxs(_tiny_payload(tmp_path), product_version="0.4.0")
    # Parses as XML -- catches unescaped attrs / malformed nesting.
    minidom.parseString(wxs)


def test_generate_wxs_declares_service_and_nssm_registry(tmp_path):
    wxs = M.generate_wxs(_tiny_payload(tmp_path), product_version="0.4.0")
    # Declarative service registration, no custom action.
    assert "<ServiceInstall" in wxs and f'Name="{M.SERVICE_NAME}"' in wxs
    assert "<ServiceControl" in wxs
    assert "CustomAction" not in wxs
    # NSSM gets its service name from argv[1]; we must pass it.
    assert f'Arguments="{M.SERVICE_NAME}"' in wxs
    # NSSM run parameters point at the bundled python + the agent module + config.
    assert "python\\python.exe" in wxs
    assert "-m printer_nanny_agent" in wxs
    assert "[INSTALLDIR]config.toml" in wxs
    # perMachine x64 install under Program Files with a stable UpgradeCode.
    assert "ProgramFiles64Folder" in wxs
    assert M.UPGRADE_CODE in wxs
    assert "<MajorUpgrade" in wxs


def test_generate_wxs_every_component_has_guid_and_is_referenced(tmp_path):
    wxs = M.generate_wxs(_tiny_payload(tmp_path), product_version="0.4.0")
    dom = minidom.parseString(wxs)
    comp_ids = []
    for comp in dom.getElementsByTagName("Component"):
        assert comp.getAttribute("Guid"), "every component needs a GUID"
        comp_ids.append(comp.getAttribute("Id"))
    ref_ids = {r.getAttribute("Id") for r in dom.getElementsByTagName("ComponentRef")}
    # Every component is referenced by the feature (an unreferenced component is
    # silently dropped from the install).
    assert set(comp_ids) == ref_ids
    assert len(comp_ids) == len(set(comp_ids)), "component ids must be unique"


def test_generate_wxs_nssm_is_root_keypath(tmp_path):
    """The service binds to its component's keypath -- that must be nssm.exe."""
    wxs = M.generate_wxs(_tiny_payload(tmp_path), product_version="0.4.0")
    dom = minidom.parseString(wxs)
    for comp in dom.getElementsByTagName("Component"):
        if comp.getElementsByTagName("ServiceInstall"):
            files = comp.getElementsByTagName("File")
            keypath = [f for f in files if f.getAttribute("KeyPath") == "yes"]
            assert keypath and keypath[0].getAttribute("Name") == "nssm.exe"
            break
    else:
        pytest.fail("no component carried the ServiceInstall")


def test_capability_shape():
    cap = M.msi_build_available()
    assert isinstance(cap.available, bool)
    assert isinstance(cap.reason, str) and cap.reason


def test_embed_version_parsing():
    assert M._embed_version_from_url(
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
    ) == "3.12.10"
    assert M._embed_version_from_url("file:///srv/python-3.11.9-embed-amd64.zip") == "3.11.9"
    # Unparseable -> documented default, never a crash.
    assert M._embed_version_from_url("embed.zip") == M.DEFAULT_PYTHON_EMBED_VERSION


def test_local_embeddable_is_used_without_download(tmp_path):
    z = tmp_path / "python-3.12.10-embed-amd64.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("python.exe", b"MZ")
    cache = tmp_path / "cache"
    # Both a plain path and a file:// URL resolve to the staged zip; no network.
    assert M._ensure_python_embed(str(z), cache) == z
    assert M._ensure_python_embed(f"file://{z}", cache) == z


def test_patch_pth_enables_site_packages(tmp_path):
    py = tmp_path / "python"
    py.mkdir()
    (py / "python312._pth").write_text(
        "python312.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n"
    )
    M._patch_pth(py)
    out = (py / "python312._pth").read_text()
    assert "Lib\\site-packages" in out
    # Exactly one enabled `import site`, and the disabled form is gone.
    assert "\nimport site\n" in ("\n" + out)
    assert "#import site" not in out


# --------------------------------------------------------------------------- #
# 2. Real end-to-end build (needs msitools/wixl)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAVE_WIXL, reason="msitools/wixl not installed")
def test_build_msi_end_to_end(tmp_path, monkeypatch):
    # Fabricated embeddable stands in for python.org's; pip pulls the REAL agent.
    embed = tmp_path / "python-3.12.10-embed-amd64.zip"
    with zipfile.ZipFile(embed, "w") as zf:
        zf.writestr("python.exe", b"MZ\x90\x00fake")
        zf.writestr("python312.dll", b"MZ")
        zf.writestr("python312.zip", b"PK")
        zf.writestr("python312._pth", "python312.zip\n.\n#import site\n")

    cache = tmp_path / "cache"
    monkeypatch.setenv("PN_CACHE_DIR", str(cache))
    # Pre-stage NSSM so the build never touches nssm.cc.
    from central.dashboard.installer import _nssm_cache_path
    np = _nssm_cache_path("x64")
    np.parent.mkdir(parents=True, exist_ok=True)
    np.write_bytes(b"MZ" + b"x" * 60000)

    out = tmp_path / "out"
    res = M.build_msi(
        agent_id=99, agent_name="e2e", central_url="https://printers.example.com",
        api_key="pn_e2e_key", verify_tls=True, out_dir=out, cache_dir=cache,
        embed_url=f"file://{embed}", product_version="0.4.0",
    )
    assert res.path.exists() and res.size > 0

    v = M.validate_msi(res.path)
    assert v["has_service"] and v["has_registry"] and v["has_files"]
    assert v["nssm_application_ok"] and v["nssm_appparameters_ok"]
    # Enrollment really was baked in.
    assert v["config_toml"] and "pn_e2e_key" in v["config_toml"]
    assert "https://printers.example.com" in v["config_toml"]
    # The real agent + a dependency actually landed in the runtime cache.
    sp = next(cache.glob("msi/rt-*/python/Lib/site-packages"))
    assert (sp / "printer_nanny_agent").exists()
    assert (sp / "httpx").exists()


# --------------------------------------------------------------------------- #
# 3. Route tests
# --------------------------------------------------------------------------- #
def _seed_agent(db) -> m.Agent:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(
        site_id=site.id, name="HQ-agent",
        api_key_hash=hash_api_key(generate_api_key()), version="0.4.0",
    )
    db.add(agent)
    db.flush()
    return agent


def _login(db, role=m.UserRole.admin) -> TestClient:
    db.add(m.User(username="u", password_hash=hash_password("pw"), role=role))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "u", "password": "pw"}, follow_redirects=False)
    return cli


def _fake_available(monkeypatch, available=True):
    monkeypatch.setattr(
        M, "msi_build_available",
        lambda: M.MsiCapability(available=available, reason="ok" if available else "no tools"),
    )


def _fake_build(monkeypatch):
    """Patch build_msi to drop a fake .msi into the route's out_dir."""
    def fake(**kw):
        out = Path(kw["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"printer-nanny-agent-{kw['agent_id']}.msi"
        p.write_bytes(b"MSI\x00fake-artifact")
        return M.MsiBuildResult(
            path=p, size=p.stat().st_size, product_version="0.4.0",
            agent_version="0.4.0", agent_id=kw["agent_id"],
        )
    monkeypatch.setattr(M, "build_msi", fake)


def _audit(db, action="agent.msi_build"):
    return db.scalars(select(m.AuditLog).where(m.AuditLog.action == action)).all()


def test_msi_button_shown_after_enroll_when_available(db, monkeypatch):
    _fake_available(monkeypatch, True)
    _seed_agent(db)
    cli = _login(db)
    # Enroll -> the page renders the one-time install block with the MSI button.
    cli.post("/manage/agents", data={"site_id": 1, "name": "new"}, follow_redirects=False)
    body = cli.get("/manage/agents", follow_redirects=False).text
    assert "Download Windows MSI" in body
    assert 'action="/manage/agents/msi"' in body


def test_msi_hint_shown_when_unavailable(db, monkeypatch):
    _fake_available(monkeypatch, False)
    _seed_agent(db)
    cli = _login(db)
    cli.post("/manage/agents", data={"site_id": 1, "name": "new"}, follow_redirects=False)
    body = cli.get("/manage/agents", follow_redirects=False).text
    assert "Download Windows MSI" not in body
    assert "MSI builder unavailable" in body


def test_build_route_success_streams_and_audits(db, monkeypatch):
    _fake_available(monkeypatch, True)
    _fake_build(monkeypatch)
    agent = _seed_agent(db)
    db.commit()
    cli = _login(db)
    resp = cli.post(
        "/manage/agents/msi",
        data={"agent_id": agent.id, "api_key": "pn_live_key"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-msi"
    assert f"printer-nanny-agent-{agent.id}.msi" in resp.headers.get("content-disposition", "")
    assert resp.content == b"MSI\x00fake-artifact"
    audits = _audit(db)
    assert audits and any((a.detail or "").startswith("ok") for a in audits)
    # The baked-in key must never appear in an audit row.
    assert all("pn_live_key" not in (a.detail or "") for a in audits)


def test_build_route_unavailable_flashes_and_audits(db, monkeypatch):
    _fake_available(monkeypatch, False)
    agent = _seed_agent(db)
    db.commit()
    cli = _login(db)
    resp = cli.post(
        "/manage/agents/msi",
        data={"agent_id": agent.id, "api_key": "k"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    audits = _audit(db)
    assert audits and audits[0].detail.startswith("unavailable")


def test_build_route_failure_flashes_and_audits(db, monkeypatch):
    _fake_available(monkeypatch, True)

    def boom(**kw):
        raise RuntimeError("wixl exploded")

    monkeypatch.setattr(M, "build_msi", boom)
    agent = _seed_agent(db)
    db.commit()
    cli = _login(db)
    resp = cli.post(
        "/manage/agents/msi",
        data={"agent_id": agent.id, "api_key": "k"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    audits = _audit(db)
    assert audits and audits[0].detail.startswith("failed")
    assert "wixl exploded" in audits[0].detail


def test_build_route_client_readonly_denied(db, monkeypatch):
    _fake_available(monkeypatch, True)
    _fake_build(monkeypatch)
    agent = _seed_agent(db)
    db.commit()
    cli = _login(db, role=m.UserRole.client_readonly)
    resp = cli.post(
        "/manage/agents/msi",
        data={"agent_id": agent.id, "api_key": "k"},
        follow_redirects=False,
    )
    # Bounced to login; nothing built, nothing audited.
    assert resp.status_code in (302, 303)
    assert resp.headers.get("location", "").endswith("/login")
    assert _audit(db) == []


def test_build_route_agent_not_found(db, monkeypatch):
    _fake_available(monkeypatch, True)
    _fake_build(monkeypatch)
    cli = _login(db)
    resp = cli.post(
        "/manage/agents/msi",
        data={"agent_id": 9999, "api_key": "k"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert _audit(db) == []
