"""Server-side agent enrollment CLI — mints an agent (and its key) without the
dashboard, for setup scripts / automation.

    python -m central.enroll --client "Acme" --site "HQ" --agent "HQ agent" \
        --subnet 10.0.3.0/24 --community public --json

Creates the client/site if they don't exist, creates the agent, assigns the
subnet, and prints the agent id + API key (shown once — stored hashed). Run it
where the central code + DB are reachable, e.g.:
    docker compose exec -T api python -m central.enroll ... --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from sqlalchemy import select

from central.db import SessionLocal, create_all
from central import models as m
from central.config import settings
from central.security import generate_api_key, hash_api_key


def _get_or_create_client(db, name: str) -> m.Client:
    client = db.scalar(select(m.Client).where(m.Client.name == name))
    if client is None:
        client = m.Client(name=name)
        db.add(client)
        db.flush()
    return client


def _get_or_create_site(db, client: m.Client, name: str) -> m.Site:
    site = db.scalar(
        select(m.Site).where(m.Site.client_id == client.id, m.Site.name == name)
    )
    if site is None:
        site = m.Site(client_id=client.id, name=name)
        db.add(site)
        db.flush()
    return site


def enroll(
    *, client_name: str, site_name: str, agent_name: str,
    subnet: Optional[str] = None, community: str = "public", version: str = "2c",
) -> dict:
    if settings.is_sqlite:
        create_all()  # convenience for local SQLite; Postgres uses migrations
    db = SessionLocal()
    try:
        client = _get_or_create_client(db, client_name)
        site = _get_or_create_site(db, client, site_name)
        api_key = generate_api_key()
        agent = m.Agent(site_id=site.id, name=agent_name, api_key_hash=hash_api_key(api_key))
        db.add(agent)
        db.flush()
        if subnet:
            exists = db.scalar(
                select(m.Subnet).where(m.Subnet.site_id == site.id, m.Subnet.cidr == subnet)
            )
            if exists is None:
                db.add(m.Subnet(
                    site_id=site.id, agent_id=agent.id, cidr=subnet,
                    snmp_community=community, snmp_version=version,
                ))
            else:
                exists.agent_id = agent.id
                exists.snmp_community = community
                exists.snmp_version = version
        db.commit()
        return {
            "agent_id": agent.id, "api_key": api_key,
            "client_id": client.id, "site_id": site.id,
            "client": client.name, "site": site.name, "subnet": subnet,
        }
    finally:
        db.close()


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m central.enroll", description="Enroll a site agent")
    p.add_argument("--client", default="Default Client")
    p.add_argument("--site", default="Default Site")
    p.add_argument("--agent", default="Site agent")
    p.add_argument("--subnet", help="CIDR to assign to the agent, e.g. 10.0.3.0/24")
    p.add_argument("--community", default="public")
    p.add_argument("--snmp-version", default="2c", dest="version")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    args = p.parse_args(argv)

    result = enroll(
        client_name=args.client, site_name=args.site, agent_name=args.agent,
        subnet=args.subnet, community=args.community, version=args.version,
    )
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Enrolled agent #{result['agent_id']} '{args.agent}' "
              f"at {result['client']} / {result['site']}")
        if args.subnet:
            print(f"  subnet: {args.subnet}  community: {args.community}")
        print(f"  API key (shown once): {result['api_key']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
