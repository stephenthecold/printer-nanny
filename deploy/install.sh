#!/usr/bin/env bash
# Printer Nanny — Docker Compose installer + updater.
#
# Pulled from the network (the advertised one-liner):
#   curl -fsSL https://raw.githubusercontent.com/stephenthecold/printer-nanny/main/deploy/install.sh | bash
#
# Or from a checkout:
#   bash deploy/install.sh                     # interactive first-run setup
#   bash deploy/install.sh --update            # pull + rebuild + restart (no prompts)
#   bash deploy/install.sh --proxy bundled --hostname printers.example.com --acme-email ops@example.com
#   bash deploy/install.sh --proxy external    # API exposed on :8000 (default)
#   bash deploy/install.sh --proxy none --http-port 8536   # plain HTTP, LAN only
#
# On a fresh install (no .env file) and an interactive shell, the installer
# walks you through TLS / hostname choices. Pipe input or pass --proxy on the
# command line to skip the prompts. Re-running is safe — your .env and data
# are preserved unless you pass --demo (destructive reseed) or --reset-caddy.
set -euo pipefail

REPO_URL="${PRINTER_NANNY_REPO:-https://github.com/stephenthecold/printer-nanny.git}"
BRANCH="${PRINTER_NANNY_BRANCH:-main}"
INSTALL_DIR=""
DEMO=0
UPDATE=0
WITH_CADDY=0
RESET_CADDY=0
PROXY=""                 # bundled | external | none
HOSTNAME_ARG=""
ACME_EMAIL=""
HTTP_PORT=""
PORT=""                  # /healthz poll target
BUILD_FLAG="--build"
COMPOSE_PROFILES=""

while [ $# -gt 0 ]; do
  case "$1" in
    --update)       UPDATE=1; shift ;;
    --demo)         DEMO=1; shift ;;
    --with-caddy)   WITH_CADDY=1; PROXY="bundled"; shift ;;
    --proxy)        PROXY="$2"; shift 2 ;;
    --hostname)     HOSTNAME_ARG="$2"; shift 2 ;;
    --acme-email)   ACME_EMAIL="$2"; shift 2 ;;
    --http-port)    HTTP_PORT="$2"; shift 2 ;;
    --reset-caddy)  RESET_CADDY=1; shift ;;
    --dir)          INSTALL_DIR="$2"; shift 2 ;;
    --branch)       BRANCH="$2"; shift 2 ;;
    --repo-url)     REPO_URL="$2"; shift 2 ;;
    --port)         PORT="$2"; shift 2 ;;
    --no-build)     BUILD_FLAG=""; shift ;;
    -h|--help)
      sed -n '2,18{/^#/{s/^# \{0,1\}//;p;}}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die()  { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found in PATH"; }
in_repo() { [ -f docker-compose.yml ] && [ -d central ] && [ -f deploy/Dockerfile ]; }

need docker
docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 plugin missing (install 'docker-compose-plugin')."

# --- Locate or fetch the checkout ------------------------------------------ #
if [ -z "$INSTALL_DIR" ] && in_repo; then
  echo "==> using current checkout: $(pwd)"
else
  TARGET="${INSTALL_DIR:-printer-nanny}"
  if [ -d "$TARGET/.git" ]; then
    echo "==> updating existing checkout at $TARGET"
    need git
    git -C "$TARGET" fetch --quiet origin "$BRANCH"
    git -C "$TARGET" checkout --quiet "$BRANCH"
    git -C "$TARGET" pull --quiet --ff-only
  else
    [ "$UPDATE" -eq 1 ] && die "--update needs an existing checkout; got nothing at $TARGET"
    need git
    echo "==> cloning $REPO_URL (branch $BRANCH) → $TARGET"
    git clone --quiet --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET"
  fi
  cd "$TARGET"
fi

# --- Update mode: pull, rebuild, restart, exit ----------------------------- #
if [ "$UPDATE" -eq 1 ]; then
  in_repo || die "not a printer-nanny checkout: $(pwd)"
  [ -f .env ] || die ".env missing — this looks like a first-run, not an update."
  if [ -d .git ]; then
    echo "==> pulling latest from origin/$BRANCH"
    git fetch --quiet origin "$BRANCH"
    BEFORE=$(git rev-parse --short HEAD)
    git checkout --quiet "$BRANCH"
    git pull --quiet --ff-only origin "$BRANCH"
    AFTER=$(git rev-parse --short HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
      echo "    already at latest ($AFTER) — nothing to pull."
    else
      echo "    updated $BEFORE → $AFTER"
      git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
    fi
  fi
  # Honor an existing Caddy profile selection so we don't accidentally drop it.
  # Older .envs (pre-CADDY_PROFILE) used --with-caddy at install time; detect
  # that by the presence of a running printer-nanny-caddy-* container.
  if grep -q '^CADDY_PROFILE=1' .env 2>/dev/null \
     || docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^printer-nanny[-_]caddy'; then
    COMPOSE_PROFILES="--profile caddy"
  fi
  echo "==> docker compose build --pull"
  docker compose $COMPOSE_PROFILES build --pull
  echo "==> docker compose up -d"
  docker compose $COMPOSE_PROFILES up -d
  echo "==> done. Migrations + idempotent admin bootstrap ran on container start."
  echo "    Logs: docker compose logs -f api worker"
  exit 0
fi

# --- First-run: interactive TLS / hostname setup --------------------------- #
ask() { local prompt="$1" default="$2" var; read -r -p "$prompt [$default] " var </dev/tty || var=""; echo "${var:-$default}"; }

if [ ! -f .env ]; then
  echo "==> first-run setup"

  # Decide on the proxy strategy. Interactive if not supplied + we have a tty.
  if [ -z "$PROXY" ] && [ -t 0 ]; then
    echo
    echo "How do you want to terminate TLS?"
    echo "  1) external  — you already run Caddy / Nginx / Traefik (default)"
    echo "  2) bundled   — use the bundled Caddy + Let's Encrypt (needs a public hostname)"
    echo "  3) none      — plain HTTP on a port (LAN testing only)"
    CHOICE=$(ask "Choice (1/2/3)" "1")
    case "$CHOICE" in
      1|external) PROXY="external" ;;
      2|bundled)  PROXY="bundled"  ;;
      3|none)     PROXY="none"     ;;
      *) die "unrecognised choice: $CHOICE" ;;
    esac
  fi
  PROXY="${PROXY:-external}"

  case "$PROXY" in
    bundled)
      [ -z "$HOSTNAME_ARG" ] && [ -t 0 ] && \
        HOSTNAME_ARG=$(ask "Public hostname (DNS A-record must point here)" "")
      [ -n "$HOSTNAME_ARG" ] || die "--hostname is required for --proxy bundled"
      [ -z "$ACME_EMAIL" ] && [ -t 0 ] && \
        ACME_EMAIL=$(ask "ACME / Let's Encrypt contact email (recommended)" "")
      WITH_CADDY=1
      COMPOSE_PROFILES="--profile caddy"
      : "${PORT:=443}"
      ;;
    none)
      [ -z "$HTTP_PORT" ] && [ -t 0 ] && \
        HTTP_PORT=$(ask "Host port for plain HTTP" "8080")
      HTTP_PORT="${HTTP_PORT:-8080}"
      WITH_CADDY=1
      COMPOSE_PROFILES="--profile caddy"
      : "${PORT:=$HTTP_PORT}"
      ;;
    external|"")
      PROXY="external"
      WITH_CADDY=0
      : "${PORT:=8000}"
      ;;
    *) die "unrecognised --proxy value: $PROXY" ;;
  esac

  echo "==> generating .env with a fresh SECRET_KEY"
  if command -v openssl >/dev/null 2>&1; then
    SECRET=$(openssl rand -base64 48 | tr -d '\n')
  else
    SECRET=$(head -c 48 /dev/urandom | base64 | tr -d '\n')
  fi
  umask 077
  {
    echo "# Generated by deploy/install.sh — do not commit. Rotate by deleting and re-running."
    echo "SECRET_KEY=$SECRET"
    if [ "$PROXY" = "bundled" ]; then
      echo "# Bundled Caddy with Let's Encrypt TLS — selected during first-run install."
      echo "CADDY_PROFILE=1"
      echo "CADDY_HTTP_PORT=80"
      echo "CADDY_HTTPS_PORT=443"
      echo "PN_HOSTNAME=$HOSTNAME_ARG"
      [ -n "$ACME_EMAIL" ] && echo "PN_ACME_EMAIL=$ACME_EMAIL"
    elif [ "$PROXY" = "none" ]; then
      echo "# Bundled Caddy on plain HTTP — selected during first-run install."
      echo "CADDY_PROFILE=1"
      echo "CADDY_HTTP_PORT=$HTTP_PORT"
      echo "CADDY_HTTPS_PORT=$((HTTP_PORT + 1))"  # unused but compose needs *something*
    else
      echo "# External reverse proxy mode — API exposed on the host for your own proxy."
      echo "API_PORT=8000"
    fi
  } > .env
else
  echo "==> .env already present; leaving it alone"
  # Re-derive WITH_CADDY / PORT from what's in .env so a plain re-run still works.
  if grep -q '^CADDY_PROFILE=1' .env; then
    WITH_CADDY=1
    COMPOSE_PROFILES="--profile caddy"
    if grep -q '^PN_HOSTNAME=' .env; then
      : "${PORT:=443}"
    else
      HTTP_PORT_FROM_ENV=$(grep '^CADDY_HTTP_PORT=' .env | cut -d= -f2)
      : "${PORT:=${HTTP_PORT_FROM_ENV:-8080}}"
    fi
  else
    : "${PORT:=8000}"
  fi
fi

# --- Caddyfile generation (only when bundled) ------------------------------ #
if [ "$WITH_CADDY" -eq 1 ]; then
  if [ ! -f deploy/Caddyfile ] || [ "$RESET_CADDY" -eq 1 ]; then
    SITE_HOST="$(grep '^PN_HOSTNAME=' .env 2>/dev/null | cut -d= -f2- || true)"
    SITE_EMAIL="$(grep '^PN_ACME_EMAIL=' .env 2>/dev/null | cut -d= -f2- || true)"
    if [ -n "$SITE_HOST" ]; then
      SITE_LINE="$SITE_HOST"
      if [ -n "$SITE_EMAIL" ]; then
        GLOBAL="    email $SITE_EMAIL"
      else
        GLOBAL="    # email not set — Caddy will use ZeroSSL fallback"
      fi
    else
      HTTP_PORT_VAL="$(grep '^CADDY_HTTP_PORT=' .env 2>/dev/null | cut -d= -f2- || echo 8080)"
      SITE_LINE=":$HTTP_PORT_VAL"
      GLOBAL="    auto_https off"
    fi
    sed -e "s|__SITE__|$SITE_LINE|" \
        -e "s|__GLOBAL_OPTIONS__|$GLOBAL|" \
        deploy/Caddyfile.template > deploy/Caddyfile
    echo "==> wrote deploy/Caddyfile (site: $SITE_LINE)"
  else
    echo "==> deploy/Caddyfile already present; leaving it alone (--reset-caddy to regenerate)"
  fi
fi

echo "==> docker compose ${COMPOSE_PROFILES} up -d $BUILD_FLAG"
docker compose $COMPOSE_PROFILES up -d $BUILD_FLAG

# --- Wait for /healthz ----------------------------------------------------- #
echo "==> waiting for the API on http://localhost:${PORT}/healthz"
DEADLINE=$(( $(date +%s) + 180 ))
SCHEME="http"
[ "$PORT" = "443" ] && SCHEME="https"
CURL_ARGS="-fsS"
[ "$SCHEME" = "https" ] && CURL_ARGS="$CURL_ARGS -k"
until curl $CURL_ARGS "${SCHEME}://localhost:${PORT}/healthz" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo
    echo "API didn't respond within 3 minutes. Recent logs:"
    docker compose logs --tail=40 api || true
    die "startup timed out"
  fi
  sleep 2
done

if [ "$DEMO" -eq 1 ]; then
  echo
  echo "!! --demo will DROP all tables and reseed with fake clients/printers."
  if [ -t 0 ]; then
    read -r -p "   Type 'yes' to continue: " ans
    [ "$ans" = "yes" ] || die "aborted"
  fi
  echo "==> seeding demo data"
  docker compose exec -T api python -m central.seed
fi

# --- Closing banner -------------------------------------------------------- #
case "$PROXY" in
  bundled)
    URL="https://$(grep '^PN_HOSTNAME=' .env | cut -d= -f2)"
    ENTRY="$URL  (bundled Caddy + Let's Encrypt — first request triggers cert issuance)"
    ;;
  none)
    ENTRY="http://localhost:${PORT}  (bundled Caddy, no TLS — LAN testing only)"
    ;;
  *)
    ENTRY="http://localhost:${PORT}  (API directly — point your reverse proxy here)"
    ;;
esac

cat <<EOF

  Printer Nanny is up: ${ENTRY}
  Login: admin / admin   ← change this password immediately
                           (Settings → Users, or /manage)

  Logs:    docker compose logs -f api worker
  Stop:    docker compose down
  Update:  bash deploy/install.sh --update
  Reset Caddyfile: bash deploy/install.sh --reset-caddy

EOF
