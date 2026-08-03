"""The definition-driven provider: central's device definitions, applied.

This is the agent half of "a new printer model must not need an agent release".
Central serves definitions (see ``printer_nanny_agent.definitions`` for the
data language and its validator); this module caches them, picks the one that
applies to a device, reads the OIDs it names, and folds the answers into the
reading the standard Printer-MIB poller already built.

PRECEDENCE -- THE DECISION, STATED
----------------------------------
**Built-in providers run first; a definition runs last and FILLS ONLY.**

That ordering is why this module is imported last in ``providers/__init__.py``
and it is not incidental. The built-in providers are hardware-proven code:
Brother's four passes and HP's private-MIB read have been run against real
devices. A definition is data somebody typed, which by construction has never
executed anywhere. So a definition may:

* fill a supply whose ``level_pct`` is still ``None`` (the case it exists for --
  the standard MIB reported a bucket, or nothing at all),
* add a supply row no provider produced,
* fill meters, status text and identity fields that are still absent,

and it may **not** replace a value a built-in already established. A definition
that could silently overwrite a working provider is a mechanism for making a
working printer stop working with no release, no diff and no way to correlate.

``override_builtin`` exists for the case the fill-only rule cannot reach: a
built-in that decodes a *particular* model wrongly, which is exactly a thing you
want to fix without an agent release. It defaults off, an operator sets it
per-definition in the UI, and every field it overrides is named in
``provider_trace`` -- so central shows what happened on that printer's own
detail page. The contract is "never silently", not "never".

ONE DEFINITION PER PRINTER, TIES REFUSED
----------------------------------------
Two definitions claiming one device is two sets of private-MIB OIDs pointed at
the same supply rows; applying both is a coin flip. The most specific match wins
(longer model tag, then brand, then enterprise) and an exact tie is refused and
logged -- the same discipline as an ambiguous driver-package match, for the same
reason: a wrong decode is worse than no decode, because it looks like data.

DEGRADING
---------
With no definitions loaded, ``detect`` returns False and this provider is never
entered -- not even a trace row. An agent that cannot reach central, has no
cache, or holds a cache it refuses is byte-for-byte the agent that shipped
before this feature existed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

from printer_nanny_agent import definitions as defs
from printer_nanny_agent.providers import PrinterProvider, enterprise_matches, record_meters, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams
from printer_nanny_agent.snmp_parse import decode_supply_text

log = logging.getLogger("printer_nanny_agent.providers.definitions")

#: OIDs per SNMP GET. A definition may name up to 32, and stuffing all of them
#: into one PDU is how a device answers ``tooBig`` and the whole read yields
#: nothing. Four round trips at worst, against a device we are already talking
#: to, is the cheaper failure mode.
_GET_CHUNK = 10

#: Longest status/identity string a definition may write into a reading. The
#: device supplies this text, so it is hostile input by this project's standing
#: rule, and it ends up in the dashboard, in alert titles and in exports.
_MAX_VALUE_TEXT = 200


# --------------------------------------------------------------------------- #
# The active set
# --------------------------------------------------------------------------- #
# Module-level, and deliberately so. ``run_providers`` iterates a module-level
# registry of singleton providers, and ``poll_printer`` takes no definitions
# argument -- threading one through would change the signature of every poll
# entry point for a feature that is optional. The set is swapped atomically
# (rebind a tuple, never mutate one) so the 16 concurrent polls in a cycle
# either all see the old set or all see the new one, and no poll ever observes
# a half-updated list.
_ACTIVE: Tuple[dict, ...] = ()


def set_active(definitions: Sequence[dict]) -> int:
    """Validate and install ``definitions`` as the active set. Returns the count.

    Validation happens HERE rather than at the caller so there is exactly one
    door into the active set. A definition that does not validate is dropped
    with its reason logged and the rest are still installed -- one bad row must
    not cost an operator every other definition they wrote.
    """
    global _ACTIVE
    good: List[dict] = []
    for raw in definitions or ():
        try:
            good.append(defs.validate_definition(raw))
        except defs.DefinitionError as exc:
            key = raw.get("key") if isinstance(raw, dict) else None
            log.warning("refusing device definition %r: %s", key, exc)
    _ACTIVE = tuple(good)
    return len(_ACTIVE)


def active() -> Tuple[dict, ...]:
    return _ACTIVE


def clear() -> None:
    """Drop every definition. The agent is then exactly what it was before."""
    global _ACTIVE
    _ACTIVE = ()


# --------------------------------------------------------------------------- #
# Local cache
# --------------------------------------------------------------------------- #
def key_fingerprint(api_key: str) -> str:
    """SHA-256 hex of this agent's API key.

    Identical by construction to what central stores as ``agents.api_key_hash``
    (``central.security.hash_api_key``), which is what lets both ends key the
    feed HMAC on a value neither has to transmit. The agent holds the key and
    derives this; central holds only this and never sees the key again after
    enrollment.
    """
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


class DefinitionStore:
    """The agent's on-disk copy of the feed.

    Exists so a restart -- or a boot with central unreachable, which is exactly
    when a freshly imaged site agent starts -- does not lose the definitions the
    fleet depends on.

    The signature is re-checked on every load, keyed on this agent's own
    credential. That is what makes a cache file copied from another agent, or
    edited on disk, fail closed rather than becoming a way to hand one agent
    another's (or an attacker's) OID list. A refused cache degrades to no
    definitions, never to unchecked ones.
    """

    def __init__(self, path: str):
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def load(self, api_key_hash: str) -> Optional[dict]:
        """Return the cached payload, or None if absent, unreadable or refused."""
        try:
            if not os.path.exists(self._path):
                return None
            if os.path.getsize(self._path) > defs.MAX_PAYLOAD_BYTES:
                log.warning("definition cache %s is oversized; ignoring", self._path)
                return None
            with open(self._path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError, RecursionError) as exc:
            log.warning("could not read definition cache %s: %s", self._path, exc)
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        definitions = payload.get("definitions")
        if not isinstance(version, str) or not isinstance(definitions, list):
            return None
        if not defs.signature_ok(
            api_key_hash, version, definitions, payload.get("signature")
        ):
            log.warning(
                "definition cache %s does not verify against this agent's "
                "credential -- ignoring it (a cache from another agent, or an "
                "edited one, is refused rather than trusted)",
                self._path,
            )
            return None
        return payload

    def save(self, payload: dict) -> bool:
        """Persist ``payload`` atomically. Best-effort: never raises."""
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".defs-", suffix=".tmp")
            try:
                # 0600 before anything is written. The file holds no secret
                # today, but it decides what every printer at this site is read
                # with, and a world-writable one would be a way to change that.
                os.chmod(tmp, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, separators=(",", ":"))
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except (OSError, ValueError) as exc:
            log.warning("could not write definition cache %s: %s", self._path, exc)
            return False
        return True


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _reading_brand(reading: dict) -> str:
    identity = reading.get("identity") or {}
    return str(reading.get("brand") or identity.get("brand") or "").lower()


def _reading_model(reading: dict) -> str:
    identity = reading.get("identity") or {}
    return str(reading.get("model") or identity.get("model") or "").lower()


def _specificity(match: dict) -> Tuple[int, int, int]:
    """How specific a match is. Compared as a tuple, so a model tag always beats
    a brand tag however long the brand tag is -- "longest tag wins" within a
    criterion, never across them."""
    return (
        len(match.get("model", "")),
        len(match.get("brand", "")),
        len(match.get("enterprise", "")),
    )


def definition_matches(definition: dict, reading: dict, sys_object_id: Optional[str]) -> bool:
    """True when every criterion the definition states is satisfied.

    Criteria are AND-ed and only stated ones are tested; a definition with no
    criteria at all cannot exist (the validator refuses it), so this can never
    degenerate into "matches everything".
    """
    match = definition.get("match") or {}
    enterprise = match.get("enterprise")
    if enterprise and not enterprise_matches(enterprise, sys_object_id):
        return False
    brand = match.get("brand")
    if brand and brand not in _reading_brand(reading):
        return False
    model = match.get("model")
    if model and model not in _reading_model(reading):
        return False
    return True


def select_definition(
    definitions: Sequence[dict], reading: dict, sys_object_id: Optional[str]
) -> Optional[dict]:
    """The one definition that applies to this device, or None.

    Most specific wins; an exact tie is refused rather than guessed, because
    two definitions over one device's supply rows is a coin flip whose losing
    side looks exactly like data.
    """
    candidates = [d for d in definitions if definition_matches(d, reading, sys_object_id)]
    if not candidates:
        return None
    best = max(_specificity(d.get("match") or {}) for d in candidates)
    winners = [d for d in candidates if _specificity(d.get("match") or {}) == best]
    if len(winners) != 1:
        log.warning(
            "%s matches %d device definitions equally (%s); refusing to guess",
            reading.get("ip") or "device", len(winners),
            ", ".join(sorted(d.get("key", "?") for d in winners)),
        )
        return None
    return winners[0]


# --------------------------------------------------------------------------- #
# Decoding -- the entire interpreter for a definition
# --------------------------------------------------------------------------- #
def _raw(values: Dict[str, Optional[str]], oid: str) -> Optional[str]:
    text = values.get(oid)
    if text is None:
        return None
    text = str(text).strip()
    if not text or text.startswith(("No Such Object", "No Such Instance")):
        return None
    return text


def _as_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    try:
        return int(text, 10)
    except (TypeError, ValueError):
        return None


def _as_text(text: Optional[str]) -> Optional[str]:
    """Device text, hex-decoded where the device sent an OCTET STRING as bytes."""
    if text is None:
        return None
    decoded = decode_supply_text(text, "")
    decoded = (decoded or "").strip()
    return decoded[:_MAX_VALUE_TEXT] or None


def decode_percent(decode: dict, values: Dict[str, Optional[str]], oid: str) -> Optional[float]:
    """Turn one OID's answer into a 0-100 percentage, or None.

    Every path returns None rather than guessing. A value that does not decode
    is a value we do not have, and reporting a wrong percentage as a supply
    level is how a definition causes an order that should not have been placed.
    """
    kind = decode.get("kind")
    raw = _raw(values, oid)
    if raw is None:
        return None

    if kind == "percent":
        num = _as_int(raw)
        # Out of range is a WRONG decode, not a full cartridge. Clamping to 100
        # would report "full" for a misread, which is the failure that costs an
        # operator a printer that runs dry with a green dot next to it.
        return float(num) if num is not None and 0 <= num <= 100 else None

    if kind == "ratio":
        current = _as_int(raw)
        if "max_oid" in decode:
            maximum = _as_int(_raw(values, decode["max_oid"]))
        else:
            maximum = decode.get("max")
        if current is None or maximum is None or maximum <= 0:
            return None
        if current < 0 or current > maximum:
            return None
        return round((current / maximum) * 100.0, 1)

    if kind == "map":
        mapped = decode.get("values") or {}
        hit = mapped.get(raw)
        return float(hit) if isinstance(hit, int) and not isinstance(hit, bool) else None

    if kind == "text_between":
        text = _as_text(raw)
        if text is None:
            return None
        after, before = decode.get("after"), decode.get("before")
        if after:
            index = text.find(after)
            if index < 0:
                return None
            text = text[index + len(after):]
        if before:
            index = text.find(before)
            if index < 0:
                return None
            text = text[:index]
        num = _as_int(text.strip())
        return float(num) if num is not None and 0 <= num <= 100 else None

    return None


def decode_count(values: Dict[str, Optional[str]], oid: str) -> Optional[int]:
    """A non-negative meter reading, or None. Bounded to what central's INT4
    columns hold, so a misdecode cannot 500 the ingest and drop the batch."""
    num = _as_int(_raw(values, oid))
    if num is None or num < 0 or num > 2**31 - 1:
        return None
    return num


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def _find_supply(reading: dict, supply_type: str, color: Optional[str]) -> Optional[dict]:
    """The existing supply row this definition entry refers to.

    Keyed on ``(type, color)`` exactly -- which is the same key central's
    ``upsert_supply`` uses. Matching more loosely here would append a row that
    central then collapses onto an existing one, so the looser match would
    silently become an overwrite by another name.
    """
    for supply in reading.get("supplies") or []:
        if str(supply.get("type") or "").lower() != supply_type:
            continue
        existing = supply.get("color")
        existing = str(existing).lower() if existing else None
        if existing == (color or None):
            return supply
    return None


def apply_definition(
    definition: dict, values: Dict[str, Optional[str]], reading: dict
) -> dict:
    """Fold decoded OID answers into ``reading`` in place. Returns a report.

    Pure apart from the mutation of ``reading``: no I/O, no clock, no network,
    so the precedence rules above are testable without a device. The report
    (``filled`` / ``added`` / ``held`` / ``overrode`` / ``dropped``) is what
    reaches central's printer detail page through ``provider_trace``, which is
    how "never silently" is actually delivered rather than merely promised.
    """
    override = bool(definition.get("override_builtin"))
    report: Dict[str, List[str]] = {
        "filled": [], "added": [], "held": [], "overrode": [], "dropped": []
    }

    for entry in definition.get("supplies") or []:
        supply_type = entry["type"]
        color = entry.get("color")
        label = f"{color} {supply_type}" if color else supply_type
        try:
            pct = decode_percent(entry["decode"], values, entry["oid"])
        except (TypeError, ValueError, ZeroDivisionError):
            pct = None
        if pct is None:
            report["dropped"].append(label)
            continue
        existing = _find_supply(reading, supply_type, color)
        if existing is None:
            reading.setdefault("supplies", []).append(
                {
                    "type": supply_type,
                    "color": color,
                    "description": entry.get("description") or label,
                    "level_pct": pct,
                    "status_note": None,
                    "current": None,
                    "max_capacity": None,
                }
            )
            report["added"].append(f"{label} {pct:.0f}%")
        elif existing.get("level_pct") is None:
            existing["level_pct"] = pct
            # A note like "some remaining" described the absence of a number.
            # Leaving it beside a real percentage reads as a contradiction.
            existing["status_note"] = None
            if entry.get("description") and not existing.get("description"):
                existing["description"] = entry["description"]
            report["filled"].append(f"{label} {pct:.0f}%")
        elif override:
            report["overrode"].append(
                f"{label} {existing['level_pct']:.0f}% -> {pct:.0f}%"
            )
            existing["level_pct"] = pct
            existing["status_note"] = None
        else:
            report["held"].append(f"{label} {existing['level_pct']:.0f}%")

    meters: Dict[str, int] = {}
    for entry in definition.get("meters") or []:
        count = decode_count(values, entry["oid"])
        name = entry["meter"]
        if count is None:
            report["dropped"].append(f"meter {name}")
            continue
        meters[name] = count

    if meters:
        snapshot = reading.get("meter_snapshot") or {}
        functions = {}
        core: Dict[str, Optional[int]] = {"total": None, "mono": None, "color": None}
        for name, count in meters.items():
            already = (
                reading.get("mono_count") if name == "mono"
                else reading.get("color_count") if name == "color"
                else snapshot.get(name)
            )
            if already is not None and not override:
                report["held"].append(f"meter {name}={already}")
                continue
            if already is not None:
                report["overrode"].append(f"meter {name} {already} -> {count}")
            else:
                report["filled"].append(f"meter {name}={count}")
            if name in defs.CORE_METERS:
                core[name] = count
            else:
                functions[name] = count
        if any(v is not None for v in core.values()) or functions:
            record_meters(
                reading,
                total=core["total"], mono=core["mono"], color=core["color"],
                functions=functions or None,
            )

    status = definition.get("status_text")
    if status:
        text = _as_text(_raw(values, status["oid"]))
        if text is None:
            report["dropped"].append("status text")
        elif reading.get("device_status_text") and not override:
            report["held"].append("status text")
        else:
            if reading.get("device_status_text"):
                report["overrode"].append("status text")
            else:
                report["filled"].append("status text")
            reading["device_status_text"] = text

    identity_spec = definition.get("identity")
    if identity_spec:
        identity = reading.setdefault("identity", {})
        brand = identity_spec.get("brand")
        if brand and not (reading.get("brand") or identity.get("brand")):
            identity["brand"] = brand
            report["filled"].append(f"brand {brand}")
        model_oid = identity_spec.get("model_oid")
        if model_oid:
            model = _as_text(_raw(values, model_oid))
            if model and not (reading.get("model") or identity.get("model")):
                identity["model"] = model
                report["filled"].append("model")

    return report


# --------------------------------------------------------------------------- #
# The provider
# --------------------------------------------------------------------------- #
class DefinitionProvider(PrinterProvider):
    """Registered LAST, so every built-in has already had its say.

    Holds no per-poll state: ``detect`` and ``augment`` both call the pure
    ``select_definition``. A cached selection on the instance would be a race,
    because one provider object serves the 16 concurrent polls in a cycle.
    """

    name = "definition"

    def detect(self, reading: dict, sys_object_id: Optional[str]) -> bool:
        # No definitions -> not entered at all, not even a trace row. This is
        # what "degrades to exactly the agent that shipped before" means.
        if not _ACTIVE:
            return False
        return select_definition(_ACTIVE, reading, sys_object_id) is not None

    async def augment(
        self,
        backend: SnmpBackend,
        ip: str,
        params: SnmpParams,
        reading: dict,
        sys_object_id: Optional[str],
    ) -> dict:
        definition = select_definition(_ACTIVE, reading, sys_object_id)
        if definition is None:
            return reading

        key = definition["key"]
        reading["_definition"] = key

        oids = _oids_of(definition)
        values: Dict[str, Optional[str]] = {}
        for start in range(0, len(oids), _GET_CHUNK):
            chunk = oids[start:start + _GET_CHUNK]
            try:
                values.update(await backend.get(ip, chunk, params))
            except SnmpError as exc:
                # A device that will not answer these OIDs is the ordinary case
                # for a definition aimed at a sibling model. Say so on the
                # reading and leave everything else alone; the standard reading
                # still ships, which is the contract every provider keeps.
                log.debug("definition %s: SNMP read failed for %s: %s", key, ip, exc)
                reading["_definition_note"] = "snmp read failed"
                return reading

        report = apply_definition(definition, values, reading)
        changes = sum(len(report[k]) for k in ("filled", "added", "overrode"))
        if changes and not reading.get("_supply_precision"):
            # Only claims precision when it actually supplied something, and
            # never displaces a built-in's claim.
            reading["_supply_precision"] = f"definition:{key}"
        summary = []
        for name in ("filled", "added", "overrode", "held", "dropped"):
            if report[name]:
                summary.append(f"{name}={len(report[name])}")
        reading["_definition_note"] = " ".join(summary) or "no change"
        if report["overrode"]:
            # The loud half of "never silently". An override is a deliberate
            # operator decision, and it is named per field both here and in the
            # trace central renders.
            log.info(
                "definition %s overrode built-in values on %s: %s",
                key, ip, "; ".join(report["overrode"]),
            )
            reading["_definition_overrode"] = "; ".join(report["overrode"])[:400]
        return reading


def _oids_of(definition: dict) -> List[str]:
    """Every OID this definition reads, de-duplicated, in a stable order."""
    oids: List[str] = []

    def add(oid: Optional[str]) -> None:
        if oid and oid not in oids:
            oids.append(oid)

    for entry in definition.get("supplies") or []:
        add(entry.get("oid"))
        add((entry.get("decode") or {}).get("max_oid"))
    for entry in definition.get("meters") or []:
        add(entry.get("oid"))
    add((definition.get("status_text") or {}).get("oid"))
    add((definition.get("identity") or {}).get("model_oid"))
    return oids


register(DefinitionProvider())
