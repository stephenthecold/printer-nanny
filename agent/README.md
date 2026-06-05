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

## Install & run
```bash
pip install -e ".[agent]"           # from the repo root
cp agent/printer_nanny_agent/agent.example.toml /etc/printer-nanny/agent.toml
# fill in central_url, agent_id, api_key (from POST /api/v1/agents), and subnets

printer-nanny-agent --config /etc/printer-nanny/agent.toml selftest   # check central
printer-nanny-agent --config ... poll 10.10.0.20                      # one printer -> JSON
printer-nanny-agent --config ... discover                            # sweep subnets -> JSON
printer-nanny-agent --config ... run                                 # main loop
printer-nanny-agent --config ... run --once                          # single cycle
```
A sample systemd unit is in [`../deploy/printer-nanny-agent.service`](../deploy/printer-nanny-agent.service).

## End-to-end demo (no real printers)
With the central server running and seeded, [`../scripts/e2e_agent_demo.py`](../scripts/e2e_agent_demo.py)
drives a full agent cycle through a fake SNMP backend:
```bash
PYTHONPATH=. python scripts/e2e_agent_demo.py http://localhost:8000
```

## Note on shared code
The agent currently imports `central.snmp_parse` for supply normalization (single
source of truth, no drift). For a slim standalone deploy that logic can later be
extracted into a small shared package both sides depend on.
