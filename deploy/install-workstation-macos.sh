#!/usr/bin/env bash
# Printer Nanny -- macOS workstation client installer.
#
# Installs the workstation client into a venv and runs it as a LaunchDaemon, so
# assigned printers appear for whoever is at the console. Designed to be piped
# from the central server with the client-scoped enrollment key baked in:
#
#   curl -fsSL https://CENTRAL/install-workstation-macos.sh | sudo bash -s -- \
#     --server https://CENTRAL --enroll-key pnw_xxxxx
#
# Re-running upgrades in place and keeps the machine's identity, which is what
# makes an upgrade not look like a new machine to central.
#
# WHAT THIS IS NOT
# ----------------
# It is not a signed, notarized .pkg. An MSP pushing this through an MDM wants
# one; building it needs an Apple Developer ID and a notarization round trip, so
# it is deliberately out of scope here rather than half-built. This script is the
# supported path today, and it is what a .pkg postinstall would end up running.
#
# WHY A LAUNCHDAEMON AND NOT A LAUNCHAGENT
# ----------------------------------------
# lpadmin needs root, queues are machine-wide, the machine's credential must not
# be readable by the user whose printers it manages, and the client sets the
# CONSOLE user's default -- which means it has to be able to become whoever is
# there now, not be pinned to one session. See deploy/com.printernanny.workstation.plist.
set -euo pipefail

SERVER=""
ENROLL_KEY=""
INTERVAL=""
PREFIX=""
VERIFY_TLS="true"
PIP_SOURCE="git+https://github.com/your-org/printer-nanny.git#subdirectory=agent"
INSTALL_DIR="/usr/local/printer-nanny-workstation"
STATE_DIR="/Library/Application Support/PrinterNanny"
LOG_DIR="/Library/Logs/PrinterNanny"
PLIST="/Library/LaunchDaemons/com.printernanny.workstation.plist"
LABEL="com.printernanny.workstation"
BIN_LINK="/usr/local/bin/printer-nanny-workstation"
PYTHON_BIN=""
UNINSTALL="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --server)      SERVER="$2"; shift 2 ;;
    --enroll-key)  ENROLL_KEY="$2"; shift 2 ;;
    --interval)    INTERVAL="$2"; shift 2 ;;
    --prefix)      PREFIX="$2"; shift 2 ;;
    --verify-tls)  VERIFY_TLS="$2"; shift 2 ;;
    --pip-source)  PIP_SOURCE="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --python)      PYTHON_BIN="$2"; shift 2 ;;
    --uninstall)   UNINSTALL="true"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root (use sudo)"
[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS; use install-agent.sh on Linux"

# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
if [ "$UNINSTALL" = "true" ]; then
  say "stopping $LABEL"
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$BIN_LINK"
  rm -rf "$INSTALL_DIR"
  # The state directory is left in place ON PURPOSE: it holds the machine's GUID
  # and credential, so removing it turns a reinstall into a brand-new machine and
  # strands this Mac's printer assignments on a record nobody will look at again.
  # Adoption-by-name can recover it, but only if the name still matches.
  echo
  echo "Removed the client. Queues it created were NOT removed -- run"
  echo "  lpstat -p    and    sudo lpadmin -x <queue>"
  echo "for any you no longer want."
  echo
  echo "The machine's identity is still in:"
  echo "  $STATE_DIR"
  echo "Leave it to keep this Mac's printer assignments on a reinstall; delete it"
  echo "to make the next install enroll as a new machine."
  exit 0
fi

[ -n "$SERVER" ] || die "--server is required"
[ -n "$ENROLL_KEY" ] || die "--enroll-key is required (mint one on the Machines page)"
case "$PIP_SOURCE" in
  *your-org*) die "pip source still points at the 'your-org' placeholder. Pass --pip-source with your real repo." ;;
esac

# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #
# The system python3 at /usr/bin/python3 is a stub that can trigger the Command
# Line Tools install prompt -- fine interactively, fatal in an MDM push. Prefer a
# real interpreter and say so clearly if there is none.
if [ -z "$PYTHON_BIN" ]; then
  for candidate in \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /usr/bin/python3
  do
    if [ -x "$candidate" ]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || die "no python3 found; pass --python /path/to/python3"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "$PYTHON_BIN is older than 3.9"
say "using $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

command -v lpadmin >/dev/null 2>&1 || command -v /usr/sbin/lpadmin >/dev/null 2>&1 \
  || die "lpadmin not found; this does not look like a working macOS install"

# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
say "installing into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/python" -m pip install --quiet --upgrade "$PIP_SOURCE"

ENTRY="$INSTALL_DIR/venv/bin/printer-nanny-workstation"
[ -x "$ENTRY" ] || die "the package installed but $ENTRY is missing"
ln -sf "$ENTRY" "$BIN_LINK"

mkdir -p "$STATE_DIR" "$LOG_DIR"
# 0700: the machine's credential lives here and the users whose printers this
# manages must not be able to read it.
chmod 700 "$STATE_DIR"
chown root:wheel "$STATE_DIR"

# --------------------------------------------------------------------------- #
# Config -- the enrollment key goes HERE and nowhere else
# --------------------------------------------------------------------------- #
# Never in the plist: ProgramArguments and EnvironmentVariables are readable by
# any local user via `launchctl print`, and /Library/LaunchDaemons is
# world-readable by launchd's own requirement. Written 0600 before the daemon is
# started so there is no window where it is loose.
CONFIG="$STATE_DIR/workstation.toml"
say "writing $CONFIG (0600)"
umask 077
{
  echo "# Written by install-workstation-macos.sh. Holds a credential: keep 0600."
  echo "server = \"$SERVER\""
  echo "enroll_key = \"$ENROLL_KEY\""
  [ -n "$INTERVAL" ] && echo "interval = $INTERVAL"
  [ -n "$PREFIX" ] && echo "queue_prefix = \"$PREFIX\""
  if [ "$VERIFY_TLS" = "false" ]; then echo "verify_tls = false"; fi
} > "$CONFIG"
umask 022
chmod 600 "$CONFIG"
chown root:wheel "$CONFIG"

# --------------------------------------------------------------------------- #
# LaunchDaemon
# --------------------------------------------------------------------------- #
say "installing $PLIST"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN_LINK</string>
        <string>--config</string>
        <string>$CONFIG</string>
    </array>
    <key>UserName</key>
    <string>root</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/workstation.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/workstation.log</string>
</dict>
</plist>
PLISTEOF
# launchd refuses a plist that is group- or world-writable.
chmod 644 "$PLIST"
chown root:wheel "$PLIST"

say "starting $LABEL"
launchctl bootout "system/$LABEL" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
launchctl enable "system/$LABEL"

# Report what launchd actually thinks, rather than assuming the bootstrap worked.
sleep 2
if launchctl print "system/$LABEL" >/dev/null 2>&1; then
  say "loaded"
else
  die "launchd did not load $LABEL -- see $LOG_DIR/workstation.log"
fi

cat <<EOF

Installed. The client enrolls on its first poll and then converges queues every
${INTERVAL:-300}s.

  log        : $LOG_DIR/workstation.log
  identity   : $STATE_DIR/machine.json
  queues     : lpstat -p
  run by hand: sudo $BIN_LINK --config "$CONFIG" --once -v
  stop       : sudo launchctl bootout system/$LABEL
  uninstall  : sudo bash $0 --uninstall

Two things worth knowing before you file a bug:

  * A printer whose driver_tier is driver_required is SKIPPED on macOS with a
    stated reason -- vendor driver staging is Windows-only today. Driverless
    (IPP Everywhere) printers are the supported case.
  * The user's default printer is only set while somebody is at the console.
    At the login window it is reported as "nobody is signed in", which is normal.
EOF
