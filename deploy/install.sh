#!/usr/bin/env bash
# Printer Nanny — Docker Compose installer + updater.
#
# Pulled from the network (the advertised one-liner):
#   curl -fsSL https://raw.githubusercontent.com/stephenthecold/printer-nanny/main/deploy/install.sh | bash
#
# Or from a checkout:
#   bash deploy/install.sh                     # interactive first-run setup
#   bash deploy/install.sh --update            # pull + rebuild + restart (no prompts)
#   bash deploy/install.sh --update --pull-only  # update the checkout, don't restart
#   bash deploy/install.sh --migrate-compose   # hand-edited compose -> override file
#   bash deploy/install.sh --proxy bundled --hostname printers.example.com --acme-email ops@example.com
#   bash deploy/install.sh --proxy external    # API exposed on :8000 (default)
#   bash deploy/install.sh --proxy none --http-port 8536   # plain HTTP, LAN only
#
# On a fresh install (no .env file) and an interactive shell, the installer
# walks you through TLS / hostname choices. Pipe input or pass --proxy on the
# command line to skip the prompts. Re-running is safe — your .env and data
# are preserved unless you pass --demo (destructive reseed) or --reset-caddy.
#
# Customise through .env (credentials, ports, image tags, worker interval) and
# docker-compose.override.yml -- never by editing docker-compose.yml, which
# --update fast-forwards. --update stashes and re-applies local edits so a
# hand-edited compose file cannot wedge the updater, but it will eventually
# conflict; --migrate-compose converts one into an override that never does.
set -euo pipefail

REPO_URL="${PRINTER_NANNY_REPO:-https://github.com/stephenthecold/printer-nanny.git}"
BRANCH="${PRINTER_NANNY_BRANCH:-main}"
INSTALL_DIR=""
DEMO=0
UPDATE=0
PULL_ONLY=0
MIGRATE_COMPOSE=0
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
    --pull-only)    PULL_ONLY=1; UPDATE=1; shift ;;
    --migrate-compose) MIGRATE_COMPOSE=1; shift ;;
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
      sed -n '2,25{/^#/{s/^# \{0,1\}//;p;}}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die()  { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found in PATH"; }
in_repo() { [ -f docker-compose.yml ] && [ -d central ] && [ -f deploy/Dockerfile ]; }

# True when docker-compose.yml carries local edits (staged or not) -- the thing
# that used to abort `--update` with "would be overwritten by merge".
compose_dirty() {
  [ -d .git ] || return 1
  ! git diff --quiet HEAD -- docker-compose.yml 2>/dev/null
}

# A URL-safe secret: the Postgres password is embedded in DATABASE_URL, so
# base64's +/= would have to be percent-encoded to survive the round trip.
# Alphanumeric-only sidesteps that entirely.
rand_urlsafe() {
  local want="${1:-24}" raw
  if command -v openssl >/dev/null 2>&1; then
    raw=$(openssl rand -base64 48)
  else
    raw=$(head -c 48 /dev/urandom | base64)
  fi
  printf '%s' "$raw" | tr -d '\n' | tr -dc 'A-Za-z0-9' | cut -c1-"$want"
}

# Compose's default project name is the directory name, lowercased and stripped
# of anything but [a-z0-9_-]. Needed to find OUR pgdata volume rather than some
# other project's.
compose_project() {
  printf '%s' "${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
}

# Does an initialised Postgres data volume already exist for this project?
# Decides whether a generated password is safe: the postgres image only reads
# POSTGRES_PASSWORD when it initialises an EMPTY data directory, so inventing
# one for an existing volume would lock api/worker out of their own database.
# Returns 2 ("can't tell") when the daemon won't answer -- callers must treat
# that as "assume it exists", never as "no".
pgdata_volume_exists() {
  local proj
  proj="$(compose_project)"
  docker volume ls -q >/dev/null 2>&1 || return 2
  docker volume ls -q \
      --filter "label=com.docker.compose.project=$proj" \
      --filter "label=com.docker.compose.volume=pgdata" 2>/dev/null | grep -q . && return 0
  docker volume ls -q 2>/dev/null | grep -qx "${proj}_pgdata" && return 0
  return 1
}

# Take a hand-edited docker-compose.yml out of the update's way: back it up, hand
# the edits back as a starter override, restore the tracked file. Deliberately
# does NOT try to synthesise a working override -- a unified diff is not a
# compose fragment (a changed line means nothing without the service key it sits
# under), and a plausible-looking auto-translation that silently drops a setting
# is worse than an honest one the operator finishes by hand.
migrate_compose() {
  in_repo || die "not a printer-nanny checkout: $(pwd)"
  [ -d .git ] || die "--migrate-compose needs a git checkout; this directory has no .git"
  need git

  if ! compose_dirty; then
    echo "==> docker-compose.yml already matches upstream — nothing to migrate."
    if [ -f docker-compose.override.yml ]; then
      echo "    docker-compose.override.yml is in place; Compose merges it automatically."
    else
      echo "    To customise: cp docker-compose.override.yml.example docker-compose.override.yml"
    fi
    return 0
  fi

  echo "==> docker-compose.yml has local edits:"
  git --no-pager diff --stat HEAD -- docker-compose.yml | sed 's/^/    /'
  echo
  echo "    They will be preserved in a backup and handed back as an override."
  if [ -t 0 ]; then
    read -r -p "    Continue? [y/N] " ans </dev/tty || ans=""
    case "$ans" in y|Y|yes|YES) ;; *) die "aborted; nothing was changed" ;; esac
  fi

  STAMP=$(date -u +%Y%m%d-%H%M%S)
  BAK="docker-compose.yml.bak.$STAMP"
  cp docker-compose.yml "$BAK"

  OUT="docker-compose.override.yml"
  [ -e "$OUT" ] && OUT="docker-compose.override.yml.migrated-$STAMP"

  {
    echo "# Generated by 'deploy/install.sh --migrate-compose' at ${STAMP}Z."
    echo "#"
    echo "# Your previous docker-compose.yml is saved in full at:"
    echo "#     $BAK"
    echo "#"
    echo "# Its differences from upstream are reproduced at the bottom of this file as"
    echo "# a COMMENTED-OUT diff. They are comments because a diff cannot simply be"
    echo "# pasted in: an override states whole keys under the service they belong to."
    echo "# Restate each change you still want, then delete the 'services: {}' line."
    echo "#"
    echo "# Check .env.example FIRST. Postgres credentials, the API and mailhog ports,"
    echo "# image tags and the worker interval are plain .env variables now, so most"
    echo "# edits people used to make here need no override at all."
    echo "#"
    echo "# Syntax and merge rules: docker-compose.override.yml.example"
    echo "# Verify the result with:  docker compose config"
    echo ""
    echo "services: {}"
    echo ""
    echo "# --- your edits, as a diff against upstream -------------------------------"
    git --no-pager diff HEAD -- docker-compose.yml | sed 's/^/# /'
  } > "$OUT"

  git checkout --quiet HEAD -- docker-compose.yml

  echo "==> backed up  $BAK"
  echo "==> wrote      $OUT"
  echo "==> restored   docker-compose.yml to the tracked version"
  echo
  echo "    Next: port the edits you still want into $OUT, then"
  echo "          docker compose config      # confirm the merged result"
  echo "          bash deploy/install.sh --update"
  if [ "$OUT" != "docker-compose.override.yml" ]; then
    echo
    echo "    NOTE: docker-compose.override.yml already existed and was left alone;"
    echo "          your migrated edits went to $OUT, which Compose does NOT read."
    echo "          Merge them into docker-compose.override.yml yourself."
  fi
}

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

# --- Explicit compose migration -------------------------------------------- #
if [ "$MIGRATE_COMPOSE" -eq 1 ]; then
  migrate_compose
  [ "$UPDATE" -eq 1 ] || exit 0
fi

# --- Update mode: pull, rebuild, restart, exit ----------------------------- #
if [ "$UPDATE" -eq 1 ]; then
  in_repo || die "not a printer-nanny checkout: $(pwd)"
  [ -f .env ] || die ".env missing — this looks like a first-run, not an update."
  if [ -d .git ]; then
    need git
    echo "==> pulling latest from origin/$BRANCH"
    git fetch --quiet origin "$BRANCH"

    # Local edits to tracked files make git refuse the pull outright ("Your
    # local changes to the following files would be overwritten by merge"),
    # which used to leave an operator who had touched docker-compose.yml with no
    # way to update at all. Park the edits, fast-forward, put them back.
    STASHED=0
    if ! git diff --quiet HEAD -- 2>/dev/null; then
      echo "    local changes to tracked files — stashing them for the pull:"
      git diff --name-only HEAD -- | sed 's/^/      /'
      git stash push --quiet --message "printer-nanny installer auto-stash $(date -u +%Y%m%dT%H%M%SZ)" \
        || die "could not stash local changes; sort the tree out by hand and re-run"
      STASHED=1
    fi

    # Restore the tree to exactly how we found it, then fail. Used on every
    # abort below so a failed update is never a half-applied one.
    restore_and_die() {
      git reset --quiet --hard "${BEFORE:-HEAD}" 2>/dev/null || true
      [ "$STASHED" -eq 1 ] && git stash pop --quiet 2>/dev/null || true
      die "$1"
    }

    git checkout --quiet "$BRANCH" 2>/dev/null || {
      [ "$STASHED" -eq 1 ] && git stash pop --quiet 2>/dev/null || true
      die "could not switch to branch $BRANCH"
    }
    BEFORE=$(git rev-parse --short HEAD)

    git pull --quiet --ff-only origin "$BRANCH" \
      || restore_and_die "pull is not a fast-forward (local commits on $BRANCH?) — resolve with: git -C $(pwd) status"
    AFTER=$(git rev-parse --short HEAD)

    if [ "$STASHED" -eq 1 ]; then
      if git stash pop --quiet 2>/dev/null; then
        echo "    re-applied your local changes on top of $AFTER"
      else
        # A failed pop leaves conflict markers in the tree AND keeps the stash
        # entry. Neither is safe to hand to `docker compose build`, so unwind
        # the whole update: reset to the pre-pull commit (the pull was a
        # fast-forward, so no local commit can be lost), then pop cleanly onto
        # the base the edits were written against.
        CONFLICTS=$(git diff --name-only --diff-filter=U 2>/dev/null || true)
        git reset --quiet --hard "$BEFORE" 2>/dev/null || true
        git stash pop --quiet 2>/dev/null || true
        echo >&2
        echo "  Your local edits conflict with the upstream update." >&2
        echo "  Nothing was changed — still at $BEFORE with your edits in place." >&2
        echo >&2
        [ -n "$CONFLICTS" ] && { echo "  Conflicting:" >&2; echo "$CONFLICTS" | sed 's/^/    /' >&2; echo >&2; }
        if printf '%s' "$CONFLICTS" | grep -qx 'docker-compose.yml'; then
          echo "  docker-compose.yml is meant to be upstream's. Move your changes into" >&2
          echo "  .env / docker-compose.override.yml, which updates never touch:" >&2
          echo "    bash deploy/install.sh --migrate-compose" >&2
        else
          echo "  Reconcile them by hand (git stash / git diff), then re-run." >&2
        fi
        echo "  Then: bash deploy/install.sh --update" >&2
        die "update aborted — no changes made"
      fi
    fi

    if [ "$BEFORE" = "$AFTER" ]; then
      echo "    already at latest ($AFTER) — nothing to pull."
    else
      echo "    updated $BEFORE → $AFTER"
      git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
    fi

    if compose_dirty; then
      echo
      echo "    NOTE: docker-compose.yml still has local edits. They survived this"
      echo "          update, but they will conflict the next time upstream touches"
      echo "          the same lines. Move them somewhere updates never touch:"
      echo "            bash deploy/install.sh --migrate-compose"
    fi
  fi

  if [ "$PULL_ONLY" -eq 1 ]; then
    echo "==> --pull-only: checkout updated, containers left running as-is."
    echo "    Apply it when you're ready: bash deploy/install.sh --update"
    exit 0
  fi
  # Honor an existing Caddy profile selection so we don't accidentally drop it.
  # Older .envs (pre-CADDY_PROFILE) used --with-caddy at install time; detect
  # that by the presence of a running printer-nanny-caddy-* container.
  if grep -q '^CADDY_PROFILE=1' .env 2>/dev/null \
     || docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^printer-nanny[-_]caddy'; then
    COMPOSE_PROFILES="--profile caddy"
  fi
  # Mailhog used to be an unconditional service publishing :8025 on every
  # install. It is now opt-in, so an existing container is reported as an orphan
  # rather than silently carried forward -- say so instead of letting compose's
  # warning look like breakage.
  if grep -q '^MAILHOG_PROFILE=1' .env 2>/dev/null; then
    COMPOSE_PROFILES="$COMPOSE_PROFILES --profile mailhog"
  elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^printer-nanny[-_]mailhog'; then
    echo "==> note: mailhog is now opt-in (it accepts unauthenticated mail and"
    echo "    serves an unauthenticated UI of it, so it no longer runs by default)."
    echo "    Keep it:   add MAILHOG_PROFILE=1 to .env and re-run --update"
    echo "    Remove it: docker compose rm -sf mailhog"
    echo "    Until then compose will list it as an orphan container."
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

  # Postgres password. A brand-new data volume gets a generated one -- shipping
  # nanny/nanny to production is not a default worth keeping. An ALREADY
  # INITIALISED volume must keep the password it was built with, because the
  # image ignores POSTGRES_PASSWORD on a non-empty data directory and the stack
  # would simply be locked out. That case is reachable: deleting .env and
  # re-running is documented as the way to rotate SECRET_KEY.
  # `|| PGVOL=$?` keeps the non-zero return out of set -e's reach.
  PGVOL=0; pgdata_volume_exists || PGVOL=$?
  case "$PGVOL" in
    0) PG_PASSWORD="nanny"
       echo "    existing Postgres volume found — keeping its current credentials."
       echo "    (rotate later via ALTER USER; see .env.example)" ;;
    1) PG_PASSWORD=$(rand_urlsafe 24)
       echo "    fresh Postgres volume — generating a random database password." ;;
    *) PG_PASSWORD="nanny"
       echo "    warning: docker daemon unreachable, so an existing Postgres volume"
       echo "    can't be ruled out. Keeping the legacy password rather than risk"
       echo "    locking the stack out of its own data. Change it later per .env.example." ;;
  esac

  umask 077
  {
    echo "# Generated by deploy/install.sh — do not commit. Rotate by deleting and re-running."
    echo "SECRET_KEY=$SECRET"
    echo "# Only applied when the pgdata volume is FIRST created; see .env.example"
    echo "# before changing this on a running install."
    echo "POSTGRES_USER=nanny"
    echo "POSTGRES_PASSWORD=$PG_PASSWORD"
    echo "POSTGRES_DB=printer_nanny"
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
