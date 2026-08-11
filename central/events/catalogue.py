"""The event catalogue: what Printer Nanny will ever send, and in what shape.

Every outbound event is one of these types. The registry is the contract: an
``emit`` of a type that is not here raises rather than shipping an event nobody
documented, because the whole value of a typed surface is that a consumer can
write a handler against a fixed set of names and be told when that set changes.

VERSIONING
----------
``version`` is per TYPE, not per release. It is bumped only on a **breaking**
change to that type's ``data`` block -- removing a field, renaming one, or
changing what an existing field means. Adding a field is not breaking: consumers
are told, here and in the docs, to ignore keys they do not recognise. That rule
is what lets this ship new data without a flag day across every MSP's
integration.

DEVICE STRINGS
--------------
Almost every string in here originates on a customer LAN: SNMP sysDescr, a
front-panel message, an operator's free-text display name. Two rules, and only
two:

* **Quoting is json.dumps's job and nothing else's.** A model of
  ``x", "evil": "`` cannot break out of a JSON string literal -- the serializer
  escapes the quote and the backslash, and the result parses back to exactly the
  original characters on the consumer's side. HTML-escaping on top of that would
  be actively wrong: the consumer is a JSON API, not a browser, so a printer at
  "Sales & Marketing" must arrive as ``Sales & Marketing`` and not as
  ``Sales &amp; Marketing`` in somebody's database. The escaping added
  per-channel for the HTML/Markdown-rendering channels (FreeScout, Slack) is
  needed *because* those render; this one does not.
* **Bounded and normalised, which is not the same as escaped.** ``_text`` caps
  length and strips C0/C1 control characters. That is not about quoting -- it is
  because a device is free to return a 40KB sysDescr or an embedded NUL, and
  neither belongs in a payload that will be POSTed to somebody's helpdesk and
  probably stored in a text column that rejects ``\\u0000``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Optional, Tuple

#: Longest a single device/operator string may be in a payload. Generous enough
#: for a real sysDescr, short enough that a hostile device cannot inflate a
#: delivery into a denial of service against the subscriber.
MAX_TEXT = 500

#: C0 and C1 control characters, minus tab/newline which are legitimate inside a
#: multi-line detail string. ``\x7f`` (DEL) is in the same category.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _text(value: Any, limit: int = MAX_TEXT) -> Optional[str]:
    """Normalise one device- or operator-supplied string for a JSON payload.

    Not escaping -- see the module docstring. This bounds length, drops control
    characters, and normalises Unicode to NFC so two spellings of the same name
    compare equal downstream. ``None`` stays ``None``: a field we do not know is
    reported as unknown, never as an empty string that reads like "no toner
    fitted" when it means "the device did not say".
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text)
    text = text.strip()
    if not text:
        return None
    if len(text) > limit:
        # Marked rather than silently cut, so a consumer debugging a truncated
        # model number can see that we did it and did not receive it that way.
        return text[: limit - 1] + "…"
    return text


class EventType:
    """One entry in the catalogue.

    ``fields`` is documentation, not validation: it is what the subscriptions
    page shows an operator deciding whether to subscribe, and what a reviewer
    checks a builder against. Validating it would turn "we added a field" into a
    runtime failure, which is the opposite of the additive-is-safe rule above.
    """

    __slots__ = ("name", "version", "summary", "fields")

    def __init__(self, name: str, version: int, summary: str, fields: Tuple[str, ...]):
        self.name = name
        self.version = version
        self.summary = summary
        self.fields = fields

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "EventType(%r, v%d)" % (self.name, self.version)


#: Every type that may ever leave this system. Adding one here is the whole of
#: "adding an event type" apart from calling ``emit``.
CATALOGUE: Dict[str, EventType] = {
    t.name: t
    for t in (
        EventType(
            "alert.opened",
            1,
            "A fleet condition opened: low supply, an error code, an offline "
            "device or agent, a maintenance schedule coming due.",
            (
                "alert.id", "alert.type", "alert.severity", "alert.title",
                "alert.detail", "alert.dedupe_key", "alert.opened_at",
                "printer.id", "printer.name", "printer.ip", "printer.model",
                "printer.serial", "printer.location", "site.id", "site.name",
            ),
        ),
        EventType(
            "alert.resolved",
            1,
            "A previously-opened condition cleared, was serviced, or was closed "
            "by an operator.",
            (
                "alert.id", "alert.type", "alert.severity", "alert.title",
                "alert.dedupe_key", "alert.opened_at", "alert.resolved_at",
                "printer.id", "printer.name", "printer.ip", "site.id", "site.name",
            ),
        ),
        EventType(
            "printer.offline",
            1,
            "A printer stopped answering polls for longer than the configured "
            "grace period, while its site's agent was still reporting in.",
            (
                "printer.id", "printer.name", "printer.ip", "printer.model",
                "printer.serial", "printer.location", "printer.last_seen",
                "site.id", "site.name",
            ),
        ),
        EventType(
            "supply.reorder_recommended",
            1,
            "A supply is forecast to run out inside the configured lead time. "
            "A recommendation only -- it does not claim a vendor order was placed.",
            (
                "printer.id", "printer.name", "printer.ip", "printer.model",
                "printer.serial", "printer.location",
                "supply.id", "supply.type", "supply.color", "supply.description",
                "supply.level_pct", "supply.is_receptacle",
                "supply.days_to_empty", "supply.order_by",
                "site.id", "site.name",
            ),
        ),
        EventType(
            "supply.replaced",
            1,
            "A cartridge was replaced: the supply level rose, which is the only "
            "replacement signal a printer gives. Carries what the cartridge that "
            "came OUT delivered -- pages, level consumed, how long it lasted.",
            (
                "printer.id", "printer.name", "printer.ip", "printer.model",
                "printer.serial", "printer.location",
                "supply.type", "supply.color",
                "cycle.id", "cycle.started_at", "cycle.ended_at", "cycle.pages",
                "cycle.start_level_pct", "cycle.min_level_pct",
                "cycle.consumed_pct", "cycle.observed_yield_pages",
                "cycle.complete", "site.id", "site.name",
            ),
        ),
        EventType(
            "supply.yield_below_expected",
            1,
            "A supply slot's measured pages-per-cartridge is persistently below "
            "what a cartridge for this model is expected to deliver. A "
            "MEASUREMENT, not a verdict: a shortfall is consistent with non-OEM "
            "or refilled cartridges, and equally with heavy page coverage. The "
            "expectation's source is carried so a consumer can weigh it.",
            (
                "printer.id", "printer.name", "printer.ip", "printer.model",
                "printer.serial", "printer.location",
                "supply.type", "supply.color",
                "yield.observed_pages", "yield.expected_pages",
                "yield.expected_source", "yield.expected_detail",
                "yield.shortfall_pct", "yield.threshold_pct",
                "yield.cycles_counted", "yield.last_replaced_at",
                "site.id", "site.name",
            ),
        ),
    )
}

#: Stable ordering for the UI and for tests, so a new type does not reshuffle a
#: form and invalidate whatever an operator had ticked.
EVENT_TYPE_NAMES: Tuple[str, ...] = tuple(sorted(CATALOGUE))


class UnknownEventType(ValueError):
    """An event type that is not in the catalogue.

    Its own type so ``emit``'s callers can tell "I typo'd a name" from any other
    ValueError. Never caught and swallowed: an event nobody declared is an event
    no consumer can have written a handler for.
    """


def require(event_type: str) -> EventType:
    spec = CATALOGUE.get(event_type)
    if spec is None:
        raise UnknownEventType(
            "%r is not in the event catalogue (known: %s)"
            % (event_type, ", ".join(EVENT_TYPE_NAMES))
        )
    return spec
