"""HMAC signing for outbound events -- Stripe's scheme, and why that one.

WHAT IS SENT
------------
One header per delivery::

    X-PrinterNanny-Signature: t=1785312000,v1=<hex sha256 hmac>

where the signed material is ``b"<t>." + <the exact bytes of the request body>``
and the key is the subscription's own secret.

WHY STRIPE'S SCHEME AND NOT GITHUB'S
------------------------------------
GitHub's ``X-Hub-Signature-256`` is HMAC over the body alone. It proves the body
came from someone holding the secret, and nothing else -- so a captured request
stays valid forever and can be replayed at any time. Adding a timestamp in a
*separate* header does not fix that: an attacker rewrites that header freely,
because it is not covered by the MAC.

Stripe's scheme puts the timestamp inside the signed material, so the signature
binds the body to a moment. A consumer that checks the age of ``t`` before
comparing signatures gets replay protection out of the same operation, and an
attacker cannot move ``t`` without invalidating ``v1``. Two further properties
we specifically wanted:

* **The scheme is versioned in-band.** ``v1=`` is a label, not a fixed name, so a
  future ``v2=`` (a different hash, a different construction) can be sent
  *alongside* ``v1`` and consumers migrate one at a time instead of all at once.
  A bare hex digest has nowhere to put that.
* **It is the most widely implemented webhook signature there is.** An MSP
  wiring this into an existing tool very often already has verification code for
  Stripe-shaped signatures, and where they do not, every language has a
  copy-pasteable example. That matters more here than elegance: the security
  property is only real if the consumer actually verifies, and the cheapest
  verification is the one they have already written.

Idempotency (replay *detection*, as opposed to prevention) is a separate
mechanism and rides in the payload -- see ``events.catalogue`` and the
``X-PrinterNanny-Idempotency-Key`` header.

THE SECRET
----------
Generated here, never typed by an operator, shown once, and stored Fernet-
encrypted (``central.secrets``). 32 URL-safe random bytes -- ~192 bits, so
guessing it is not a threat model. It is a *signing* key, so unlike an API key
we could store only a hash of, we must be able to read it back to sign with it;
encryption at rest is exactly the right primitive and is what
``directory_connections.secret`` already uses.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

#: Header the signature travels in.
SIGNATURE_HEADER = "X-PrinterNanny-Signature"

#: In-band scheme label. A future algorithm ships as an additional ``v2=`` pair
#: in the same header rather than a replacement.
SCHEME = "v1"

#: How far a delivery's timestamp may be from the consumer's clock before the
#: signature should be rejected as a replay. Five minutes is Stripe's default and
#: is comfortably wider than any clock skew a self-hosted install will see.
DEFAULT_TOLERANCE_SECONDS = 300

SECRET_PREFIX = "pnevt_"


def generate_secret() -> str:
    """A fresh per-subscription signing secret.

    Prefixed so an operator who finds one in a ticket, a log or a config file can
    tell what it is and rotate the right thing -- the same reasoning as
    ``pn_`` / ``pnc_`` / ``pnw_`` in central.security.
    """
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def signed_payload(timestamp: int, body: bytes) -> bytes:
    """The exact bytes the MAC is computed over.

    Separated out so a test can assert the construction directly, and so the
    signer and the verifier cannot disagree about it -- a mismatch there yields a
    signature that fails for every consumer and looks like a key problem.
    """
    return str(int(timestamp)).encode("ascii") + b"." + body


def compute(secret: str, body: bytes, timestamp: int) -> str:
    """Raw hex digest for one (secret, body, timestamp)."""
    return hmac.new(
        (secret or "").encode("utf-8"),
        signed_payload(timestamp, body),
        hashlib.sha256,
    ).hexdigest()


def sign(secret: str, body: bytes, timestamp: Optional[int] = None) -> str:
    """The full header value: ``t=<unix>,v1=<hex>``."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return "t=%d,%s=%s" % (ts, SCHEME, compute(secret, body, ts))


def parse(header: str) -> "tuple":
    """Split a signature header into ``(timestamp_or_None, [digests])``.

    Tolerant of unknown pairs and of extra whitespace, because a proxy in the
    middle may reformat, and because a ``v2=`` we have not shipped yet must not
    make a ``v1=`` we did ship unparseable. Returns every digest offered for the
    current scheme, since a rotation window may legitimately send two.
    """
    timestamp = None
    digests = []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except (TypeError, ValueError):
                timestamp = None
        elif key == SCHEME and value:
            digests.append(value)
    return timestamp, digests


def verify(
    secret: str,
    body: bytes,
    header: str,
    *,
    now: Optional[int] = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Consumer-side verification, kept here so the scheme has one definition.

    Printer Nanny does not receive its own webhooks, so nothing in production
    calls this -- it exists because a signature scheme documented only in prose
    is a scheme with no executable statement of what "correct" means. The tests
    verify real signed bodies through it, and it is what an operator writing
    their own consumer can read (or port) instead of guessing.

    Both checks are mandatory and ordered: **age first, then digest**. A caller
    that compares digests and forgets the timestamp has authentication without
    replay protection, which is the exact failure the scheme was chosen to
    avoid.
    """
    timestamp, digests = parse(header)
    if timestamp is None or not digests:
        return False
    current = int(time.time()) if now is None else int(now)
    if tolerance_seconds is not None and abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute(secret, body, timestamp)
    # compare_digest, not ==: a byte-at-a-time comparison leaks the correct
    # prefix through timing, which is enough to forge given enough attempts.
    return any(hmac.compare_digest(expected, d) for d in digests)
