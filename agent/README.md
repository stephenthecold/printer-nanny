# printer-nanny-agent (Milestone 2 — placeholder)

The site agent is the next milestone. It will be a standalone, pip-installable
Python package that runs at each site (systemd service or Docker container) and:

1. **Discovers** printers by SNMP-sweeping its assigned subnet(s), GETting
   `sysDescr` across the host range and keeping anything that answers the Printer
   MIB → `POST /api/v1/agents/{id}/discovered`.
2. **Polls** approved printers on a schedule over SNMP (`pysnmp`), reading the
   OIDs documented in [`../central/snmp.md`](../central/snmp.md), normalizing
   supplies via the shared [`central/snmp_parse.py`](../central/snmp_parse.py)
   helpers → `POST /api/v1/agents/{id}/readings`.
3. **Heartbeats** and **pulls commands** (`rescan`, `poll_now`, `update_config`)
   from `GET /api/v1/agents/{id}/commands` — the hybrid model: no inbound ports
   at the site, central can still trigger work on demand.

## Config (planned: `/etc/printer-nanny/agent.toml`)
```toml
central_url = "https://printers.msp.example.com"
api_key     = "pn_xxxxxxxx"          # issued by POST /api/v1/agents
poll_interval_seconds = 300
heartbeat_interval_seconds = 60

[[subnets]]
cidr = "10.10.0.0/24"
snmp_version = "2c"
snmp_community = "public"
```

The agent reuses the central server's parsing/contract code, so discovery and
polling produce exactly the payloads the ingest API already accepts (and tests).
