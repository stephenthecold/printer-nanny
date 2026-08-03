"""Typed, signed outbound events -- the integration surface.

PSA integration was dropped from this product on purpose (FreeScout stays the
ticket path). This is what replaces it: an MSP wires Printer Nanny into whatever
they already run -- ERP, procurement, an internal automation -- by subscribing to
a small set of documented event types, rather than by Printer Nanny growing a
model of their commercial world.

Four pieces, each with one job:

``catalogue``    what may ever be sent, versioned per type, plus the one
                 normalisation every device string passes through.
``signing``      the HMAC scheme (Stripe-shaped) and why that one.
``destinations`` which URLs this server will make requests to -- the SSRF
                 boundary, with its decision written down.
``emit``         recording an event and fanning it out, and the tenancy
                 invariant that decides who is allowed to receive it.
``delivery``     the envelope on the wire and the retry sweeper, which reuses
                 the notification machinery rather than growing a second one.
"""

from central.events.catalogue import (
    CATALOGUE,
    EVENT_TYPE_NAMES,
    EventType,
    UnknownEventType,
)
from central.events.destinations import DestinationError, validate_url
from central.events.emit import (
    emit,
    emit_alert_opened,
    emit_alert_resolved,
    emit_printer_offline,
    emit_supply_reorder_recommended,
    scope_allows,
    subscriptions_for,
)

__all__ = [
    "CATALOGUE",
    "EVENT_TYPE_NAMES",
    "DestinationError",
    "EventType",
    "UnknownEventType",
    "emit",
    "emit_alert_opened",
    "emit_alert_resolved",
    "emit_printer_offline",
    "emit_supply_reorder_recommended",
    "scope_allows",
    "subscriptions_for",
    "validate_url",
]
