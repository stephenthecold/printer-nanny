#!/usr/bin/env bash
# Regenerate the vendored frontend assets in central/static/.
#
# WHY THESE ARE VENDORED, NOT LOADED FROM A CDN
# ---------------------------------------------
# Printer Nanny is self-hosted on MSP infrastructure, and a good number of those
# installs sit on segmented management VLANs with no outbound internet. Loading
# Tailwind and htmx from cdn.tailwindcss.com / unpkg.com meant those installs
# rendered an unstyled dashboard where every interactive control was dead --
# htmx drives the whole UI. Vendoring makes the dashboard a function of the
# image alone. It also drops the in-browser Tailwind compiler (which upstream
# documents as development-only) in favour of a ~21KB tree-shaken stylesheet.
#
# WHEN TO RUN THIS
# ----------------
# After changing any Tailwind class in central/dashboard/templates/. The CSS is
# tree-shaken against those templates, so a class that no template used at build
# time is NOT in the stylesheet and will silently do nothing in the browser.
# tests/test_static_assets.py fails when a template uses a class the vendored
# CSS lacks, so a forgotten run is caught by the suite rather than by an
# operator noticing a broken layout.
#
# Node is a BUILD-TIME dependency only. It is deliberately absent from
# deploy/Dockerfile and from the runtime path -- this script is run by a
# developer, and its output is committed.
set -euo pipefail

# Tailwind is pinned to v3. v4 renames core utilities this codebase uses
# (`shadow` -> `shadow-sm`, `rounded` -> `rounded-sm`) and shifts the default
# palette, so an unpinned upgrade would silently restyle every page.
TAILWIND_VERSION="3.4.19"
# Matches the version previously loaded from unpkg, so vendoring changed no
# behaviour. The npm package is the same artifact unpkg served.
HTMX_VERSION="1.9.12"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC_DIR="$REPO_ROOT/central/static"
TEMPLATE_DIR="$REPO_ROOT/central/dashboard/templates"

command -v npx >/dev/null 2>&1 || {
  echo "error: npx not found. Node is needed to regenerate assets (build-time only)." >&2
  exit 1
}

# Build in a scratch dir so node_modules never lands in the repo -- it would
# otherwise be swept into the Docker build context by deploy/Dockerfile's
# `COPY . .` and bloat the image with build-only dependencies.
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

cat > "$BUILD_DIR/tailwind.config.js" <<EOF
module.exports = {
  content: ['$TEMPLATE_DIR/**/*.html'],
  theme: { extend: {} },
  plugins: [],
};
EOF

printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' \
  > "$BUILD_DIR/input.css"

mkdir -p "$STATIC_DIR"

echo "==> building tailwind ${TAILWIND_VERSION} css from ${TEMPLATE_DIR}"
npx --yes "tailwindcss@${TAILWIND_VERSION}" \
  -c "$BUILD_DIR/tailwind.config.js" \
  -i "$BUILD_DIR/input.css" \
  -o "$STATIC_DIR/tailwind.css" \
  --minify

echo "==> vendoring htmx ${HTMX_VERSION}"
(cd "$BUILD_DIR" && npm install --silent --no-audit --no-fund "htmx.org@${HTMX_VERSION}")
cp "$BUILD_DIR/node_modules/htmx.org/dist/htmx.min.js" "$STATIC_DIR/htmx.min.js"

echo
echo "==> done. Vendored assets:"
ls -la "$STATIC_DIR"
echo
echo "sha256:"
sha256sum "$STATIC_DIR/tailwind.css" "$STATIC_DIR/htmx.min.js"
echo
echo "Commit the result, then run: pytest tests/test_static_assets.py"
