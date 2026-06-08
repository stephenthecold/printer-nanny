"""Public installer endpoints - served unauthenticated so curl/iwr can fetch them."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from central.main import app


def test_install_agent_sh_served():
    cli = TestClient(app)
    r = cli.get("/install-agent.sh")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-shellscript")
    assert "#!/usr/bin/env bash" in r.text
    assert "Printer Nanny" in r.text
    # Auto-installs python3-venv via the available package manager rather than
    # bailing with a "you need to apt install ..." hint. Operators should never
    # need to know the package name.
    assert "apt-get" in r.text
    assert "dnf" in r.text
    assert "yum" in r.text
    assert "python3-venv" in r.text


def test_install_agent_ps1_served():
    cli = TestClient(app)
    r = cli.get("/install-agent.ps1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # PowerShell parser key markers + the things operators rely on.
    assert "[CmdletBinding()]" in r.text
    assert "PrinterNannyAgent" in r.text  # service name
    assert "nssm" in r.text.lower()  # NSSM is what wraps the service
    # The PS1 must mention the required Python version so the error is searchable.
    assert "Python 3.10" in r.text
    # Must probe Python via PEP 514 registry, not just PATH - winget --silent
    # installs leave PATH unrefreshed in the current shell, and an operator with
    # Python correctly installed should NOT see "Python not found".
    assert "HKLM:\\SOFTWARE\\Python\\PythonCore" in r.text
    # PN_PYTHON_EXE escape hatch for non-standard install locations.
    assert "PN_PYTHON_EXE" in r.text
    # Auto-installs Python via winget when missing rather than throwing - the
    # whole point of the install one-liner is the operator doesn't need to do
    # a prerequisite step first.
    assert "winget install Python.Python.3.12" in r.text
    # Same for git - pip needs it for git+https:// installs and Server 2022
    # doesn't ship it. Failing silently and leaving the operator to install
    # git themselves is what this whole "auto-install deps" effort is fixing.
    assert "winget install Git.Git" in r.text
    # NSSM mirror: agents pull NSSM from the central server, not from nssm.cc
    # directly (the architecture promise is outbound-to-central only, and
    # nssm.cc returns 503 in real deployments).
    assert "install-agent-nssm.exe" in r.text
    # Pip install must bail loudly on $LASTEXITCODE -- silent pip failures
    # left operators with a half-installed agent and no useful error.
    assert "pip install $PipSource failed" in r.text


def test_install_nssm_serves_cached_binary(tmp_path, monkeypatch):
    """When the cache directory has the binary, the endpoint serves it byte-
    for-byte. No network access during this test."""
    monkeypatch.setenv("PN_CACHE_DIR", str(tmp_path))
    expected = b"fake nssm.exe payload " + (b"x" * 200_000)  # > 50KB size sanity-check
    (tmp_path / "nssm-2.24-x64.exe").write_bytes(expected)

    cli = TestClient(app)
    r = cli.get("/install-agent-nssm.exe?arch=x64")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.content == expected


def test_install_nssm_populates_cache_from_upstream_zip(tmp_path, monkeypatch):
    """First request downloads upstream zip, extracts win64/nssm.exe + win32/nssm.exe
    into the cache, then serves. Subsequent requests are pure disk reads."""
    # Build a fake nssm zip in the same layout as nssm.cc's release.
    x64_payload = b"NSSM-X64-BINARY" + b"\x00" * 70_000
    x86_payload = b"NSSM-X86-BINARY" + b"\x00" * 60_000
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("nssm-2.24/win64/nssm.exe", x64_payload)
        zf.writestr("nssm-2.24/win32/nssm.exe", x86_payload)
    zip_bytes = zip_buf.getvalue()

    # Serve the fake zip via a local httpx mock by stubbing the Client used by
    # the installer module. Simplest: monkeypatch httpx.Client in the module.
    from central.dashboard import installer

    class _StubResp:
        content = zip_bytes
        def raise_for_status(self): pass

    class _StubClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return _StubResp()

    monkeypatch.setenv("PN_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PN_NSSM_URL", "https://example.invalid/nssm-2.24.zip")
    monkeypatch.setattr(installer, "httpx", type("M", (), {"Client": _StubClient}))

    cli = TestClient(app)
    r = cli.get("/install-agent-nssm.exe?arch=x64")
    assert r.status_code == 200
    assert r.content == x64_payload
    # Cache should now be populated; serve x86 from disk (no Client touched).
    r2 = cli.get("/install-agent-nssm.exe?arch=x86")
    assert r2.status_code == 200
    assert r2.content == x86_payload


def test_install_nssm_returns_503_when_upstream_fails(tmp_path, monkeypatch):
    """When the cache is empty AND upstream is unreachable, the agent gets a
    503 with operator instructions, not a 500 stack trace."""
    from central.dashboard import installer

    class _BoomClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): raise RuntimeError("simulated upstream outage")

    monkeypatch.setenv("PN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(installer, "httpx", type("M", (), {"Client": _BoomClient}))
    cli = TestClient(app)
    r = cli.get("/install-agent-nssm.exe")
    assert r.status_code == 503
    assert "NSSM mirror unavailable" in r.text
    assert "simulated upstream outage" in r.text


def test_install_scripts_are_ascii_only():
    """PowerShell 5.1 reads .ps1 files as Windows-1252 by default when there's
    no BOM, so a UTF-8 em-dash decodes as garbage and breaks the parser at every
    comment line that uses it (real failure reported on Server 2022). Strip
    non-ASCII from both installers so encoding can never break parsing again,
    regardless of how the file is transferred, saved, or re-encoded."""
    cli = TestClient(app)
    for path in ("/install-agent.sh", "/install-agent.ps1"):
        r = cli.get(path)
        assert r.status_code == 200, path
        bad = sorted({c for c in r.text if ord(c) > 127})
        assert not bad, (
            f"{path} contains non-ASCII chars that will break PowerShell 5.1: {bad!r}"
        )
