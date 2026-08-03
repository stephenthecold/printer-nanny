"""The macOS installer bundle.

WHAT THESE CAN AND CANNOT PROVE
-------------------------------
These cover the half that runs here: the bundle central assembles, its modes, and
the route that mints and rolls back the key. They prove **nothing** about
``pkgbuild`` -- that tool is macOS-only, so `.github/workflows/macos-pkg.yml` on a
`macos-latest` runner is the only place the package is actually built, exactly as
`windows-client.yml` is the only place the spooler is actually driven.

The invariants worth stating, because each is a failure that would not surface
until it was on somebody's fleet:

* the bundle carries a **live enrollment key**, so its mode is 0600 inside the
  archive and the download is ``no-store``;
* a build that fails **rolls the key back**, or a credential exists that nobody
  holds and nobody will revoke;
* every wheel is **pure-Python**, or the offline install fails on the Mac while
  succeeding on the build host;
* the plist and the install scripts are the **same files** ``deploy/`` holds, not
  second copies -- this repo has already been bitten by having two plists and
  shipping only one.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select

from fastapi.testclient import TestClient

from central import models as m
from central import pkg_builder
from central.main import app
from central.security import hash_password

ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "deploy" / "com.printernanny.workstation.plist"
PREINSTALL = ROOT / "deploy" / "macos-pkg" / "preinstall"
POSTINSTALL = ROOT / "deploy" / "macos-pkg" / "postinstall"
BUILD_SH = ROOT / "scripts" / "build-macos-pkg.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "macos-pkg.yml"

CONFIG_ARC = "payload/Library/Application Support/PrinterNanny/workstation.toml"
PLIST_ARC = "payload/Library/LaunchDaemons/com.printernanny.workstation.plist"


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    """One real bundle, built once. Slow (it builds wheels), so module-scoped."""
    out = tmp_path_factory.mktemp("pkg")
    return pkg_builder.build_workstation_pkg_bundle(
        client_name="Acme Health",
        client_id=7,
        central_url="https://central.example.com/",
        enroll_key="pnw_test_key_not_real",
        enroll_key_id=42,
        out_dir=out,
    )


@pytest.fixture(scope="module")
def members(bundle):
    with tarfile.open(bundle.path) as tar:
        return {mi.name: mi for mi in tar.getmembers()}


@pytest.fixture(scope="module")
def contents(bundle):
    with tarfile.open(bundle.path) as tar:
        return {
            mi.name: tar.extractfile(mi).read()
            for mi in tar.getmembers()
            if mi.isfile()
        }


def test_the_bundle_is_assembled_here(bundle):
    assert bundle.path.exists()
    assert bundle.size > 0
    assert bundle.identifier == "com.printernanny.workstation"


def test_the_capability_check_does_not_require_a_mac():
    """Reporting "unavailable" on Linux because pkgbuild is missing would be
    backwards -- assembling the bundle is the part that works here."""
    cap = pkg_builder.pkg_bundle_available()
    assert cap.available, cap.reason


def test_the_key_file_is_owner_only_inside_the_archive(members):
    """The one mode in here that is load-bearing. pkgbuild preserves payload
    modes, so a 0644 here becomes a world-readable key on every Mac."""
    assert oct(members[CONFIG_ARC].mode) == "0o600"


def test_every_other_payload_file_is_not_secret(members):
    for name, mi in members.items():
        if name == CONFIG_ARC or not mi.isfile():
            continue
        assert oct(mi.mode) in ("0o644", "0o755"), (name, oct(mi.mode))


def test_the_install_scripts_are_executable(members):
    """macOS Installer will not run a script it cannot execute, and says so only
    in /var/log/install.log."""
    assert oct(members["scripts/preinstall"].mode) == "0o755"
    assert oct(members["scripts/postinstall"].mode) == "0o755"
    assert oct(members["build-macos-pkg.sh"].mode) == "0o755"


def test_archive_entries_are_root_owned(members):
    assert all(mi.uid == 0 and mi.gid == 0 for mi in members.values())


def test_the_key_reaches_the_config_and_nothing_else(contents):
    config = contents[CONFIG_ARC].decode()
    assert 'enroll_key = "pnw_test_key_not_real"' in config
    for name, body in contents.items():
        if name == CONFIG_ARC:
            continue
        assert b"pnw_test_key_not_real" not in body, name


def test_the_plist_carries_no_credential(contents):
    """ProgramArguments and EnvironmentVariables are readable by any local user
    via launchctl print, and launchd requires the plist world-readable."""
    plist = contents[PLIST_ARC].decode()
    assert "pnw_test_key_not_real" not in plist
    assert "enroll_key" not in plist
    assert "workstation.toml" in plist  # only the PATH travels here


def test_the_plist_is_the_reviewable_one_verbatim(contents):
    """Two copies is how one of them drifts -- this repo already learned that
    about having a plist in deploy/ and another in an installer."""
    assert contents[PLIST_ARC] == PLIST.read_bytes()


def test_the_scripts_are_the_reviewable_ones_verbatim(contents):
    assert contents["scripts/preinstall"] == PREINSTALL.read_bytes()
    assert contents["scripts/postinstall"] == POSTINSTALL.read_bytes()
    assert contents["build-macos-pkg.sh"] == BUILD_SH.read_bytes()


def test_the_payload_carries_the_agent_wheel_and_its_deps(members, bundle):
    wheels = [n for n in members if n.endswith(".whl")]
    assert any("printer_nanny_agent-" in n for n in wheels)
    assert any("httpx-" in n for n in wheels)
    assert len(wheels) == bundle.wheel_count


def test_every_wheel_is_pure_python(members):
    """A platform-specific wheel installs on the build host and fails ON THE MAC,
    which is the worst place for it to surface. The builder refuses one; this is
    the tripwire for a dependency that grows a C extension."""
    for name in members:
        if name.endswith(".whl"):
            assert name.endswith("-py3-none-any.whl"), name


def test_the_readme_says_the_bundle_is_a_secret(contents):
    readme = contents["README.md"].decode()
    assert "live credential" in readme.lower()
    assert "enrollment key #42" in readme
    assert "Do not commit it" in readme


def test_the_readme_names_the_key_id_not_the_key(contents):
    readme = contents["README.md"].decode()
    assert "pnw_test_key_not_real" not in readme


def test_the_env_file_pins_the_identifier_and_version(contents, bundle):
    env = contents["pkg.env"].decode()
    assert 'PN_PKG_IDENTIFIER="com.printernanny.workstation"' in env
    assert f'PN_PKG_VERSION="{bundle.agent_version}"' in env


def test_a_bundle_without_a_key_is_refused(tmp_path):
    with pytest.raises(ValueError, match="enroll_key"):
        pkg_builder.build_workstation_pkg_bundle(
            client_name="x", client_id=1, central_url="https://c",
            enroll_key="", enroll_key_id=1, out_dir=tmp_path,
        )


def test_the_wheelhouse_refuses_a_platform_specific_wheel(tmp_path, monkeypatch):
    """Simulated, because this host cannot produce a macOS-only wheel. The guard
    is what matters, and it must refuse rather than ship something that fails on
    the target."""
    real = pkg_builder.subprocess.run

    def fake(argv, **kw):
        if "wheel" in argv:
            (tmp_path / "cffi-1.0-cp312-cp312-manylinux_x86_64.whl").write_bytes(b"x")

            class R:
                returncode = 0
                stdout = stderr = ""

            return R()
        return real(argv, **kw)

    monkeypatch.setattr(pkg_builder.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="platform-specific"):
        pkg_builder.build_wheelhouse(tmp_path)


def test_an_empty_wheelhouse_is_refused(tmp_path, monkeypatch):
    def fake(argv, **kw):
        class R:
            returncode = 0
            stdout = stderr = ""

        return R()

    monkeypatch.setattr(pkg_builder.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="no wheels"):
        pkg_builder.build_wheelhouse(tmp_path)


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


def _manager(db, username="pkgadmin"):
    u = m.User(username=username, password_hash=hash_password("pw12345678"),
               role=m.UserRole.admin)
    db.add(u)
    db.commit()
    return u


def _login(username="pkgadmin"):
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw12345678"},
             follow_redirects=False)
    return cli


def test_the_route_mints_a_key_and_serves_the_bundle(db):
    _manager(db)
    c = m.Client(name="Acme")
    db.add(c)
    db.commit()
    client = _login()

    before = len(list(db.scalars(select(m.WorkstationEnrollKey))))
    resp = client.post("/manage/machines/pkg", data={"client_id": c.id},
                       follow_redirects=False)
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["content-type"] == "application/gzip"
    # no-store, not merely no-cache: this body is a credential.
    assert "no-store" in resp.headers.get("cache-control", "")
    db.expire_all()
    keys = list(db.scalars(select(m.WorkstationEnrollKey)))
    assert len(keys) == before + 1
    assert "macOS pkg build" in keys[-1].label


def test_the_download_is_a_real_bundle(db):
    _manager(db)
    c = m.Client(name="Acme")
    db.add(c)
    db.commit()
    client = _login()

    resp = client.post("/manage/machines/pkg", data={"client_id": c.id},
                       follow_redirects=False)
    assert resp.status_code == 200
    import io

    with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
        names = tar.getnames()
    assert CONFIG_ARC in names
    assert "build-macos-pkg.sh" in names
    assert any(n.endswith(".whl") for n in names)


def test_a_failed_build_rolls_the_key_back(db, monkeypatch):
    """A key minted for an installer that was never produced is a live credential
    nobody holds and nobody will think to revoke -- worse than no key, because it
    looks legitimate in the list."""
    _manager(db)
    c = m.Client(name="Acme")
    db.add(c)
    db.commit()
    client = _login()

    import central.pkg_builder as pb

    def boom(**kw):
        raise RuntimeError("wheels exploded")

    monkeypatch.setattr(pb, "build_workstation_pkg_bundle", boom)

    before = len(list(db.scalars(select(m.WorkstationEnrollKey))))
    resp = client.post("/manage/machines/pkg", data={"client_id": c.id},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    db.expire_all()
    assert len(list(db.scalars(select(m.WorkstationEnrollKey)))) == before
    audits = [
        a for a in db.scalars(select(m.AuditLog))
        if a.action == "workstation.pkg_bundle"
    ]
    assert audits and "failed" in audits[-1].detail


def test_the_audit_records_the_key_id_never_the_key(db):
    _manager(db)
    c = m.Client(name="Acme")
    db.add(c)
    db.commit()
    client = _login()

    client.post("/manage/machines/pkg", data={"client_id": c.id},
                       follow_redirects=False)
    db.expire_all()
    audits = [
        a for a in db.scalars(select(m.AuditLog))
        if a.action == "workstation.pkg_bundle"
    ]
    assert audits
    detail = audits[-1].detail
    assert "enroll_key=" in detail
    assert "identifier=com.printernanny.workstation" in detail
    assert "pnw_" not in detail


def test_the_route_is_manager_only(db):
    c = m.Client(name="Acme")
    db.add(c)
    db.commit()
    client = TestClient(app)  # not logged in
    resp = client.post("/manage/machines/pkg", data={"client_id": c.id},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("location", "")


def test_an_unknown_client_does_not_mint_a_key(db):
    _manager(db)
    db.commit()
    client = _login()
    before = len(list(db.scalars(select(m.WorkstationEnrollKey))))
    resp = client.post("/manage/machines/pkg", data={"client_id": 99999},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    db.expire_all()
    assert len(list(db.scalars(select(m.WorkstationEnrollKey)))) == before


# --------------------------------------------------------------------------- #
# The scripts and the workflow
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="no bash on PATH to syntax-check with"
)
@pytest.mark.parametrize("script", [PREINSTALL, POSTINSTALL, BUILD_SH])
def test_scripts_parse(script):
    import subprocess

    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script", [PREINSTALL, POSTINSTALL, BUILD_SH])
def test_scripts_are_executable(script, git_file_mode):
    """pkgbuild takes these modes from the checkout, so git's is the one that
    ships. Asserted there rather than via stat() -- see the fixture."""
    mode = git_file_mode(script)
    if mode is None:
        pytest.skip("not a git checkout; the exec bit is a repository property")
    assert mode == "100755", f"{script.name} is {mode} in git, not 100755"
    if os.name != "nt":
        assert script.stat().st_mode & 0o111, script.name


def test_postinstall_installs_offline():
    """--no-index is the whole reason the wheelhouse is in the payload. Without
    it an MDM push onto a Mac behind a captive portal fails."""
    body = POSTINSTALL.read_text(encoding="utf-8")
    assert "--no-index" in body
    assert "--find-links" in body


def test_postinstall_aborts_on_error():
    """A postinstall that half-completes leaves a service that cannot start, and
    macOS Installer reporting success for that is the exact failure this codebase
    is organised against."""
    assert any(ln.startswith("set -e") for ln in _effective_lines(POSTINSTALL))


def test_postinstall_fails_loudly():
    """A postinstall that half-completes leaves a service that cannot start, and
    macOS Installer reporting success for that is the exact failure this codebase
    is organised against."""
    body = POSTINSTALL.read_text(encoding="utf-8")
    assert "launchctl print" in body  # verifies the daemon actually loaded
    assert "die " in body


def test_postinstall_reasserts_the_key_mode():
    body = POSTINSTALL.read_text(encoding="utf-8")
    assert "chmod 600" in body
    assert "chmod 700" in body


def _effective_lines(path: Path) -> list:
    """Script lines with comments and blanks removed.

    Substring-matching a whole script is a weak assertion: both of these scripts
    *discuss* `set -e` and `set -x` in their comments, so a naive `in body` check
    matched the prose explaining why the option is absent.
    """
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def test_preinstall_does_not_abort_the_install():
    """`launchctl bootout` on a label that is not loaded exits non-zero, and a
    preinstall that aborts leaves the Mac with the old install and an error."""
    lines = _effective_lines(PREINSTALL)
    assert not [ln for ln in lines if ln.startswith("set -e")]
    assert "exit 0" in lines


def test_the_build_script_refuses_to_run_off_a_mac():
    body = BUILD_SH.read_text(encoding="utf-8")
    assert 'uname -s' in body
    assert "Darwin" in body


def test_the_build_script_takes_credentials_from_the_environment():
    """A process's command line is readable by every user on the machine, and one
    of these is an App Store Connect key."""
    body = BUILD_SH.read_text(encoding="utf-8")
    for var in ("PN_SIGN_IDENTITY", "PN_TEAM_ID", "PN_ASC_KEY_ID", "PN_APPLE_PASSWORD"):
        assert var in body
    # No --password/--sign taken as a script argument.
    assert "--password)" not in body
    assert "--sign)" not in body


@pytest.mark.parametrize("script", [PREINSTALL, POSTINSTALL, BUILD_SH])
def test_no_script_enables_xtrace(script):
    """set -x would echo the notarization credentials and the enrollment key --
    notarytool takes its credentials in argv because it accepts them no other
    way, so tracing is the one thing that must never be switched on."""
    assert not [ln for ln in _effective_lines(script) if "set -x" in ln]


def test_the_build_script_staples():
    """Without stapling, a Mac with no route to Apple refuses a perfectly
    notarized package -- which describes a segmented client VLAN."""
    body = BUILD_SH.read_text(encoding="utf-8")
    assert "stapler staple" in body
    assert "stapler validate" in body


def test_the_build_script_asks_gatekeeper_rather_than_assuming():
    assert "spctl --assess" in BUILD_SH.read_text(encoding="utf-8")


def test_the_build_script_asserts_the_key_mode():
    """A bundle that travelled through a tool which normalised modes would
    otherwise produce an installer whose key is world-readable everywhere."""
    body = BUILD_SH.read_text(encoding="utf-8")
    assert "stat -f" in body
    assert "chmod 600" in body


def test_the_unsigned_build_says_it_is_unsigned():
    """An operator must not think an unsigned package is double-clickable."""
    body = BUILD_SH.read_text(encoding="utf-8")
    assert "NOT signed" in body
    assert "Gatekeeper will refuse it" in body


def test_the_workflow_runs_on_macos_and_is_path_filtered():
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert wf["jobs"]["build"]["runs-on"] == "macos-latest"
    # macOS runner minutes bill at 10x on private repos.
    assert "paths" in wf[True]["pull_request"]


def test_the_signing_gate_can_actually_evaluate():
    """A step's own `env:` is NOT visible to that step's `if`, so testing it there
    is always false and signing silently never runs."""
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = wf["jobs"]["build"]
    sign = [s for s in job["steps"] if str(s.get("name", "")).startswith("Sign")][0]
    assert "PN_SIGN_IDENTITY" in job.get("env", {})
    assert "env.PN_SIGN_IDENTITY" in sign["if"]


def test_the_workflow_checks_the_key_mode_survived_the_round_trip():
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "expected 600" in body


def test_the_workflow_deletes_the_signing_keychain():
    """A Developer ID certificate must not be left on a shared runner."""
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "create-keychain" in body
    assert "delete-keychain" in body


def test_the_component_package_is_kept_out_of_the_output_directory():
    """It is installable but carries no Distribution -- so no volume-check and no
    product identity -- which means an operator who picks it by mistake installs
    onto an OS the real installer would have refused. Two .pkg files side by side
    under one name is how that mistake gets made."""
    body = BUILD_SH.read_text(encoding="utf-8")
    assert 'BUILD_DIR="$OUT_DIR/.build"' in body
    assert 'COMPONENT="$BUILD_DIR/' in body
    assert '--package-path "$BUILD_DIR"' in body


def test_the_workflow_refuses_more_than_one_shippable_package():
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "holds more than one .pkg" in body
