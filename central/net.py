"""Where a request came from -- for the two questions that need different answers.

``request.client.host`` cannot serve both. ``ProxyHeadersMiddleware`` is installed
with ``trusted_hosts="*"`` so that ``request.base_url`` comes back as https behind
Caddy, which means it rewrites ``request.client`` from ``X-Forwarded-For`` sent by
*anyone*. The comment at that call site justifies the trust with "the proxy is a
trusted hop (same docker network or LAN)" -- but docker-compose publishes the api
on ``${API_PORT:-8000}`` of the host, and the README tells operators to point their
own proxy at it, so the port is routinely reachable without passing through any
proxy at all. Anything reachable there can set that header to whatever it likes.

For display and for the audit trail that is an acceptable trade (it is what an
operator wants to read, and a forged value is a forged value in a log). For a rate
limiter it is fatal: one header per request buys an attacker an unlimited supply of
fresh buckets. So the throttle keys on a source the caller cannot choose:

* the **TCP peer** by default -- unspoofable, but shared by everyone when a reverse
  proxy is in front, which is why the per-source budget is set much higher than the
  per-username one;
* the real client, unwrapped from ``X-Forwarded-For``, **only** when the peer is
  inside ``auth.trusted_proxies``. An outsider hitting the port directly is not in
  that list, so their header is ignored; the proxy is, so its header is believed.

The peer is captured by ``PeerAddressMiddleware`` (central/middleware.py), which
sits OUTSIDE ProxyHeadersMiddleware in the stack -- once ProxyHeaders has run, the
original peer is gone, because it overwrites ``scope["client"]`` in place.
"""

from __future__ import annotations

import ipaddress
from typing import Any, List

#: Scope key the peer address is stashed under. A top-level key rather than
#: ``scope["state"]`` so nothing that rebuilds per-request state can drop it.
PEER_SCOPE_KEY = "pn_peer_ip"

UNKNOWN = "unknown"


def peer_ip(request: Any) -> str:
    """The TCP peer, before any proxy header was believed."""
    stashed = request.scope.get(PEER_SCOPE_KEY)
    if stashed:
        return str(stashed)
    client = request.scope.get("client")
    if client:
        return str(client[0])
    return ""


def parse_networks(spec: str) -> List[Any]:
    """Parse a comma/space separated list of IPs and CIDRs, skipping junk."""
    out: List[Any] = []
    for chunk in (spec or "").replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            continue  # an operator typo must not take the login route down
    return out


def _within(address: str, networks: List[Any]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def throttle_ip(request: Any, trusted_proxies: str = "") -> str:
    """The source identity a rate limiter may safely key on.

    Walks ``X-Forwarded-For`` right-to-left because the rightmost entry is the one
    the nearest proxy appended and the leftmost is whatever the client claimed;
    the first hop that is not itself a trusted proxy is the real client. Entries
    that are not IP addresses are skipped rather than trusted, so a client that
    prepends garbage cannot mint bucket keys.
    """
    peer = peer_ip(request)
    networks = parse_networks(trusted_proxies)
    if not networks or not _within(peer, networks):
        return peer or UNKNOWN
    forwarded = ""
    try:
        forwarded = request.headers.get("x-forwarded-for", "") or ""
    except Exception:  # noqa: BLE001 - a header store that will not answer is not fatal
        forwarded = ""
    for hop in reversed([h.strip() for h in forwarded.split(",")]):
        if not hop or _within(hop, networks):
            continue
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            continue
        return hop
    return peer or UNKNOWN
