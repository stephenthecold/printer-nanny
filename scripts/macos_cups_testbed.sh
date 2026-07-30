#!/usr/bin/env bash
# A throwaway CUPS scheduler for verifying the macOS backend without a Mac.
#
# WHY THIS EXISTS
# ---------------
# Everything below platforms/macos._run is unit-tested with a fake, and a fake
# returns what it is told -- so it cannot notice that `lpoptions -d` with no
# destination is a usage error, that `lpstat -p` output is translated, or that
# cupsd commits device-uri before it runs the IPP query. All three shipped as
# defects and all three were found by pointing scripts/macos_provision_check.py
# at a real scheduler on Linux. This is that scheduler.
#
# CUPS is CUPS: macOS ships the same daemon and the same client tools, so the
# command shapes, the exit codes, the translated prose and the per-user
# ~/.cups/lpoptions precedence all behave identically here. What this does NOT
# reproduce is a Mac: no launchd, no /dev/console owner, no dscl, no real
# printer. Those stay manual, per deploy/MACOS-CLIENT-TESTING.md.
#
#   sudo scripts/macos_cups_testbed.sh start     # private cupsd + a test account
#   sudo PYTHONPATH=agent python3 scripts/macos_provision_check.py --as-user pntest
#   sudo scripts/macos_cups_testbed.sh stop      # and it takes everything with it
#
# It listens on /run/cups/cups.sock -- the default socket -- deliberately: `sudo`
# scrubs the environment, so a CUPS_SERVER pointing somewhere else would not
# reach the per-user `lpoptions` call and the default-printer check would test
# nothing. That is also the real macOS arrangement, where the scheduler is local.
set -euo pipefail

ROOT="${PN_CUPS_ROOT:-/tmp/pn-cups-testbed}"
TEST_USER="${PN_TEST_USER:-pntest}"

require_root() {
  [ "$(id -u)" = "0" ] || { echo "needs root (cupsd, lpadmin, useradd)" >&2; exit 2; }
}

have_cupsd() {
  command -v cupsd >/dev/null 2>&1 || [ -x /usr/sbin/cupsd ]
}

# The vendor-driver checks hinge on cupsd's own `cups-missing-filter-warning`
# verdict, which is only meaningful when the BASELINE filter set is complete.
# With cups-core-drivers absent, cupsd cannot find `commandtops` and flags the
# warning on every queue -- so a good PPD and a PPD naming a nonexistent vendor
# filter look identical, and the check silently proves nothing while still
# passing. That is the failure mode this whole file exists to avoid, so it is
# checked rather than assumed.
warn_if_filters_incomplete() {
  local missing=""
  for f in commandtops pstops rastertopwg; do
    [ -x "/usr/lib/cups/filter/$f" ] || missing="$missing $f"
  done
  if [ -n "$missing" ]; then
    echo
    echo "WARNING: the CUPS filter baseline is incomplete (missing:$missing)."
    echo "  cupsd will then flag cups-missing-filter-warning on EVERY queue, so the"
    echo "  vendor-driver checks cannot tell a good PPD from a broken one -- they"
    echo "  will pass without proving anything. Install them first:"
    echo "    apt-get install -y cups-core-drivers cups-filters"
    echo
  fi
}

start() {
  require_root
  if ! have_cupsd; then
    echo "cupsd not installed. On Debian/Ubuntu: apt-get install -y cups-daemon" >&2
    exit 2
  fi

  mkdir -p "$ROOT"/{spool,run,tmp,log,ppd} /run/cups
  chmod 755 /run/cups

  # A private cups-files.conf rather than the system one. Two things here are
  # load-bearing: cupsd refuses to run as root ("Will not use User root"), so it
  # needs an unprivileged User; and ServerBin must point at the system helpers or
  # cups-driverd cannot be executed and every model lookup fails.
  cat > "$ROOT/cups-files.conf" <<EOF
ServerRoot $ROOT
ServerBin /usr/lib/cups
DataDir /usr/share/cups
DocumentRoot /usr/share/cups/doc-root
RequestRoot $ROOT/spool
StateDir $ROOT/run
CacheDir $ROOT/tmp
TempDir $ROOT/tmp
AccessLog $ROOT/log/access_log
ErrorLog $ROOT/log/error_log
PageLog $ROOT/log/page_log
User lp
Group lp
SystemGroup root lpadmin
EOF

  # No TCP listener and no web interface: this accepts unauthenticated admin
  # requests so the check can create queues, and that must not be reachable off
  # the box. A unix socket is the whole exposure.
  cat > "$ROOT/cupsd.conf" <<EOF
LogLevel warn
Listen /run/cups/cups.sock
WebInterface No
DefaultAuthType None
Browsing Off
<Location />
  Order allow,deny
  Allow all
</Location>
<Location /admin>
  AuthType None
  Order allow,deny
  Allow all
</Location>
EOF

  stop_quiet
  /usr/sbin/cupsd -c "$ROOT/cupsd.conf" -s "$ROOT/cups-files.conf"

  # cupsd forks; give it a moment and then prove it answered rather than assume.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if LC_ALL=C lpstat -r 2>/dev/null | grep -q running; then break; fi
    sleep 0.5
  done
  LC_ALL=C lpstat -r

  # The default-printer checks need a *second* account, because reading root's
  # default and calling it the console user's is the exact failure the read-back
  # exists to catch -- one account cannot demonstrate that.
  if ! id "$TEST_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$TEST_USER"
    echo "created test account $TEST_USER"
  fi
  chmod 666 /run/cups/cups.sock

  warn_if_filters_incomplete
  echo
  echo "scheduler up at /run/cups/cups.sock (state in $ROOT)"
  echo "now:  sudo PYTHONPATH=agent python3 scripts/macos_provision_check.py --as-user $TEST_USER"
}

stop_quiet() {
  pkill -f "cupsd -c $ROOT/cupsd.conf" 2>/dev/null || true
  sleep 1
}

stop() {
  require_root
  stop_quiet
  rm -rf "$ROOT"
  rm -f /run/cups/cups.sock "/home/$TEST_USER/.cups/lpoptions" /etc/cups/lpoptions
  echo "scheduler stopped, $ROOT removed"
  echo "note: the $TEST_USER account was left in place; userdel -r $TEST_USER to remove it"
}

status() {
  if LC_ALL=C lpstat -r 2>/dev/null | grep -q running; then
    LC_ALL=C lpstat -r
    LC_ALL=C lpstat -v 2>/dev/null || echo "(no queues)"
  else
    echo "no scheduler running"
    exit 1
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
