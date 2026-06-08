"""Public installer endpoints — served unauthenticated so curl/iwr can fetch them."""

from __future__ import annotations

from fastapi.testclient import TestClient

from central.main import app


def test_install_agent_sh_served():
    cli = TestClient(app)
    r = cli.get("/install-agent.sh")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-shellscript")
    assert "#!/usr/bin/env bash" in r.text
    assert "Printer Nanny" in r.text


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
    # Must probe Python via PEP 514 registry, not just PATH — winget --silent
    # installs leave PATH unrefreshed in the current shell, and an operator with
    # Python correctly installed should NOT see "Python not found".
    assert "HKLM:\\SOFTWARE\\Python\\PythonCore" in r.text
    # PN_PYTHON_EXE escape hatch for non-standard install locations.
    assert "PN_PYTHON_EXE" in r.text
