"""Where an outbound event is allowed to go -- the SSRF boundary.

An event subscription is an operator-supplied URL that this server then makes
requests to. That is server-side request forgery by construction, and the
question is not whether to have an opinion about it but which opinion. This
module is that decision, stated once and enforced in two places (when a
subscription is saved, and again immediately before every send).

THE DECISION
------------
1. **Scheme is http or https, and nothing else.** No ``file:``, ``gopher:``,
   ``ftp:``, ``data:``. Credentials embedded in the URL (``https://u:p@host``)
   are refused outright rather than stripped: they would be a second secret
   living in a column that IS rendered in the UI, which is exactly the mistake
   ``directory_connections`` was shaped to avoid.

2. **Link-local is refused always, with no override.** ``169.254.0.0/16`` and
   ``fe80::/10``. This is the cloud instance-metadata range
   (``169.254.169.254``) on every major provider, and reaching it means handing
   out the host's IAM credentials. There is no legitimate MSP webhook on a
   link-local address, so unlike the ranges below this one is not a trade-off
   and gets no setting. Unspecified (``0.0.0.0``, ``::``), multicast and
   IETF-reserved addresses are refused on the same terms.

3. **Loopback and private ranges are refused BY DEFAULT, and an operator can
   allow them** (``events.allow_private_destinations``, default off). This is
   the part that is a judgement call, so here is the judgement: Printer Nanny is
   self-hosted on an MSP's own infrastructure and the most likely subscriber is
   the MSP's own internal system, frequently on the same LAN
   (``http://helpdesk.lan:8080/hooks/printers``). A blanket refusal would be
   *safer in the abstract* and wrong in practice -- it would block the primary
   use case and push operators to work around it, which is how a security
   control ends up disabled everywhere. So the default is closed, the exception
   is one explicit, audited, documented setting, and the range whose compromise
   is catastrophic (2) is excluded from that exception.

4. **Names are resolved and every returned address is checked**, so
   ``evil.example.com`` pointing at ``169.254.169.254`` is refused exactly as
   the literal would be. A name that will not resolve is *not* a refusal at save
   time (a transient DNS failure must not block an operator saving a correct
   URL) but is an ordinary delivery failure at send time.

5. **Redirects are never followed** (see ``events.delivery``). A pre-flight
   address check is trivially defeated by a destination that answers ``302
   Location: http://169.254.169.254/``, and following redirects is what turns a
   checked destination back into an arbitrary one.

RESIDUAL RISK, STATED
---------------------
The resolve-then-connect gap is a TOCTOU window: we check the addresses a name
resolves to, then hand the *name* to httpx, which resolves it again. A DNS
rebinding attacker controlling the authoritative server can return a public
address to our check and a private one to the connection. Closing it properly
means resolving once and connecting to the pinned address while still sending
the original SNI/Host -- doable, but it means owning the transport, and it buys
protection against an attacker who already has the highest privilege in this
application (only an admin can create a subscription). The trade is recorded
here rather than left to be discovered: this control raises the bar against
accident and against a confused-deputy mistake, and is not a defence against a
malicious administrator.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

ALLOWED_SCHEMES = ("http", "https")


class DestinationError(ValueError):
    """A subscription URL this server refuses to make requests to.

    Its own type so a route can render it as a form error and a delivery can
    record it as a stated reason, without either having to string-match a bare
    ValueError.
    """


def _refuse_always(ip) -> Optional[str]:
    """Reasons that no setting may override. Returns a message or None.

    ``is_loopback`` short-circuits, because ``::1`` is *also* ``is_reserved``
    (it sits inside ``::/8``) and would otherwise be refused on terms an
    operator cannot override -- making IPv6 loopback permanently unreachable
    while IPv4 loopback obeyed the setting. Two spellings of one address must
    not have two policies.
    """
    if ip.is_loopback:
        return None
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_link_local:
        # 169.254.169.254 is the cloud metadata service on AWS, GCP, Azure,
        # DigitalOcean and Oracle alike. Naming it explicitly makes the refusal
        # message actionable rather than cryptic.
        return "a link-local address (this range carries cloud instance metadata)"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved:
        return "an IETF-reserved address"
    return None


def _refuse_unless_allowed(ip) -> Optional[str]:
    """Reasons ``events.allow_private_destinations`` may override."""
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_private:
        return "a private address"
    if not ip.is_global:
        # The backstop, and it is load-bearing rather than tidy: CGNAT
        # (100.64.0.0/10) is NOT in ``is_private`` on Python 3.9 -- it was added
        # to that predicate in a later release -- so a version-portable rule
        # cannot be built out of ``is_private`` alone. ``is_global`` is the
        # inverse question and answers it correctly for CGNAT, the
        # documentation ranges and benchmarking space on every version this runs
        # on. Checked last so the two cases above keep their specific message.
        return "not a globally-routable address"
    return None


def _normalise(ip):
    """Fold an IPv4-mapped IPv6 address down to its IPv4 form.

    ``::ffff:127.0.0.1`` is a loopback address wearing IPv6 clothing;
    ``IPv6Address.is_loopback`` is False for it, so without this the classifier
    below would read it as ordinary and let it through under a setting that only
    ever meant to allow real IPv6.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def classify(address: str, *, allow_private: bool) -> Optional[str]:
    """Why this literal IP is refused, or None if it is acceptable."""
    try:
        ip = _normalise(ipaddress.ip_address(address))
    except ValueError:
        return None  # Not an IP literal; the caller resolves the name instead.
    reason = _refuse_always(ip)
    if reason:
        return "%s is %s" % (address, reason)
    if not allow_private:
        reason = _refuse_unless_allowed(ip)
        if reason:
            return (
                "%s is %s -- enable 'Allow private destinations' in Settings if "
                "the subscriber really is on this network" % (address, reason)
            )
    return None


def resolve(host: str, port: int) -> List[str]:
    """Every address ``host`` currently resolves to.

    Raises ``socket.gaierror`` on failure; callers decide whether that is fatal
    (it is at send time, it is not at save time).
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    seen: List[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


def validate_url(
    url: str,
    *,
    allow_private: bool = False,
    resolve_names: bool = True,
    require_resolvable: bool = False,
) -> str:
    """Refuse anything this server must not POST to; return the normalised URL.

    ``require_resolvable`` is the difference between the two callers. At save
    time it is False -- a name that does not resolve right now is a name the
    operator may be about to create, and refusing it turns a DNS blip into a
    form error nobody can act on. At send time it is True, because a delivery to
    a name that will not resolve has to fail somewhere and failing here names
    the reason precisely.
    """
    raw = (url or "").strip()
    if not raw:
        raise DestinationError("a destination URL is required")

    # urlparse itself is lenient, but `.port` and `.hostname` raise ValueError on
    # a malformed authority ("http://h:abc/", "http://[::1/"). That must surface
    # as a refusal an operator can read, not as a 500 on the settings form or an
    # unhandled exception inside the worker's delivery sweep.
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        explicit_port = parsed.port
        has_credentials = bool(parsed.username or parsed.password)
    except ValueError as exc:
        raise DestinationError("destination URL could not be parsed: %s" % exc)

    if scheme not in ALLOWED_SCHEMES:
        raise DestinationError(
            "destination scheme must be http or https (got %r)" % (parsed.scheme or "",)
        )
    if has_credentials:
        raise DestinationError(
            "credentials in the URL are not accepted -- the signature header "
            "authenticates this server to the subscriber, and a password here "
            "would live in a column that is rendered in the UI"
        )
    if not host:
        raise DestinationError("destination URL has no host")

    port = explicit_port or (443 if scheme == "https" else 80)

    literal_reason = classify(host, allow_private=allow_private)
    if literal_reason:
        raise DestinationError("refusing to send to %s" % literal_reason)

    if resolve_names:
        try:
            addresses = resolve(host, port)
        except (socket.gaierror, socket.error, UnicodeError) as exc:
            if require_resolvable:
                raise DestinationError("cannot resolve %s: %s" % (host, exc))
            addresses = []
        for address in addresses:
            reason = classify(address, allow_private=allow_private)
            if reason:
                raise DestinationError(
                    "refusing to send to %s: it resolves to %s" % (host, reason)
                )

    # Drop any fragment: it is never transmitted, so keeping it in the stored URL
    # only invites an operator to believe it means something.
    return urlunparse(
        (scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, "")
    )
