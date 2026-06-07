# Site agent image. Build/push to your registry, then run with the enrollment
# values as env vars (PN_CENTRAL_URL / PN_AGENT_ID / PN_API_KEY):
#
#   docker build -f deploy/agent.Dockerfile -t ghcr.io/stephenthecold/printer-nanny-agent .
#   docker run -d --restart=always --name printer-nanny-agent \
#     -e PN_CENTRAL_URL=https://central -e PN_AGENT_ID=12 -e PN_API_KEY=pn_xxx \
#     ghcr.io/stephenthecold/printer-nanny-agent
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Install just the self-contained agent package (agent/ subdir).
COPY agent /app/agent
RUN pip install --no-cache-dir /app/agent

# SNMP discovery needs host-network reachability to printer subnets — run with
# --network host (or route the subnets to the container) at `docker run` time.
ENTRYPOINT ["printer-nanny-agent"]
CMD ["run"]
