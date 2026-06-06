"""Serve the agent install script so `curl https://central/install-agent.sh` works.

Public (no auth) like any get.example.com bootstrap — the secret is the per-agent
API key passed as an argument by the operator, never embedded in the script.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["installer"])

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "install-agent.sh"


@router.get("/install-agent.sh", response_class=PlainTextResponse)
def install_script() -> PlainTextResponse:
    try:
        body = _SCRIPT_PATH.read_text()
    except OSError:
        return PlainTextResponse("# install-agent.sh not found on server\n", status_code=500)
    return PlainTextResponse(body, media_type="text/x-shellscript")
