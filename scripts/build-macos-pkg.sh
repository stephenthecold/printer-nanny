#!/usr/bin/env bash
# Build, sign and notarize the macOS workstation installer. RUNS ON A MAC.
#
# WHY THIS IS NOT DONE IN THE CONTAINER
# =====================================
# The Windows MSI is built in-container with msitools, so it is fair to ask why
# this is not. Because it cannot be:
#
#   pkgbuild / productbuild   macOS-only.
#   productsign               needs a "Developer ID Installer" certificate in a
#                             keychain. There is no Linux equivalent.
#   xcrun notarytool          a closed Apple binary talking to an Apple service.
#   xcrun stapler             likewise.
#
# A .pkg is a XAR archive with a BOM, so an *unsigned* one can be hand-assembled
# on Linux with xar + bomutils. That was rejected: neither is packaged in current
# Ubuntu (both would have to be built from source into the runtime image, xar being
# unmaintained), and the output would still need a Mac to sign -- so it adds a
# fragile dependency and an Apple-tooling-didn't-make-this artifact to reach the
# same place. Since a Mac is required for signing regardless, pkgbuild on that Mac
# is correct by construction.
#
# So central's job is to mint the per-client key and assemble the payload; this
# script's job is everything that needs Apple's tools. Central hands you a bundle
# containing the payload plus a copy of this script.
#
# WHAT SIGNING AND NOTARIZATION ACTUALLY BUY
# ==========================================
# An MDM push (`InstallEnterpriseApplication`) installs an unsigned .pkg fine, so
# an unsigned build is genuinely usable and this script produces one by default.
# What signing + notarization buy is a package a **human can double-click**:
# unsigned, Gatekeeper refuses it outright, and the usual workaround is teaching
# people to right-click-Open, which is a habit you do not want to teach a customer.
#
#   ./build-macos-pkg.sh                       # unsigned, MDM-only
#   ./build-macos-pkg.sh --sign-only           # signed, not notarized
#   ./build-macos-pkg.sh --notarize            # signed + notarized + stapled
#
# Credentials come from the environment, never from arguments -- a process's
# command line is readable by every user on the machine, and one of these is an
# App Store Connect key:
#
#   PN_SIGN_IDENTITY   "Developer ID Installer: Acme Inc (AB12CD34EF)"
#   PN_TEAM_ID         AB12CD34EF
#   # then EITHER an App Store Connect API key (preferred, no 2FA prompts):
#   PN_ASC_KEY_ID / PN_ASC_ISSUER_ID / PN_ASC_KEY_PATH
#   # OR an Apple ID with an app-specific password:
#   PN_APPLE_ID / PN_APPLE_PASSWORD
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD="${PN_PAYLOAD:-$BUNDLE_DIR/payload}"
SCRIPTS="${PN_SCRIPTS:-$BUNDLE_DIR/scripts}"
OUT_DIR="${PN_OUT_DIR:-$BUNDLE_DIR/out}"

# Distinct from any agent package, for the same reason the MSI carries its own
# UpgradeCode: macOS treats a shared identifier as the same product, so an MSP's
# own server running both would have one replace the other.
IDENTIFIER="${PN_PKG_IDENTIFIER:-com.printernanny.workstation}"
VERSION="${PN_PKG_VERSION:-0.0.0}"
PKG_NAME="${PN_PKG_NAME:-PrinterNannyWorkstation}"

MODE="unsigned"
while [ $# -gt 0 ]; do
  case "$1" in
    --sign-only) MODE="signed"; shift ;;
    --notarize)  MODE="notarized"; shift ;;
    --identifier) IDENTIFIER="$2"; shift 2 ;;
    --version)    VERSION="$2"; shift 2 ;;
    --out-dir)    OUT_DIR="$2"; shift 2 ;;
    -h|--help)    sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[ "$(uname -s)" = "Darwin" ] || die "this must run on macOS: pkgbuild, productsign and notarytool have no Linux equivalent"
command -v pkgbuild >/dev/null || die "pkgbuild not found; install the Xcode command line tools (xcode-select --install)"
command -v productbuild >/dev/null || die "productbuild not found; install the Xcode command line tools"
[ -d "$PAYLOAD" ] || die "no payload at $PAYLOAD"
[ -x "$SCRIPTS/postinstall" ] || die "no executable postinstall at $SCRIPTS/postinstall"

mkdir -p "$OUT_DIR"
# The component package is an INTERMEDIATE and is kept out of $OUT_DIR, so that
# directory only ever holds something shippable. It is installable on its own but
# carries no Distribution -- so no volume-check and no product identity -- which
# means an operator who picks it by mistake installs onto an OS the real installer
# would have refused. Leaving two .pkg files side by side under one name is how
# that mistake gets made.
BUILD_DIR="$OUT_DIR/.build"
mkdir -p "$BUILD_DIR"
COMPONENT="$BUILD_DIR/${PKG_NAME}-component.pkg"
DISTRIBUTION="$OUT_DIR/${PKG_NAME}-${VERSION}.pkg"
SIGNED="$OUT_DIR/${PKG_NAME}-${VERSION}-signed.pkg"
FINAL="$DISTRIBUTION"

# --------------------------------------------------------------------------- #
# The payload holds a live credential
# --------------------------------------------------------------------------- #
# workstation.toml carries this client's enrollment key. pkgbuild preserves payload
# modes, so it must be 0600 going in -- and it is asserted rather than assumed,
# because a bundle that travelled through a tool which normalised modes (an
# unzip at the default umask, a git checkout) would silently produce an installer
# whose key is world-readable on every Mac it touches.
CONFIG="$PAYLOAD/Library/Application Support/PrinterNanny/workstation.toml"
if [ -f "$CONFIG" ]; then
  mode="$(stat -f '%Lp' "$CONFIG")"
  if [ "$mode" != "600" ]; then
    say "tightening $CONFIG from $mode to 600"
    chmod 600 "$CONFIG"
  fi
else
  die "the payload has no workstation.toml; the installer would enroll nothing"
fi

# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
say "pkgbuild ($IDENTIFIER $VERSION)"
rm -f "$COMPONENT" "$DISTRIBUTION" "$SIGNED"
# --ownership recommended: the payload is built on a machine where these files
# belong to whoever unpacked the bundle, and what must land on the target is
# root:wheel. Without this the installed tree carries the builder's uid.
pkgbuild \
  --root "$PAYLOAD" \
  --scripts "$SCRIPTS" \
  --identifier "$IDENTIFIER" \
  --version "$VERSION" \
  --ownership recommended \
  --install-location / \
  "$COMPONENT"

say "productbuild"
DIST_XML="$BUILD_DIR/distribution.xml"
cat > "$DIST_XML" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
    <title>Printer Nanny Workstation</title>
    <organization>com.printernanny</organization>
    <domains enable_anywhere="false" enable_currentUserHome="false" enable_localSystem="true"/>
    <options customize="never" require-scripts="true" hostArchitectures="arm64,x86_64"/>
    <!-- 10.12 is where lpadmin -m everywhere arrives; below that the client
         refuses driverless queues rather than fudging them, so the installer
         refuses the OS rather than shipping something inert. -->
    <volume-check>
        <allowed-os-versions><os-version min="10.12"/></allowed-os-versions>
    </volume-check>
    <choices-outline><line choice="default"><line choice="$IDENTIFIER"/></line></choices-outline>
    <choice id="default"/>
    <choice id="$IDENTIFIER" visible="false">
        <pkg-ref id="$IDENTIFIER"/>
    </choice>
    <pkg-ref id="$IDENTIFIER" version="$VERSION" onConclusion="none">$(basename "$COMPONENT")</pkg-ref>
</installer-gui-script>
XML
productbuild \
  --distribution "$DIST_XML" \
  --package-path "$BUILD_DIR" \
  "$DISTRIBUTION"

if [ "$MODE" = "unsigned" ]; then
  say "built UNSIGNED: $DISTRIBUTION"
  cat <<EOF

This package is NOT signed and NOT notarized.

  Usable now: an MDM push (InstallEnterpriseApplication) installs it fine.
  NOT usable: a human double-clicking it. Gatekeeper will refuse it.

To sign, set PN_SIGN_IDENTITY and re-run with --sign-only (or --notarize).
EOF
  exit 0
fi

# --------------------------------------------------------------------------- #
# Sign
# --------------------------------------------------------------------------- #
: "${PN_SIGN_IDENTITY:?set PN_SIGN_IDENTITY to your Developer ID Installer identity}"
command -v productsign >/dev/null || die "productsign not found"

# Fail before doing work if the identity is not actually present, rather than
# after: productsign's own error for a missing identity is easy to misread as a
# problem with the package.
if ! security find-identity -v 2>/dev/null | grep -Fq "$PN_SIGN_IDENTITY"; then
  echo "warning: '$PN_SIGN_IDENTITY' was not listed by 'security find-identity -v'." >&2
  echo "         Continuing, because installer identities are not always listed there," >&2
  echo "         but a failure on the next line means the certificate is missing." >&2
fi

say "productsign"
productsign --sign "$PN_SIGN_IDENTITY" "$DISTRIBUTION" "$SIGNED"
FINAL="$SIGNED"

say "verifying the signature"
pkgutil --check-signature "$FINAL"

if [ "$MODE" = "signed" ]; then
  say "built SIGNED (not notarized): $FINAL"
  echo
  echo "Gatekeeper on macOS 10.15+ requires NOTARIZATION as well for a"
  echo "double-clickable install. Re-run with --notarize for that."
  exit 0
fi

# --------------------------------------------------------------------------- #
# Notarize
# --------------------------------------------------------------------------- #
command -v xcrun >/dev/null || die "xcrun not found"
: "${PN_TEAM_ID:?set PN_TEAM_ID to your Apple Developer team id}"

NOTARY_ARGS=(--team-id "$PN_TEAM_ID" --wait)
if [ -n "${PN_ASC_KEY_ID:-}" ]; then
  # Preferred: an App Store Connect API key never prompts for 2FA, which is what
  # makes this work unattended in CI.
  : "${PN_ASC_ISSUER_ID:?PN_ASC_KEY_ID given, so PN_ASC_ISSUER_ID is required too}"
  : "${PN_ASC_KEY_PATH:?PN_ASC_KEY_ID given, so PN_ASC_KEY_PATH is required too}"
  [ -f "$PN_ASC_KEY_PATH" ] || die "no App Store Connect key at $PN_ASC_KEY_PATH"
  NOTARY_ARGS+=(--key "$PN_ASC_KEY_PATH" --key-id "$PN_ASC_KEY_ID" --issuer "$PN_ASC_ISSUER_ID")
else
  : "${PN_APPLE_ID:?set PN_ASC_KEY_ID (preferred) or PN_APPLE_ID}"
  : "${PN_APPLE_PASSWORD:?PN_APPLE_ID given, so PN_APPLE_PASSWORD (an app-specific password) is required}"
  NOTARY_ARGS+=(--apple-id "$PN_APPLE_ID" --password "$PN_APPLE_PASSWORD")
fi

say "xcrun notarytool submit --wait (this takes minutes, not seconds)"
# The credential values are in the argv of THIS call, which is unavoidable --
# notarytool takes them no other way. They are not echoed: `set -x` is never
# enabled in this script for exactly that reason.
if ! xcrun notarytool submit "$FINAL" "${NOTARY_ARGS[@]}"; then
  echo >&2
  echo "Notarization failed. The log says why, and it is worth reading rather" >&2
  echo "than guessing -- the usual cause is an unsigned or unhardened binary" >&2
  echo "inside the payload:" >&2
  # NOT "${NOTARY_ARGS[*]}": that array holds either --password with an
  # app-specific password, or the App Store Connect key id and issuer. Printing
  # it contradicts this script's own rule six lines above about never echoing
  # the credential. GitHub would mask a registered secret; a local run, or CI
  # anywhere else, would not.
  echo "  xcrun notarytool log <submission-id> <the same credential flags you" >&2
  echo "      passed above -- deliberately not echoed here>" >&2
  die "notarization failed"
fi

say "stapling"
# Stapling is what makes the ticket available OFFLINE. Without it a Mac with no
# route to Apple refuses a package that is perfectly notarized -- which describes
# a segmented client VLAN, i.e. exactly where these get installed.
xcrun stapler staple "$FINAL"
xcrun stapler validate "$FINAL"

say "verifying Gatekeeper accepts it"
# The real question is not "is it signed" but "will a Mac run it". spctl answers
# that one.
spctl --assess --type install -vv "$FINAL" || die "Gatekeeper still refuses it"

say "built SIGNED + NOTARIZED + STAPLED: $FINAL"
