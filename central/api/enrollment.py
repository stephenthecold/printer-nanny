"""Self-registration: an agent trades a claim code for its own credentials.

This is the only endpoint in the agent API that is not authenticated by an
agent API key -- by construction, since the caller does not have one yet. Its
whole job is therefore to be narrow: one input, one effect, no way for the
caller to influence which tenant it lands in.

What the claim code buys the operator is that the credential which travels to
site is not the credential that stays there. The code dies on first use and at
its TTL; the long-lived API key is minted at the destination and returned
exactly once, over the same HTTPS the agent already uses for everything else.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from central import schemas as s
from central import services
from central.audit import record_anonymous
from central.db import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["enrollment"])


@router.post("/register", response_model=s.AgentRegistered)
def register_agent(
    body: s.AgentRegisterIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Redeem a claim code and return freshly minted agent credentials.

    Failure is deliberately undifferentiated: unknown, expired and
    already-redeemed all produce the same 401. Telling the caller which one it
    was would turn this into an oracle for probing whether a code ever existed.

    Both outcomes are audited. A success is a new agent appearing in a tenant,
    which is exactly the kind of event an operator should be able to find later;
    a failure is the only visible signal of someone trying codes, so it is
    recorded rather than silently 401'd. The code itself is never written to the
    audit row or the log -- it is a live credential right up until it is
    redeemed.
    """
    redeemed = services.redeem_claim_token(
        db, body.claim_code, hostname=body.hostname
    )
    if redeemed is None:
        record_anonymous(
            db, request, "", "agent.register_failed",
            detail="invalid, expired, or already-redeemed claim code",
        )
        db.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired claim code"
        )

    agent, api_key = redeemed
    if body.version:
        agent.version = str(body.version)[:80]
    record_anonymous(
        db, request, "", "agent.register",
        target=f"agent:{agent.id} site:{agent.site_id}",
        detail=f"self-registered via claim code as {agent.name!r}",
    )
    db.commit()
    log.info("agent %s self-registered into site %s", agent.id, agent.site_id)
    return s.AgentRegistered(
        agent_id=agent.id,
        api_key=api_key,
        site_id=agent.site_id,
        name=agent.name,
    )
