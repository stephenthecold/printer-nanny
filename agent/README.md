# printer-nanny-agent

The site agent: a Python package that runs at each site (systemd service or
container) and collects printer telemetry over SNMP, pushing it to the central
server. No inbound ports are needed at the site — the agent dials out over HTTPS
and pulls any queued commands on its heartbeat.

## What it does
1. **Heartbeat** — `POST /api/v1/agents/{id}/heartbeat` every `heartbeat_interval`.
2. **Poll** — fetches the approved-printer list (`GET .../targets`), polls each
   over SNMP (the OIDs in [`../central/snmp.md`](../central/snmp.md)), and pushes
   `POST .../readings`. Supplies are normalized with the shared
   [`central/snmp_parse.py`](../central/snmp_parse.py) (sentinel-safe).
3. **Discover** — SNMP-sweeps each configured subnet, reports anything that
   answers the Printer MIB via `POST .../discovered` (lands as *pending* for a
   tech to approve). Multi-subnet sites just list several `[[subnets]]`.
4. **Commands** — pulls `GET .../commands` and acts on `rescan` / `poll_now` /
   `update_config`.

## Architecture
The SNMP layer sits behind the `SnmpBackend` interface ([snmp.py](printer_nanny_agent/snmp.py)),
so the poller and discovery are fully unit-testable with a fake backend and never
import pysnmp. `PysnmpBackend` is the real implementation (pysnmp 7, asyncio).

```
printer_nanny_agent/
  snmp.py       SnmpBackend interface + PysnmpBackend
  oids.py       Printer-MIB / Host-Resources OID constants
  poller.py     poll one printer -> central reading payload (pure builders)
  discovery.py  concurrent subnet sweep -> discovered devices
  client.py     async httpx client for the central ingest API
  config.py     TOML config loader (agent.example.toml)
  runner.py     orchestration: run_once / run_forever (backend injectable)
  cli.py        `printer-nanny-agent` entry point
```

## Install — one-liner (recommended)
Enroll the agent in the central UI (**Agents → enroll**) and it shows a ready
copy-paste command with the key baked in. No file to edit:

```bash
# Linux (systemd) — installs a venv + service:
curl -fsSL https://CENTRAL/install-agent.sh | sudo bash -s -- \
  --central-url https://CENTRAL --agent-id 12 --api-key pn_xxxxx

# or Docker (build & push the image first — see deploy/agent.Dockerfile;
# there is no prebuilt public image yet):
docker run -d --restart=always --network host --name printer-nanny-agent \
  -e PN_CENTRAL_URL=https://CENTRAL -e PN_AGENT_ID=12 -e PN_API_KEY=pn_xxxxx \
  ghcr.io/your-org/printer-nanny-agent
```

The installer ([`../deploy/install-agent.sh`](../deploy/install-agent.sh), served
at `GET /install-agent.sh`) writes a minimal config and a systemd unit. Subnets,
SNMP, and intervals are managed in the central UI and fetched at runtime — the
local file/env holds only the central URL + key. Lost the key? **Rotate key** on
the agent in the UI for a fresh command.

## Install — manual / dev
```bash
# Standalone (e.g. on a site box, from your published repo):
pip install "git+https://github.com/your-org/printer-nanny.git#subdirectory=agent"
# or from the monorepo for dev: pip install -e ".[agent]"

# Config via env vars (no file), flags, or a TOML file — precedence: flags > env > file.
PN_CENTRAL_URL=https://CENTRAL PN_AGENT_ID=12 PN_API_KEY=pn_xxx printer-nanny-agent run
printer-nanny-agent --central-url https://CENTRAL --agent-id 12 --api-key pn_xxx selftest
printer-nanny-agent --config /etc/printer-nanny/agent.toml poll 10.10.0.20   # one printer -> JSON
printer-nanny-agent --config ... discover                                    # sweep -> JSON
printer-nanny-agent --config ... run --once                                  # single cycle
```

## End-to-end demo (no real printers)
With the central server running and seeded, [`../scripts/e2e_agent_demo.py`](../scripts/e2e_agent_demo.py)
drives a full agent cycle through a fake SNMP backend:
```bash
PYTHONPATH=. python scripts/e2e_agent_demo.py http://localhost:8000
```

## Self-contained
The package has no dependency on the central server — supply parsing is vendored
as [`snmp_parse.py`](printer_nanny_agent/snmp_parse.py) so it installs with just
`httpx` + `pysnmp`. A parity test (`tests/test_snmp_parse_parity.py`) keeps it in
lockstep with the server's copy.
