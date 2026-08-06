"""Integrity for the third-party binaries this server bakes into an installer.

Two artifacts are fetched from the internet and shipped, unmodified, to customer
workstations: the **Python embeddable runtime** (``central.msi_builder``) and
**NSSM** (``central.dashboard.installer``). NSSM is not an incidental
dependency -- it is the *service host*, the process that runs the workstation
agent as LocalSystem. A substituted ``nssm.exe`` is SYSTEM-level code execution
on every machine in a client's fleet, delivered by the MSP's own installer.

Before this module the only check on either download was a size floor
(``< 1_000_000`` bytes -> "likely an HTML error page"), which is a sanity check
against a captive portal and says so in its own comment. Nothing verified the
bytes, nothing re-checked the cache, and both fetches followed redirects.

This is the same class of risk the driver-package path already handles
correctly -- ``DriverPackage.sha256`` is computed on upload and **re-verified on
the workstation before anything is unpacked** -- so the rule below is not a new
policy, it is the existing one applied to the two artifacts that were missed.

WHY THERE IS NO HARDCODED DIGEST CONSTANT
-----------------------------------------
The obvious fix is ``EXPECTED_SHA256 = "abc123..."`` next to each URL. It is not
implementable honestly here:

* **python.org does not publish a SHA-256** for release artifacts. The release
  page carries an MD5 and a Sigstore bundle, nothing else. Pinning the MD5 that
  *is* published would be pinning a hash with practical chosen-prefix collision
  attacks against a file an attacker may also control.
* **nssm.cc publishes no checksum at all.**
* Downloading the file once and pasting the digest we happened to receive is
  trust-on-first-use wearing a constant's clothing -- and if that one download
  were tampered with, the tampering becomes canonical, blessed by a code review
  that cannot tell the difference.

Rejected: verifying python.org's Sigstore bundle. It is the only cryptographically
sound option for that artifact, and it costs a new runtime dependency
(``sigstore``, which pulls in its own transitive tree) plus a trust-root update
path, on a server whose whole deployment story is "no egress required". Worth
revisiting if this file ever needs to be stronger; recorded here so the next
reader knows it was considered rather than missed.

WHAT THIS MODULE DOES INSTEAD
-----------------------------
Verification that does not depend on a constant nobody can authenticate:

1. **An operator-supplied pin is honoured absolutely.** Set
   ``agent.python_embed_sha256`` (or ``PN_NSSM_SHA256``) and every fetch and
   every cache read is checked against it; a mismatch is fatal and the artifact
   is removed rather than left to be picked up by the next build. This is the
   configuration that closes the risk completely, and it is what an operator who
   mirrors these artifacts internally should set.

2. **Otherwise the first fetch is pinned on arrival** and every later use is
   verified against that pin (a ``.sha256`` sidecar next to the cached file).
   This does not authenticate the first download -- nothing available here can --
   but it converts the cache from "trusted because the file exists" into
   something that detects tampering on the cache volume, a poisoned partial
   write, and silent upstream substitution on any subsequent build.

3. **Plaintext HTTP is refused outright** for remote fetches, and so is a
   redirect that lands on it. Both URLs are operator-overridable free text with
   no scheme restriction, so ``http://`` was previously accepted and a redirect
   could downgrade an ``https://`` one. TLS is what authenticates the default
   path; allowing it to be dropped silently made the default no stronger than
   its weakest configuration.

Local and ``file://`` paths are exempt from (3) and, absent an explicit pin,
from (2): an operator who staged the zip on the server's own disk has already
placed it inside the trust boundary, and writing sidecars next to their files
would be surprising. A pin, if configured, is still enforced.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("printer_nanny.artifact_integrity")

#: Suffix of the sidecar carrying a cached artifact's pinned digest.
PIN_SUFFIX = ".sha256"

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

#: Schemes we will fetch over. Deliberately not a "warn and continue": see (3).
SECURE_REMOTE_SCHEMES = ("https://",)
LOCAL_SCHEMES = ("file://",)


class ArtifactIntegrityError(RuntimeError):
    """An artifact this server refuses to use.

    Its own type so a build surfaces "the runtime did not match its pin" as a
    stated reason rather than as an anonymous RuntimeError from three frames
    down, and so a caller can tell an integrity refusal apart from a network
    failure -- they need different operator advice.
    """


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Digest a file without reading it entirely into memory.

    The Python embeddable zip is ~11MB and the MSI build already holds several
    copies of the runtime tree; streaming keeps this from being the thing that
    pushes a small container over its limit.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def normalise_pin(value: Optional[str]) -> Optional[str]:
    """Validate an operator-supplied digest, or None when none was given.

    A malformed pin raises rather than being ignored. Silently skipping
    verification because somebody pasted 63 characters is precisely how a
    control ends up believed-in and absent -- the operator set the field, so
    they must be told it did not take effect.
    """
    text = (value or "").strip().lower()
    if not text:
        return None
    if not _HEX64.match(text):
        raise ArtifactIntegrityError(
            "expected a 64-character hex SHA-256 digest, got %r -- check the "
            "pinned checksum setting" % (value,)
        )
    return text


def is_remote(url: str) -> bool:
    """True when this needs to be fetched over the network."""
    return "://" in url and not url.startswith(LOCAL_SCHEMES)


def require_secure_url(url: str, *, label: str) -> str:
    """Refuse a remote artifact URL that is not HTTPS. Returns the URL."""
    if not is_remote(url):
        return url
    if not url.startswith(SECURE_REMOTE_SCHEMES):
        raise ArtifactIntegrityError(
            "%s must be fetched over https (got %r). This artifact is installed "
            "as a service on customer workstations, so plaintext transport would "
            "let anyone on the path replace it. Use https, a file:// path, or "
            "stage the file in the cache directory." % (label, url)
        )
    return url


def assert_secure_response_url(final_url: str, *, label: str) -> None:
    """Refuse a redirect chain that ended somewhere insecure.

    Checking the configured URL is not enough: redirects are followed, so an
    ``https://`` mirror answering ``302 Location: http://...`` would otherwise
    downgrade the fetch after the check had already passed.
    """
    text = str(final_url or "")
    if text and is_remote(text) and not text.startswith(SECURE_REMOTE_SCHEMES):
        raise ArtifactIntegrityError(
            "%s redirected to a non-https URL (%s); refusing the download"
            % (label, text)
        )


def pin_path(path: Path) -> Path:
    """Sidecar location for a cached artifact's pinned digest."""
    return path.with_name(path.name + PIN_SUFFIX)


def read_pin(path: Path) -> Optional[str]:
    sidecar = pin_path(path)
    try:
        return normalise_pin(sidecar.read_text(encoding="ascii"))
    except OSError:
        return None
    except ArtifactIntegrityError:
        # A corrupt sidecar must not be treated as "no pin" -- that would make
        # corrupting it the way to disable verification.
        raise ArtifactIntegrityError(
            "the pinned digest at %s is not a valid SHA-256; delete the cached "
            "artifact and its %s sidecar to re-fetch" % (sidecar, PIN_SUFFIX)
        )


def write_pin(path: Path, digest: str) -> None:
    sidecar = pin_path(path)
    tmp = sidecar.with_name(sidecar.name + ".part")
    tmp.write_text(digest + "\n", encoding="ascii")
    tmp.replace(sidecar)


def verify_bytes(data: bytes, *, expected: Optional[str], label: str) -> str:
    """Digest ``data``; raise when a pin was supplied and does not match."""
    actual = sha256_bytes(data)
    if expected and actual != expected:
        raise ArtifactIntegrityError(
            "%s failed its checksum: expected %s, got %s. The download does not "
            "match the pinned digest -- refusing to build an installer from it."
            % (label, expected, actual)
        )
    return actual


def verify_or_pin_file(
    path: Path, *, expected: Optional[str] = None, label: str, allow_pin: bool = True
) -> str:
    """Check a cached artifact, pinning it on first sight. Returns the digest.

    ``expected`` (an operator's pin) always wins. Otherwise the sidecar written
    by the first successful fetch is the reference. A mismatch raises and the
    caller is expected to discard the file -- see ``discard``.
    """
    actual = sha256_file(path)
    reference = expected or (read_pin(path) if allow_pin else None)
    if reference and actual != reference:
        raise ArtifactIntegrityError(
            "%s at %s failed its checksum: expected %s, got %s. The cached file "
            "has changed since it was pinned; it has been discarded and will be "
            "re-fetched on the next attempt." % (label, path, reference, actual)
        )
    if allow_pin and not reference:
        write_pin(path, actual)
        log.warning(
            "%s pinned on first use as %s (no checksum was configured, so this "
            "first fetch is trusted; set a pinned checksum to verify it too)",
            label, actual,
        )
    return actual


def discard(path: Path) -> None:
    """Remove a rejected artifact and its pin, best effort.

    A file that failed verification must not survive to be picked up by the next
    build: leaving it means the failure is reported once and the bad bytes are
    used forever after, since the size heuristic this replaced would accept it.
    """
    for target in (path, pin_path(path)):
        try:
            target.unlink()
        except OSError:
            pass
