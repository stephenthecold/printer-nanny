"""The workstation MSI: profile separation, baked config, and the build route.

wixl lives in the central Docker image, not in the test environment, so the
end-to-end build is skipped here exactly as the agent's is. What IS checked
without it is the part that has actually gone wrong in this codebase before:
the generated WiX source. A malformed component or a shared UpgradeCode
produces an installer that builds cleanly and then misbehaves on a real
machine, which is the failure mode worth catching in CI.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import msi_builder as mb
from central.main import app
from central.security import hash_password

_HAVE_WIXL = shutil.which("wixl") is not None
_WIX_NS = "{http://schemas.microsoft.com/wix/2006/wi}"


def _payload(tmp_path):
    """A minimal install tree shaped like the real one."""
    root = tmp_path / "payload"
    (root / "python").mkdir(parents=True)
    (root / "python" / "python.exe").write_bytes(b"MZ fake")
    (root / "nssm.exe").write_bytes(b"MZ fake")
    (root / "workstation.toml").write_text("server = \"https://x\"\n")
    return root


# --------------------------- product separation ----------------------------- #


def test_the_two_products_have_different_upgrade_codes():
    """A shared UpgradeCode makes Windows treat them as one product, so
    installing the workstation client would silently uninstall the site agent.
    An MSP's own server legitimately runs both."""
    assert mb.AGENT_PROFILE.upgrade_code != mb.WORKSTATION_PROFILE.upgrade_code


def test_the_two_products_have_different_service_names_and_dirs():
    """Sharing either would make one product's uninstall break the other."""
    assert mb.AGENT_PROFILE.service_name != mb.WORKSTATION_PROFILE.service_name
    assert mb.AGENT_PROFILE.install_dir_name != mb.WORKSTATION_PROFILE.install_dir_name


def test_the_agent_wxs_is_unchanged_by_the_profile_refactor(tmp_path):
    """The refactor threaded a parameter through; it must not have moved the
    agent's own output."""
    wxs = mb.generate_wxs(_payload(tmp_path), product_version="1.2.3")
    assert mb.UPGRADE_CODE in wxs
    assert mb.SERVICE_NAME in wxs
    assert 'Name="Agent"' in wxs


def test_the_workstation_wxs_carries_its_own_identity(tmp_path):
    wxs = mb.generate_wxs(
        _payload(tmp_path), product_version="1.2.3", profile=mb.WORKSTATION_PROFILE
    )
    assert mb.WS_UPGRADE_CODE in wxs
    assert mb.WS_SERVICE_NAME in wxs
    assert 'Name="Workstation"' in wxs
    assert mb.UPGRADE_CODE not in wxs
    assert mb.SERVICE_NAME not in wxs


# ------------------------------- the WXS ------------------------------------ #


def test_workstation_wxs_is_well_formed_xml(tmp_path):
    wxs = mb.generate_wxs(
        _payload(tmp_path), product_version="1.2.3", profile=mb.WORKSTATION_PROFILE
    )
    ET.fromstring(wxs)  # raises if malformed


def test_workstation_wxs_every_component_has_a_guid_and_is_referenced(tmp_path):
    """A component with no GUID, or one no Feature references, installs nothing
    -- with no error at build time."""
    wxs = mb.generate_wxs(
        _payload(tmp_path), product_version="1.2.3", profile=mb.WORKSTATION_PROFILE
    )
    root = ET.fromstring(wxs)
    comps = {c.get("Id") for c in root.iter(f"{_WIX_NS}Component")}
    refs = {r.get("Id") for r in root.iter(f"{_WIX_NS}ComponentRef")}
    assert comps, "no components emitted"
    for c in root.iter(f"{_WIX_NS}Component"):
        assert c.get("Guid"), f"component {c.get('Id')} has no GUID"
    assert comps == refs, "every component must be referenced exactly once"


def test_workstation_wxs_declares_its_service(tmp_path):
    wxs = mb.generate_wxs(
        _payload(tmp_path), product_version="1.2.3", profile=mb.WORKSTATION_PROFILE
    )
    root = ET.fromstring(wxs)
    installs = list(root.iter(f"{_WIX_NS}ServiceInstall"))
    assert len(installs) == 1
    assert installs[0].get("Name") == mb.WS_SERVICE_NAME
    # NSSM takes its own service name from argv[1]; without this the service
    # starts and reads nobody's parameters.
    assert installs[0].get("Arguments") == mb.WS_SERVICE_NAME


def test_the_enroll_key_never_reaches_the_service_command_line(tmp_path):
    """A service's command line is readable by any logged-in user, so a key
    passed as an argument is published to everyone at the machine."""
    wxs = mb.generate_wxs(
        _payload(tmp_path), product_version="1.2.3", profile=mb.WORKSTATION_PROFILE
    )
    assert "--config" in wxs
    assert "enroll" not in mb.WORKSTATION_PROFILE.app_parameters.lower()
    assert "--enroll-key" not in wxs


def test_the_registry_points_at_the_bundled_python(tmp_path):
    wxs = mb.generate_wxs(
        _payload(tmp_path), product_version="1.2.3", profile=mb.WORKSTATION_PROFILE
    )
    assert "python\\python.exe" in wxs
    assert "printer_nanny_agent.workstation_cli" in wxs


# ------------------------------ the config ---------------------------------- #


@pytest.mark.skipif(
    __import__("sys").version_info < (3, 11), reason="tomllib is 3.11+"
)
def test_the_rendered_config_is_valid_toml_and_carries_the_key():
    import tomllib

    cfg = mb.render_workstation_config(
        central_url="https://pn.example.com/", enroll_key="pnw_abc", interval=120
    )
    parsed = tomllib.loads(cfg)
    assert parsed["server"] == "https://pn.example.com"  # trailing slash trimmed
    assert parsed["enroll_key"] == "pnw_abc"
    assert parsed["interval"] == 120
    assert parsed["verify_tls"] is True


@pytest.mark.skipif(
    __import__("sys").version_info < (3, 11), reason="tomllib is 3.11+"
)
def test_a_hostile_key_or_url_cannot_break_the_config():
    """These are generated values today, but a malformed file is a silently
    un-startable client, so escape rather than trust."""
    import tomllib

    cfg = mb.render_workstation_config(
        central_url='https://x/"evil', enroll_key='a"b\\c'
    )
    parsed = tomllib.loads(cfg)
    assert parsed["enroll_key"] == 'a"b\\c'


def test_a_config_with_no_key_is_refused():
    """An installer with no credential enrolls nothing and reports success."""
    with pytest.raises(ValueError):
        mb.render_workstation_config(central_url="https://x", enroll_key="")


def test_the_client_reads_the_key_from_the_file_not_the_command_line(tmp_path):
    """The other half of the contract, asserted from the agent side."""
    from printer_nanny_agent import workstation_cli

    path = tmp_path / "workstation.toml"
    path.write_text(mb.render_workstation_config(
        central_url="https://pn.example.com", enroll_key="pnw_fromfile"))
    cfg = workstation_cli.load_config(str(path))
    assert cfg["enroll_key"] == "pnw_fromfile"
    assert cfg["server"] == "https://pn.example.com"


def test_a_missing_or_corrupt_config_is_not_fatal(tmp_path):
    """The flags still work, which is what makes the client runnable by hand."""
    from printer_nanny_agent import workstation_cli

    assert workstation_cli.load_config(str(tmp_path / "nope.toml")) == {}
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not = valid = toml [[[")
    assert workstation_cli.load_config(str(bad)) == {}


# ------------------------------- the route ---------------------------------- #


def _admin(db, username="admin1"):
    u = m.User(username=username, password_hash=hash_password("pw12345678"),
               role=m.UserRole.admin)
    db.add(u)
    db.commit()
    return u


def _login(username="admin1"):
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw12345678"},
             follow_redirects=False)
    return cli


def _client(db, name="Acme"):
    c = m.Client(name=name)
    db.add(c)
    db.commit()
    return c


def test_building_mints_a_fresh_key_per_installer(db, monkeypatch):
    """Keys are hashed at rest, so an existing one cannot be baked in -- and
    minting per build is better anyway: each installer is revocable alone."""
    c = _client(db)
    _admin(db)

    class Cap:
        available, reason, wixl = True, "", "/usr/bin/wixl"

    built = {}

    def fake_build(**kwargs):
        built.update(kwargs)
        out = kwargs["out_dir"] / "printer-nanny-workstation-test.msi"
        out.write_bytes(b"MSI")
        return mb.WorkstationMsiBuildResult(
            path=out, size=3, product_version="1.0.0", agent_version="0.12.0",
            client_id=kwargs["client_id"], enroll_key_id=kwargs.get("enroll_key_id"),
        )

    monkeypatch.setattr(mb, "msi_build_available", lambda: Cap())
    monkeypatch.setattr(mb, "build_workstation_msi", fake_build)

    r = _login().post("/manage/machines/msi", data={"client_id": c.id},
                      follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-msi"

    key = db.scalar(select(m.WorkstationEnrollKey))
    assert key is not None and key.client_id == c.id
    # The key handed to the builder is the plaintext; what is stored is its hash.
    from central.security import hash_enroll_key
    assert key.key_hash == hash_enroll_key(built["enroll_key"])
    assert key.key_hash != built["enroll_key"]


def test_a_failed_build_does_not_leave_a_live_key_behind(db, monkeypatch):
    """A key minted for an installer that was never produced is a credential
    nobody holds and nobody will think to revoke."""
    c = _client(db)
    _admin(db)

    class Cap:
        available, reason, wixl = True, "", "/usr/bin/wixl"

    def boom(**kwargs):
        raise RuntimeError("wixl exploded")

    monkeypatch.setattr(mb, "msi_build_available", lambda: Cap())
    monkeypatch.setattr(mb, "build_workstation_msi", boom)

    _login().post("/manage/machines/msi", data={"client_id": c.id},
                  follow_redirects=False)
    assert db.scalar(select(m.WorkstationEnrollKey)) is None
    failure = db.scalar(select(m.AuditLog).where(
        m.AuditLog.action == "workstation.msi_build"))
    assert failure is not None and "failed" in (failure.detail or "")


def test_the_audit_row_records_the_key_id_never_the_key(db, monkeypatch):
    c = _client(db)
    _admin(db)

    class Cap:
        available, reason, wixl = True, "", "/usr/bin/wixl"

    holder = {}

    def fake_build(**kwargs):
        holder["key"] = kwargs["enroll_key"]
        out = kwargs["out_dir"] / "x.msi"
        out.write_bytes(b"MSI")
        return mb.WorkstationMsiBuildResult(
            path=out, size=3, product_version="1.0.0", agent_version="0.12.0",
            client_id=kwargs["client_id"], enroll_key_id=kwargs.get("enroll_key_id"))

    monkeypatch.setattr(mb, "msi_build_available", lambda: Cap())
    monkeypatch.setattr(mb, "build_workstation_msi", fake_build)
    _login().post("/manage/machines/msi", data={"client_id": c.id},
                  follow_redirects=False)

    row = db.scalar(select(m.AuditLog).where(
        m.AuditLog.action == "workstation.msi_build"))
    assert row is not None
    assert holder["key"] not in (row.detail or ""), "the key must never be audited"
    assert "enroll_key=" in (row.detail or "")


def test_an_unavailable_toolchain_explains_itself_and_mints_nothing(db, monkeypatch):
    c = _client(db)
    _admin(db)

    class Cap:
        available, reason, wixl = False, "msitools not installed", None

    monkeypatch.setattr(mb, "msi_build_available", lambda: Cap())
    r = _login().post("/manage/machines/msi", data={"client_id": c.id},
                      follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert db.scalar(select(m.WorkstationEnrollKey)) is None


def test_a_readonly_user_cannot_build(db):
    c = _client(db)
    db.add(m.User(username="ro", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.client_readonly))
    db.commit()
    r = _login("ro").post("/manage/machines/msi", data={"client_id": c.id},
                          follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


@pytest.mark.skipif(not _HAVE_WIXL, reason="msitools/wixl not installed")
def test_build_workstation_msi_end_to_end(tmp_path):  # pragma: no cover
    """Only runs where msitools exists -- the central Docker image."""
    result = mb.build_workstation_msi(
        client_name="Acme", client_id=1, central_url="https://pn.example.com",
        enroll_key="pnw_test", out_dir=tmp_path,
    )
    assert result.path.exists() and result.size > 0
    summary = mb.validate_msi(result.path)
    assert summary["has_service"] and summary["has_registry"]
