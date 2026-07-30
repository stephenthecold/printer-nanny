"""Vendor driver staging.

This feature installs an operator-uploaded binary as LocalSystem on every
machine in a client that needs it. That is code execution across a fleet by
design, so most of this file is about the controls that bound it: the checksum,
the extraction guard, tenancy on the download, and refusing to guess which
driver a printer needs.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import driver_store
from central import models as m
from central import services
from central.main import app
from central.security import (
    generate_enroll_key,
    hash_enroll_key,
    hash_password,
)
from printer_nanny_agent import workstation_service as svc
from printer_nanny_agent.platforms import windows as windows_backend


@pytest.fixture(autouse=True)
def _windows_backend(monkeypatch):
    """Driver staging is the Windows pnputil path, so drive that backend
    explicitly rather than whatever the test host happens to be."""
    monkeypatch.setattr(svc, "_platform", lambda: windows_backend)


# --------------------------------- helpers --------------------------------- #


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


def _client_with_printer(db, name, ip, model="Brother HL-L2350DW series"):
    c = m.Client(name=name)
    db.add(c)
    db.flush()
    site = m.Site(client_id=c.id, name=f"{name} HQ")
    db.add(site)
    db.flush()
    p = m.Printer(client_id=c.id, site_id=site.id, ip=ip, model=model,
                  display_name=f"{name} MFP",
                  driver_tier=m.DriverTier.driver_required,
                  discovery_state=m.DiscoveryState.approved)
    db.add(p)
    db.commit()
    return c, p


def _zip_bytes(entries) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _package(db, client, tmp_path, *, model="HL-L2350DW", driver="Brother HL-L2350DW",
             inf="x64/BRPRF.INF", payload=None):
    """A stored package, as the upload route would leave it."""
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    blob = payload if payload is not None else _zip_bytes(
        [(inf, b"; fake inf\n"), ("x64/driver.dll", b"MZ")])
    row = m.DriverPackage(client_id=client.id, name="pkg", driver_name=driver,
                          inf_relpath=inf, model=model, sha256="", size=0)
    db.add(row)
    db.flush()
    sha, size = driver_store.save(client.id, row.id, io.BytesIO(blob))
    row.sha256, row.size = sha, size
    row.stored_at = str(driver_store.path_for(client.id, row.id))
    db.commit()
    return row, blob


def _enroll_key_for(db, client):
    key = generate_enroll_key()
    db.add(m.WorkstationEnrollKey(client_id=client.id, key_hash=hash_enroll_key(key),
                                  label="MSI"))
    db.commit()
    return key


def _enrolled(db, client, uid="GUID-A"):
    return TestClient(app).post("/api/v1/workstations/enroll", json={
        "enroll_key": _enroll_key_for(db, client), "machine_uid": uid, "name": "PC",
    }).json()


# ------------------------------ the store ---------------------------------- #


def test_the_storage_path_never_contains_a_filename(tmp_path):
    """A filename is operator-supplied; a path built from one is a traversal."""
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    path = driver_store.path_for(7, 42)
    assert path.name == "42.pkg"
    assert "7" in path.parts


def test_saving_streams_and_returns_the_digest_of_what_was_written(tmp_path):
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    blob = b"driver bytes" * 1000
    sha, size = driver_store.save(1, 1, io.BytesIO(blob))
    assert sha == hashlib.sha256(blob).hexdigest()
    assert size == len(blob)
    assert driver_store.path_for(1, 1).read_bytes() == blob


def test_an_oversize_upload_is_refused_and_leaves_nothing_behind(tmp_path, monkeypatch):
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    monkeypatch.setattr(driver_store, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(ValueError):
        driver_store.save(1, 1, io.BytesIO(b"x" * 500))
    assert not driver_store.path_for(1, 1).exists()
    assert not list(driver_store.path_for(1, 1).parent.glob("*.part"))


def test_a_missing_file_is_detectable(tmp_path):
    """The expected state after restoring a database onto a fresh host."""
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    assert driver_store.missing(str(tmp_path / "nope.pkg")) is True
    driver_store.save(1, 1, io.BytesIO(b"x"))
    assert driver_store.missing(str(driver_store.path_for(1, 1))) is False


# ------------------------------ resolution --------------------------------- #


def test_a_package_matches_on_a_model_substring(db, tmp_path):
    """SNMP model strings vary, so equality fails on the real reading."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1",
                                      model="Brother HL-L2350DW series")
    pkg, _ = _package(db, c, tmp_path, model="HL-L2350DW")
    assert services.driver_package_for(db, printer) is pkg


def test_the_more_specific_package_wins(db, tmp_path):
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1",
                                      model="Brother HL-L2350DW series")
    _package(db, c, tmp_path, model="Brother")
    exact, _ = _package(db, c, tmp_path, model="HL-L2350DW")
    assert services.driver_package_for(db, printer) is exact


def test_an_equal_tie_is_refused_rather_than_guessed(db, tmp_path):
    """The wrong driver prints garbage, which is worse than no queue."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1", model="HL-L2350DW")
    _package(db, c, tmp_path, model="L2350", driver="A")
    _package(db, c, tmp_path, model="23501"[:5], driver="B")
    # Two tags of equal length, both substrings? Force the exact tie:
    for row in db.scalars(select(m.DriverPackage)):
        row.model = "L2350"
    db.commit()
    assert services.driver_package_for(db, printer) is None


def test_a_too_short_tag_never_matches(db, tmp_path):
    """A 2-character tag is a substring of nearly every model string."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1", model="HL-L2350DW")
    pkg, _ = _package(db, c, tmp_path, model="HL")
    pkg.model = "HL"
    db.commit()
    assert services.driver_package_for(db, printer) is None


def test_packages_never_cross_a_tenant(db, tmp_path):
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1", model="HL-L2350DW")
    globex = m.Client(name="Globex")
    db.add(globex)
    db.commit()
    _package(db, globex, tmp_path, model="HL-L2350DW")
    assert services.driver_package_for(db, printer) is None


def test_a_package_whose_file_is_gone_does_not_match(db, tmp_path):
    """Returning it would send the workstation to a download that 410s."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1", model="HL-L2350DW")
    pkg, _ = _package(db, c, tmp_path, model="HL-L2350DW")
    os.unlink(pkg.stored_at)
    assert services.driver_package_for(db, printer) is None


# ------------------------------- the API ----------------------------------- #


def test_assignments_carry_the_driver_for_driver_required_printers(db, tmp_path):
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1", model="HL-L2350DW")
    pkg, _ = _package(db, c, tmp_path, model="HL-L2350DW")
    enrolled = _enrolled(db, c)
    db.add(m.PrinterAssignment(printer_id=printer.id,
                               machine_id=enrolled["machine_id"]))
    db.commit()

    body = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"}).json()
    driver = body["printers"][0]["driver"]
    assert driver["package_id"] == pkg.id
    assert driver["sha256"] == pkg.sha256
    assert driver["driver_name"] == pkg.driver_name


def test_a_driverless_printer_is_never_sent_a_vendor_driver(db, tmp_path):
    """Windows picks the inbox driver there; offering a choice invites the
    client to make one it has no business making."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1", model="HL-L2350DW")
    printer.driver_tier = m.DriverTier.driverless
    printer.ipp_endpoint = "ipp://10.0.0.1:631/ipp/print"
    db.commit()
    _package(db, c, tmp_path, model="HL-L2350DW")
    enrolled = _enrolled(db, c)
    db.add(m.PrinterAssignment(printer_id=printer.id,
                               machine_id=enrolled["machine_id"]))
    db.commit()

    body = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"}).json()
    assert body["printers"][0]["driver"] is None


def test_a_machine_cannot_download_another_tenants_package(db, tmp_path):
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex = m.Client(name="Globex")
    db.add(globex)
    db.commit()
    other_pkg, _ = _package(db, globex, tmp_path)
    enrolled = _enrolled(db, acme)

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/drivers/{other_pkg.id}",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    # 404, not 403 -- the same answer as "no such package", so this cannot be
    # used to probe which package ids exist.
    assert r.status_code == 404


def test_downloading_needs_the_machines_own_key(db, tmp_path):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    pkg, _ = _package(db, c, tmp_path)
    enrolled = _enrolled(db, c)

    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert TestClient(app).get(
            f"/api/v1/workstations/{enrolled['machine_id']}/drivers/{pkg.id}",
            headers=headers).status_code == 401


def test_a_package_with_a_missing_file_reports_gone_not_success(db, tmp_path):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    pkg, _ = _package(db, c, tmp_path)
    enrolled = _enrolled(db, c)
    os.unlink(pkg.stored_at)

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/drivers/{pkg.id}",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    assert r.status_code == 410


# ------------------------- extraction is the attack ------------------------- #


def test_an_archive_entry_that_escapes_is_refused(tmp_path):
    """The classic zip-slip: extracted naively this writes into System32 as
    LocalSystem. extractall does NOT protect against it."""
    archive = tmp_path / "evil.zip"
    archive.write_bytes(_zip_bytes([("../../escaped.txt", b"pwned")]))
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(svc.DriverError):
        svc._safe_extract(str(archive), str(dest))
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_a_windows_absolute_entry_is_refused_even_on_posix(tmp_path):
    """os.path.isabs on Linux calls C:\\Windows relative, which is exactly
    wrong for an archive that will be unpacked on Windows."""
    assert svc.ntpath_is_abs(r"C:\Windows\System32\evil.dll") is True
    assert svc.ntpath_is_abs(r"\\server\share\evil.dll") is True
    assert svc.ntpath_is_abs(r"x64/BRPRF.INF") is False

    archive = tmp_path / "evil.zip"
    archive.write_bytes(_zip_bytes([(r"C:\Windows\evil.dll", b"MZ")]))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(svc.DriverError):
        svc._safe_extract(str(archive), str(dest))


def test_a_normal_archive_extracts(tmp_path):
    archive = tmp_path / "ok.zip"
    archive.write_bytes(_zip_bytes([("x64/BRPRF.INF", b"; inf"), ("readme.txt", b"hi")]))
    dest = tmp_path / "out"
    dest.mkdir()
    svc._safe_extract(str(archive), str(dest))
    assert (dest / "x64" / "BRPRF.INF").read_bytes() == b"; inf"


def test_an_inf_path_that_escapes_the_package_is_refused(tmp_path):
    """inf_relpath is operator-typed; a path resolving outside the package
    would hand pnputil an arbitrary file on the workstation."""
    pkg = tmp_path / "pkg"
    (pkg / "x64").mkdir(parents=True)
    (pkg / "x64" / "BRPRF.INF").write_bytes(b"; inf")

    assert svc.inf_path_in(str(pkg), "x64/BRPRF.INF").endswith("BRPRF.INF")
    with pytest.raises(svc.DriverError):
        svc.inf_path_in(str(pkg), "../../../../etc/passwd")
    with pytest.raises(svc.DriverError):
        svc.inf_path_in(str(pkg), "x64/nope.inf")


# --------------------------- fetch + verify -------------------------------- #


class _FakeClient:
    def __init__(self, blob, corrupt=False):
        self.blob = b"tampered" + blob if corrupt else blob
        self.downloads = 0

    def download_driver(self, package_id, dest):
        self.downloads += 1
        with open(dest, "wb") as fp:
            fp.write(self.blob)


def test_a_tampered_package_never_reaches_extraction(tmp_path):
    """Verified BEFORE opening the archive -- checking after unpacking would
    mean a hostile archive was already written to disk."""
    blob = _zip_bytes([("x64/BRPRF.INF", b"; inf")])
    driver = {"package_id": 1, "sha256": hashlib.sha256(blob).hexdigest()}
    client = _FakeClient(blob, corrupt=True)

    with pytest.raises(svc.DriverError, match="checksum"):
        svc.fetch_driver(client, driver, str(tmp_path / "cache"))
    assert not list((tmp_path / "cache").glob("*/x64"))


def test_a_good_package_unpacks_and_is_cached_by_digest(tmp_path):
    blob = _zip_bytes([("x64/BRPRF.INF", b"; inf")])
    sha = hashlib.sha256(blob).hexdigest()
    driver = {"package_id": 1, "sha256": sha}
    client = _FakeClient(blob)
    cache = str(tmp_path / "cache")

    first = svc.fetch_driver(client, driver, cache)
    assert os.path.isfile(os.path.join(first, "x64", "BRPRF.INF"))
    assert client.downloads == 1

    # Second poll must not re-download -- this runs every few minutes.
    second = svc.fetch_driver(client, driver, cache)
    assert second == first
    assert client.downloads == 1
    assert os.path.basename(first) == sha, "cached by digest, not by package id"


def test_a_package_with_no_checksum_is_refused(tmp_path):
    with pytest.raises(svc.DriverError):
        svc.fetch_driver(_FakeClient(b""), {"package_id": 1, "sha256": ""},
                         str(tmp_path))


# ------------------------- provisioning behaviour --------------------------- #


def _payload(**driver):
    printer = {"printer_id": 1, "name": "MFP", "ip": "10.0.0.1", "is_default": False,
               "driver_tier": "driver_required", "ipp_endpoint": None}
    printer["driver"] = driver or None
    return {"printers": [printer]}


def test_driver_required_still_skips_when_no_package_exists(monkeypatch):
    monkeypatch.setattr(svc.ws, "reconcile", lambda *a, **k: {})
    report = svc.provision(object(), _payload())
    assert not report.outcomes
    assert "no driver package configured" in list(report.skipped.values())[0]


def test_driver_required_provisions_when_a_package_is_available(tmp_path, monkeypatch):
    blob = _zip_bytes([("x64/BRPRF.INF", b"; inf")])
    sha = hashlib.sha256(blob).hexdigest()
    seen = {}

    def fake_reconcile(runner, desired, managed_prefix=""):
        seen["desired"] = desired
        return {d["name"]: "created" for d in desired}

    monkeypatch.setattr(svc.ws, "reconcile", fake_reconcile)
    payload = _payload(package_id=1, sha256=sha, driver_name="Brother HL",
                       inf_relpath="x64/BRPRF.INF", size=len(blob))
    report = svc.provision(object(), payload, client=_FakeClient(blob),
                           cache_dir=str(tmp_path))

    assert report.ok
    spec = seen["desired"][0]
    assert spec["driver"] == "Brother HL"
    assert spec["inf"].endswith("BRPRF.INF")
    assert os.path.isfile(spec["inf"]), "the INF handed to pnputil must exist"
    assert "package" not in spec, "the raw payload must not leak into the spec"


def test_an_unusable_package_becomes_a_stated_skip_not_a_crash(tmp_path, monkeypatch):
    """The operator needs to know the driver is the problem, not the printer."""
    monkeypatch.setattr(svc.ws, "reconcile", lambda runner, desired, managed_prefix="": {})
    payload = _payload(package_id=1, sha256="0" * 64, driver_name="X",
                       inf_relpath="x64/BRPRF.INF", size=1)
    report = svc.provision(object(), payload,
                           client=_FakeClient(_zip_bytes([("a", b"b")])),
                           cache_dir=str(tmp_path))
    assert not report.outcomes
    reason = list(report.skipped.values())[0]
    assert "driver package unusable" in reason and "checksum" in reason


# --------------------------------- the UI ----------------------------------- #


def test_upload_stores_the_file_and_audits_the_digest(db, tmp_path):
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _admin(db)
    blob = _zip_bytes([("x64/BRPRF.INF", b"; inf")])

    r = _login().post("/manage/machines/drivers/upload", data={
        "client_id": c.id, "name": "Brother", "driver_name": "Brother HL",
        "inf_relpath": "x64/BRPRF.INF", "model": "HL-L2350DW",
    }, files={"package": ("driver.zip", blob, "application/zip")},
        follow_redirects=False)
    assert r.status_code in (302, 303, 307)

    row = db.scalar(select(m.DriverPackage))
    assert row.sha256 == hashlib.sha256(blob).hexdigest()
    assert os.path.isfile(row.stored_at)

    audit = db.scalar(select(m.AuditLog).where(
        m.AuditLog.action == "driver_package.upload"))
    assert row.sha256 in (audit.detail or ""), "the digest identifies what will run"


def test_a_too_short_model_tag_is_rejected_at_upload(db, tmp_path):
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _admin(db)

    _login().post("/manage/machines/drivers/upload", data={
        "client_id": c.id, "driver_name": "X", "inf_relpath": "a.inf", "model": "HL",
    }, files={"package": ("d.zip", _zip_bytes([("a", b"b")]), "application/zip")},
        follow_redirects=False)
    assert db.scalar(select(m.DriverPackage)) is None


def test_a_readonly_user_cannot_upload_a_driver(db, tmp_path):
    os.environ["PN_DRIVER_DIR"] = str(tmp_path)
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    db.add(m.User(username="ro", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.client_readonly))
    db.commit()

    r = _login("ro").post("/manage/machines/drivers/upload", data={
        "client_id": c.id, "driver_name": "X", "inf_relpath": "a.inf",
    }, files={"package": ("d.zip", _zip_bytes([("a", b"b")]), "application/zip")},
        follow_redirects=False)
    assert "/login" in r.headers.get("location", "")
    assert db.scalar(select(m.DriverPackage)) is None


def test_deleting_a_package_removes_the_bytes_too(db, tmp_path):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _admin(db)
    pkg, _ = _package(db, c, tmp_path)
    stored = pkg.stored_at
    assert os.path.isfile(stored)

    _login().post(f"/manage/machines/drivers/{pkg.id}/delete", follow_redirects=False)
    assert db.scalar(select(m.DriverPackage)) is None
    assert not os.path.exists(stored), "the binary must not outlive its row"
