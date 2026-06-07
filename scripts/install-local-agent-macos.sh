#!/usr/bin/env bash
# Install a PERSISTENT local agent on macOS via launchd.
#
# On Docker Desktop the containers run in a Linux VM and cannot reach your LAN,
# so when the central stack runs in Docker the local agent must run on the host.
# This enrolls an agent (key auto-generated), writes a LaunchAgent, and loads it
# so it keeps polling across logins/reboots.
#
#   scripts/install-local-agent-macos.sh                 # auto-detect /24
#   scripts/install-local-agent-macos.sh --subnet 10.0.3.0/24 --community public
#   scripts/install-local-agent-macos.sh --uninstall
set -euo pipefail

CENTRAL_URL="http://localhost:8080"
SUBNET=""; COMMUNITY="public"; CLIENT="Local"; SITE="Main Office"
AGENT="$(hostname -s 2>/dev/null || echo mac) agent"
LABEL="com.printernanny.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --central-url) CENTRAL_URL="$2"; shift 2 ;;
    --subnet) SUBNET="$2"; shift 2 ;;
    --community) COMMUNITY="$2"; shift 2 ;;
    --client) CLIENT="$2"; shift 2 ;;
    --site) SITE="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
REPO="$(pwd)"

if [ "$UNINSTALL" = "1" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled ${LABEL}."
  exit 0
fi

AGENT_BIN="$REPO/.venv/bin/printer-nanny-agent"
[ -x "$AGENT_BIN" ] || { echo "error: $AGENT_BIN not found — run: pip install -e \".[agent]\"" >&2; exit 1; }
PY="$REPO/.venv/bin/python"

if [ -z "$SUBNET" ]; then
  IP=$("$PY" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('1.1.1.1',80));print(s.getsockname()[0]);s.close()")
  SUBNET="${IP%.*}.0/24"
  echo "==> auto-detected subnet: $SUBNET"
fi

echo "==> enrolling agent (key auto-generated)"
OUT=$(docker compose exec -T api python -m central.enroll --json \
  --client "$CLIENT" --site "$SITE" --agent "$AGENT" --subnet "$SUBNET" --community "$COMMUNITY")
AID=$(printf '%s' "$OUT" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['agent_id'])")
KEY=$(printf '%s' "$OUT" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['api_key'])")
echo "    agent #$AID for $SUBNET"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>${AGENT_BIN}</string><string>run</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PN_CENTRAL_URL</key><string>${CENTRAL_URL}</string>
    <key>PN_AGENT_ID</key><string>${AID}</string>
    <key>PN_API_KEY</key><string>${KEY}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/printer-nanny-agent.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/printer-nanny-agent.err.log</string>
</dict></plist>
EOF
chmod 600 "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
echo "==> loaded ${LABEL}. It now polls $SUBNET continuously."
echo "    logs: tail -f /tmp/printer-nanny-agent.out.log"
echo "    approve newly discovered printers at ${CENTRAL_URL}/approvals"
echo "    uninstall: scripts/install-local-agent-macos.sh --uninstall"
