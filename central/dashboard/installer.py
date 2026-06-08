"""Serve the agent install scripts so `curl https://central/install-agent.sh` and
`iwr https://central/install-agent.ps1 | iex` work.

Public (no auth) like any get.example.com bootstrap — the secret is the per-agent
API key passed as an argument by the operator, never embedded in the script.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["installer"])

_DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy"
_SCRIPT_PATH = _DEPLOY_DIR / "install-agent.sh"
_PS1_PATH = _DEPLOY_DIR / "install-agent.ps1"


@router.get("/install-agent.sh", response_class=PlainTextResponse)
def install_script() -> PlainTextResponse:
    try:
        body = _SCRIPT_PATH.read_text()
    except OSError:
        return PlainTextResponse("# install-agent.sh not found on server\n", status_code=500)
    return PlainTextResponse(body, media_type="text/x-shellscript")


@router.get("/install-agent.ps1", response_class=PlainTextResponse)
def install_script_ps1() -> PlainTextResponse:
    """Windows installer — `iwr -useb https://CENTRAL/install-agent.ps1 | iex`."""
    try:
        body = _PS1_PATH.read_text()
    except OSError:
        return PlainTextResponse("# install-agent.ps1 not found on server\n", status_code=500)
    # text/plain so PowerShell `Invoke-WebRequest` content-decodes cleanly; the
    # content-type doesn't drive `iex` parsing, only `Get-Content` does.
    return PlainTextResponse(body, media_type="text/plain")
