#!/usr/bin/env bash
# Turnkey local agent setup — for running an agent on the SAME machine as the
# central stack (great for a first trial against your own LAN). It auto-generates
# the agent key (no copy-paste), assigns the detected subnet, and runs one cycle.
#
#   scripts/setup-local-agent.sh                       # auto-detect /24, defaults
#   scripts/setup-local-agent.sh --subnet 10.0.3.0/24 --community public --run
#
# For remote site boxes use the one-line installer instead (Agents → enroll).
set -euo pipefail

CENTRAL_URL="http://localhost:8080"
SUBNET=""
COMMUNITY="public"
CLIENT="Local"
SITE="Main Office"
AGENT="$(hostname -s 2>/dev/null || echo local) agent"
MODE="once"   # 'once' = single cycle then exit; 'run' = stay running

while [ $# -gt 0 ]; do
  case "$1" in
    --central-url) CENTRAL_URL="$2"; shift 2 ;;
    --subnet)      SUBNET="$2"; shift 2 ;;
    --community)   COMMUNITY="$2"; shift 2 ;;
    --client)      CLIENT="$2"; shift 2 ;;
    --site)        SITE="$2"; shift 2 ;;
    --agent)       AGENT="$2"; shift 2 ;;
    --run)         MODE="run"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."   # repo root
PY="python3"
[ -x .venv/bin/python ] && PY=".venv/bin/python"
AGENT_BIN="$PY -m printer_nanny_agent"
[ -x .venv/bin/printer-nanny-agent ] && AGENT_BIN=".venv/bin/printer-nanny-agent"

# Detect the primary /24 if not provided.
if [ -z "$SUBNET" ]; then
  IP=$("$PY" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('1.1.1.1',80));print(s.getsockname()[0]);s.close()")
  SUBNET="${IP%.*}.0/24"
  echo "==> auto-detected subnet: $SUBNET (override with --subnet)"
fi

echo "==> enrolling agent (key auto-generated server-side)"
ENROLL_ARGS="--json --client \"$CLIENT\" --site \"$SITE\" --agent \"$AGENT\" --subnet $SUBNET --community $COMMUNITY"
if docker compose ps api 2>/dev/null | grep -q . ; then
  OUT=$(eval docker compose exec -T api python -m central.enroll $ENROLL_ARGS)
else
  OUT=$(eval "$PY" -m central.enroll $ENROLL_ARGS)
fi

AID=$(printf '%s' "$OUT" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['agent_id'])")
KEY=$(printf '%s' "$OUT" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['api_key'])")
echo "    agent #$AID enrolled for subnet $SUBNET"

export PN_CENTRAL_URL="$CENTRAL_URL" PN_AGENT_ID="$AID" PN_API_KEY="$KEY"
echo "==> running agent ($MODE) against $CENTRAL_URL"
if [ "$MODE" = "run" ]; then
  exec $AGENT_BIN run
else
  $AGENT_BIN run --once
  echo
  echo "Done. Newly discovered printers are PENDING — approve them at"
  echo "  ${CENTRAL_URL}/approvals"
  echo "then re-run with --run to poll continuously (or install the systemd service)."
fi
