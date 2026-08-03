"""Device/model definitions -- the agent's copy of the validator.

VENDORED, DELIBERATELY. This is a byte-for-byte mirror of
``central/device_definitions.py`` below the header, held to it by
``tests/test_device_definitions_parity.py`` -- the same arrangement
``snmp_parse`` uses, and for the same reason: the agent is a standalone package
that cannot import ``central``, and the two ends must agree exactly about what
a definition means or a definition that passed on ingest is refused at the
device (or worse, the reverse).

The agent runs this validator on EVERY definition it receives and on EVERY load
of its local cache. That is not belt-and-braces: central signs the feed, and a
signature proves who produced bytes, not that the bytes are safe. An agent must
be able to defend itself against a central it authenticates, and against its
own cache file.

Everything below is the shared contract.

Device/model definitions: what central serves to agents so a new printer
model does not need an agent release.

WHAT A DEFINITION IS, AND WHAT IT DELIBERATELY IS NOT
-----------------------------------------------------
A definition is **data**. It names OIDs to read and states, in a closed
vocabulary, how to turn each answer into the supply / meter / status fields the
reading already has. It cannot describe *behaviour*.

That is the whole security posture of the feature, and it is enforced by
construction rather than by review:

* **No code.** Nothing here is ``eval``'d, ``exec``'d, imported, compiled, or
  passed to a shell. The agent's only interpreter for a definition is the
  ``decode`` dispatch below, which is a fixed set of six kinds.
* **No regular expressions, ever.** A pattern supplied by a definition is a
  denial-of-service on every agent in the fleet the moment one of them is
  catastrophically backtracking, and there is no way to inspect an arbitrary
  pattern and prove it is not. Extraction from a status string is therefore
  done with **literal delimiters** (``text_between``), which is linear by
  construction and covers the observed need ("Toner: 47%"). A definition
  carrying ``regex``/``pattern``/``re`` is refused with that reason named, so
  the refusal reads as a decision rather than a typo. This is the same stance
  the workstation client takes with PowerShell: make the question moot instead
  of handling it.
* **Everything is bounded**, at both ends: payload bytes, definition count,
  OIDs per definition, map entries, string lengths, JSON nesting depth. An
  unbounded field is a memory or CPU budget handed to whoever can write a row.
* **Unknown keys are refused**, at every level. That is what makes the "no
  regex" rule enforceable without enumerating every bad idea: anything not in
  the vocabulary simply does not parse.

VALIDATE ON BOTH SIDES
----------------------
This module is validated on ingest (an operator's form post) *and* vendored
into the agent (``printer_nanny_agent.definitions``) so the agent validates
again on receipt and on every load of its local cache. Central signing the feed
does not remove that need -- a signature says who produced the bytes, not that
the bytes are safe, and the agent must be able to defend itself against a
central it authenticates. ``tests/test_device_definitions_parity.py`` holds the
two copies to identical behaviour, the same arrangement ``snmp_parse`` uses.

SIGNING, STATED HONESTLY
------------------------
``payload_signature`` is an HMAC-SHA256 over the canonical JSON of the feed,
keyed on the agent's own API-key hash -- a value central stores and the agent
can derive from the key it holds. It is a **channel binding and cache-integrity
tag**, not a supply-chain signature: it proves the payload was produced for
*this* agent by something that knows its credential, which is what makes a
cache file lifted from another agent, or a feed replayed at a different agent,
fail closed. It is deliberately not claimed to be more than that -- a real
supply-chain signature needs an offline key the agent can verify without
holding, and shipping such a key inside the agent would make the claim false.
The defence that does the work is the validation above, which runs on every
definition whether or not the signature verified.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Bounds. Every one of these is a budget somebody else can spend, so each is a
# number rather than a "reasonable" absence.
# --------------------------------------------------------------------------- #
#: Whole-feed cap, checked on the agent against the raw response bytes BEFORE
#: any parse -- a JSON bomb must be refused by its size, not by its shape.
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_DEFINITIONS = 200
#: Nesting depth of a definition. Checked iteratively (never by recursing over
#: attacker-shaped input, which is how a depth check becomes the DoS).
MAX_JSON_DEPTH = 8
MAX_OIDS_PER_DEFINITION = 32
MAX_SUPPLIES = 24
MAX_METERS = 12
MAX_MAP_ENTRIES = 64
MAX_TEXT = 200
MAX_OID_LEN = 255
MAX_OID_ARCS = 64
MAX_DELIMITER = 32
MAX_KEY_LEN = 64
#: A match tag shorter than this is a substring of most model strings, so it
#: would claim a whole fleet. Same number and same reason as
#: ``services.MIN_DRIVER_MODEL_TAG``.
MIN_MATCH_TAG = 3

#: Must stay identical to ``central.models.SupplyType``. Asserted by a test
#: rather than imported, because the agent copy cannot import central.
SUPPLY_TYPES = (
    "toner", "ink", "drum", "fuser", "waste", "staples", "developer", "other",
)
#: Meters ``record_meters`` understands natively; anything else is recorded as
#: a per-function count in the meter snapshot.
CORE_METERS = ("total", "mono", "color")
DECODE_KINDS = ("percent", "ratio", "map", "count", "text", "text_between")

#: Keys whose presence anywhere in a definition is refused *by name*, so the
#: log line says why rather than "unknown key". These are the shapes somebody
#: reaches for when they want the definition language to be a programming
#: language.
_FORBIDDEN_KEYS = ("regex", "pattern", "re", "expr", "eval", "exec", "script", "code")


class DefinitionError(ValueError):
    """A definition (or a whole feed) is not valid. Message is operator-facing."""


# --------------------------------------------------------------------------- #
# Primitive checks
# --------------------------------------------------------------------------- #
def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    # bool is an int in Python and would sail through every numeric check.
    return isinstance(value, int) and not isinstance(value, bool)


def check_depth(obj: Any, limit: int = MAX_JSON_DEPTH) -> None:
    """Refuse a structure nested deeper than ``limit``.

    Iterative on purpose. A recursive depth check over hostile input hits
    Python's own recursion limit first and raises ``RecursionError`` from
    somewhere unrelated -- turning the guard into the thing that fails.
    """
    stack: List[Tuple[Any, int]] = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            raise DefinitionError(f"nested deeper than {limit} levels")
        if isinstance(node, dict):
            for key, val in node.items():
                if not _is_str(key):
                    raise DefinitionError("object keys must be strings")
                stack.append((val, depth + 1))
        elif isinstance(node, list):
            for val in node:
                stack.append((val, depth + 1))


def _reject_forbidden(node: Any) -> None:
    """Walk the structure and refuse the named-and-shamed keys with a reason.

    ``_only_keys`` already refuses anything unrecognised; this exists so the
    message an operator sees for the *expected* wrong idea says why rather than
    "unknown field". A ``values`` object is skipped deliberately: its keys are
    device response strings being matched literally, not field names, and
    refusing a sentinel a printer happens to emit would be a false positive on
    data we do not control.
    """
    stack: List[Any] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, val in item.items():
                if _is_str(key) and key.strip().lower() in _FORBIDDEN_KEYS:
                    raise DefinitionError(
                        f"{key!r} is not part of the definition language: a "
                        "pattern supplied by a definition is a denial-of-service "
                        "on every agent that loads it. Use text_between with "
                        "literal delimiters."
                    )
                if key == "values" and isinstance(val, dict):
                    continue
                stack.append(val)
        elif isinstance(item, list):
            stack.extend(item)


def _only_keys(obj: Any, allowed: Tuple[str, ...], where: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise DefinitionError(f"{where} must be an object")
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise DefinitionError(f"{where}: unknown field(s) {', '.join(extra)}")
    return obj


def _text(obj: Dict[str, Any], field: str, where: str, *, required: bool = False,
          maxlen: int = MAX_TEXT) -> Optional[str]:
    raw = obj.get(field)
    if raw is None or raw == "":
        if required:
            raise DefinitionError(f"{where}: {field} is required")
        return None
    if not _is_str(raw):
        raise DefinitionError(f"{where}: {field} must be text")
    text = raw.strip()
    if not text:
        if required:
            raise DefinitionError(f"{where}: {field} is required")
        return None
    if len(text) > maxlen:
        raise DefinitionError(f"{where}: {field} is longer than {maxlen} characters")
    return text


def is_valid_oid(value: str) -> bool:
    """True for a dotted numeric OID.

    Written as a character loop rather than a regular expression. That is not
    fastidiousness: this function's whole job is to be the thing that cannot be
    made to backtrack, and it is the gate that keeps ``"1.3.6; rm -rf /"`` out
    of a value that is handed to an SNMP library.
    """
    if not value or len(value) > MAX_OID_LEN:
        return False
    arcs = 0
    digits_in_arc = 0
    for ch in value:
        if ch == ".":
            if digits_in_arc == 0:
                return False  # empty arc: leading dot, "1..2", trailing dot
            arcs += 1
            digits_in_arc = 0
            if arcs > MAX_OID_ARCS:
                return False
        elif "0" <= ch <= "9":
            digits_in_arc += 1
            if digits_in_arc > 20:  # an arc wider than a 64-bit integer
                return False
        else:
            return False
    return digits_in_arc > 0


def _oid(obj: Dict[str, Any], field: str, where: str, *, required: bool = True) -> Optional[str]:
    raw = obj.get(field)
    if raw is None or raw == "":
        if required:
            raise DefinitionError(f"{where}: {field} is required")
        return None
    if not _is_str(raw):
        raise DefinitionError(f"{where}: {field} must be text")
    oid = raw.strip().lstrip(".")
    if not is_valid_oid(oid):
        raise DefinitionError(
            f"{where}: {field} {raw!r} is not a dotted numeric OID"
        )
    return oid


def _slug(value: Any, where: str) -> str:
    """A stable identifier: lowercase, ASCII, no surprises in a URL or a log."""
    if not _is_str(value):
        raise DefinitionError(f"{where} must be text")
    text = value.strip().lower()
    if not (MIN_MATCH_TAG <= len(text) <= MAX_KEY_LEN):
        raise DefinitionError(
            f"{where} must be {MIN_MATCH_TAG}-{MAX_KEY_LEN} characters"
        )
    if not (text[0].isalnum() and text[0].isascii()):
        raise DefinitionError(f"{where} must start with a letter or digit")
    for ch in text:
        if not (ch.isascii() and (ch.isalnum() or ch in "._-")):
            raise DefinitionError(
                f"{where} may contain only letters, digits, dot, underscore and hyphen"
            )
    return text


def _tag(value: Any, where: str) -> str:
    """A free-text match tag (brand / model substring), lowercased and bounded."""
    if not _is_str(value):
        raise DefinitionError(f"{where} must be text")
    text = " ".join(value.split()).lower()
    if len(text) < MIN_MATCH_TAG:
        raise DefinitionError(
            f"{where} must be at least {MIN_MATCH_TAG} characters -- a shorter "
            "tag is a substring of most model strings and would claim the fleet"
        )
    if len(text) > MAX_TEXT:
        raise DefinitionError(f"{where} is longer than {MAX_TEXT} characters")
    return text


def _label(value: Any, where: str, maxlen: int = 64) -> str:
    """A short display token (a colour, a meter function name).

    Charset-restricted rather than merely length-capped: these land in log
    lines, in JSON keys of ``meter_snapshot``, and in the dashboard.
    """
    if not _is_str(value):
        raise DefinitionError(f"{where} must be text")
    text = " ".join(value.split()).lower()
    if not text or len(text) > maxlen:
        raise DefinitionError(f"{where} must be 1-{maxlen} characters")
    for ch in text:
        if not (ch.isascii() and (ch.isalnum() or ch in " -_")):
            raise DefinitionError(f"{where} may contain only letters, digits, space, - and _")
    return text


# --------------------------------------------------------------------------- #
# The decode vocabulary -- the entire "language" a definition may speak
# --------------------------------------------------------------------------- #
def _validate_decode(raw: Any, where: str, *, allow: Tuple[str, ...]) -> Dict[str, Any]:
    obj = _only_keys(raw, ("kind", "max", "max_oid", "values", "after", "before"), where)
    kind = obj.get("kind")
    if not _is_str(kind) or kind not in DECODE_KINDS:
        raise DefinitionError(
            f"{where}: kind must be one of {', '.join(DECODE_KINDS)}"
        )
    if kind not in allow:
        raise DefinitionError(f"{where}: kind {kind!r} is not allowed here")

    out: Dict[str, Any] = {"kind": kind}

    if kind == "ratio":
        # Either a literal denominator or a second OID to read it from --
        # never both, because two sources for one number is a silent tie.
        has_max, has_oid = "max" in obj, "max_oid" in obj
        if has_max == has_oid:
            raise DefinitionError(f"{where}: ratio needs exactly one of max / max_oid")
        if has_max:
            if not _is_int(obj["max"]) or not (0 < obj["max"] <= 2**31 - 1):
                raise DefinitionError(f"{where}: ratio max must be a positive integer")
            out["max"] = int(obj["max"])
        else:
            out["max_oid"] = _oid(obj, "max_oid", where)
    elif "max" in obj or "max_oid" in obj:
        raise DefinitionError(f"{where}: max / max_oid only apply to kind 'ratio'")

    if kind == "map":
        values = obj.get("values")
        if not isinstance(values, dict) or not values:
            raise DefinitionError(f"{where}: map needs a non-empty values object")
        if len(values) > MAX_MAP_ENTRIES:
            raise DefinitionError(f"{where}: map has more than {MAX_MAP_ENTRIES} entries")
        mapped: Dict[str, int] = {}
        for raw_key, raw_val in values.items():
            if not _is_str(raw_key) or not raw_key or len(raw_key) > MAX_KEY_LEN:
                raise DefinitionError(f"{where}: map keys must be 1-{MAX_KEY_LEN} characters")
            if not _is_int(raw_val) or not (0 <= raw_val <= 100):
                raise DefinitionError(f"{where}: map values must be integers 0-100")
            mapped[raw_key.strip()] = int(raw_val)
        out["values"] = mapped
    elif "values" in obj:
        raise DefinitionError(f"{where}: values only applies to kind 'map'")

    if kind == "text_between":
        # Literal delimiters. This is the regex replacement, and the reason
        # there is no regex: slicing between two fixed strings is linear in the
        # input no matter what an operator types.
        after = _text(obj, "after", where, maxlen=MAX_DELIMITER)
        before = _text(obj, "before", where, maxlen=MAX_DELIMITER)
        if after is None and before is None:
            raise DefinitionError(f"{where}: text_between needs 'after' and/or 'before'")
        if after is not None:
            out["after"] = after
        if before is not None:
            out["before"] = before
    elif "after" in obj or "before" in obj:
        raise DefinitionError(f"{where}: after / before only apply to kind 'text_between'")

    return out


def _validate_supply(raw: Any, index: int) -> Dict[str, Any]:
    where = f"supplies[{index}]"
    obj = _only_keys(raw, ("oid", "type", "color", "description", "decode"), where)
    supply_type = _text(obj, "type", where, required=True, maxlen=32)
    if supply_type.lower() not in SUPPLY_TYPES:
        raise DefinitionError(
            f"{where}: type must be one of {', '.join(SUPPLY_TYPES)}"
        )
    out: Dict[str, Any] = {
        "oid": _oid(obj, "oid", where),
        "type": supply_type.lower(),
        "decode": _validate_decode(
            obj.get("decode", {"kind": "percent"}), f"{where}.decode",
            allow=("percent", "ratio", "map", "text_between"),
        ),
    }
    if obj.get("color") not in (None, ""):
        out["color"] = _label(obj["color"], f"{where}.color", maxlen=32)
    description = _text(obj, "description", where)
    if description:
        out["description"] = description
    return out


def _validate_meter(raw: Any, index: int) -> Dict[str, Any]:
    where = f"meters[{index}]"
    obj = _only_keys(raw, ("oid", "meter", "decode"), where)
    meter = _label(obj.get("meter"), f"{where}.meter", maxlen=32)
    return {
        "oid": _oid(obj, "oid", where),
        "meter": meter,
        "decode": _validate_decode(
            obj.get("decode", {"kind": "count"}), f"{where}.decode", allow=("count",)
        ),
    }


def _validate_match(raw: Any) -> Dict[str, str]:
    obj = _only_keys(raw if raw is not None else {}, ("enterprise", "brand", "model"), "match")
    out: Dict[str, str] = {}
    if obj.get("enterprise") not in (None, ""):
        enterprise = str(obj["enterprise"]).strip().lstrip(".")
        if not is_valid_oid(enterprise):
            raise DefinitionError(
                "match.enterprise must be an enterprise number or dotted prefix "
                "(e.g. '2435' or '11.2.3.9')"
            )
        out["enterprise"] = enterprise
    if obj.get("brand") not in (None, ""):
        out["brand"] = _tag(obj["brand"], "match.brand")
    if obj.get("model") not in (None, ""):
        out["model"] = _tag(obj["model"], "match.model")
    if not out:
        # A definition with no criteria matches every printer in every fleet,
        # which is how a Brother private-MIB read gets pointed at an HP. There
        # is no legitimate "applies to everything" definition: the whole point
        # is that a definition describes one device family.
        raise DefinitionError(
            "a definition needs at least one match criterion (enterprise, brand "
            "or model) -- one that matches everything would be applied to every "
            "printer in every client"
        )
    return out


def validate_definition(raw: Any) -> Dict[str, Any]:
    """Validate + normalise one definition. Raises :class:`DefinitionError`.

    The returned dict is canonical: lowercased tags, stripped OIDs, defaults
    made explicit, and nothing present that was not understood. Both ends store
    and hash *this*, never the operator's original text, so the two sides
    cannot disagree about what a definition says.
    """
    if not isinstance(raw, dict):
        raise DefinitionError("a definition must be an object")
    check_depth(raw)
    _reject_forbidden(raw)
    obj = _only_keys(
        raw,
        ("key", "match", "supplies", "meters", "status_text", "identity",
         "override_builtin", "notes"),
        "definition",
    )

    out: Dict[str, Any] = {
        "key": _slug(obj.get("key"), "key"),
        "match": _validate_match(obj.get("match")),
        "override_builtin": bool(obj.get("override_builtin", False)),
    }

    supplies_raw = obj.get("supplies") or []
    if not isinstance(supplies_raw, list):
        raise DefinitionError("supplies must be a list")
    if len(supplies_raw) > MAX_SUPPLIES:
        raise DefinitionError(f"more than {MAX_SUPPLIES} supplies")
    supplies = [_validate_supply(s, i) for i, s in enumerate(supplies_raw)]

    meters_raw = obj.get("meters") or []
    if not isinstance(meters_raw, list):
        raise DefinitionError("meters must be a list")
    if len(meters_raw) > MAX_METERS:
        raise DefinitionError(f"more than {MAX_METERS} meters")
    meters = [_validate_meter(mtr, i) for i, mtr in enumerate(meters_raw)]

    status_text = None
    if obj.get("status_text") not in (None, {}, ""):
        st = _only_keys(obj["status_text"], ("oid",), "status_text")
        status_text = {"oid": _oid(st, "oid", "status_text")}

    identity = None
    if obj.get("identity") not in (None, {}, ""):
        ident = _only_keys(obj["identity"], ("brand", "model_oid"), "identity")
        identity = {}
        brand = _text(ident, "brand", "identity", maxlen=64)
        if brand:
            identity["brand"] = brand
        model_oid = _oid(ident, "model_oid", "identity", required=False)
        if model_oid:
            identity["model_oid"] = model_oid
        if not identity:
            identity = None

    if not (supplies or meters or status_text or identity):
        # A definition that reads nothing is a row that costs an SNMP round
        # trip's worth of nothing and looks, in the UI, like it is working.
        raise DefinitionError(
            "a definition must read something: supplies, meters, status_text or identity"
        )

    oids = {s["oid"] for s in supplies} | {mt["oid"] for mt in meters}
    for supply in supplies:
        if "max_oid" in supply["decode"]:
            oids.add(supply["decode"]["max_oid"])
    if status_text:
        oids.add(status_text["oid"])
    if identity and "model_oid" in identity:
        oids.add(identity["model_oid"])
    if len(oids) > MAX_OIDS_PER_DEFINITION:
        raise DefinitionError(
            f"a definition may read at most {MAX_OIDS_PER_DEFINITION} distinct OIDs"
        )

    if supplies:
        out["supplies"] = supplies
    if meters:
        out["meters"] = meters
    if status_text:
        out["status_text"] = status_text
    if identity:
        out["identity"] = identity
    notes = _text(obj, "notes", "definition", maxlen=MAX_TEXT)
    if notes:
        out["notes"] = notes
    return out


def validate_feed(definitions: Any) -> List[Dict[str, Any]]:
    """Validate a whole feed. Refuses duplicate keys as well as bad rows."""
    if not isinstance(definitions, list):
        raise DefinitionError("definitions must be a list")
    if len(definitions) > MAX_DEFINITIONS:
        raise DefinitionError(f"more than {MAX_DEFINITIONS} definitions")
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in definitions:
        row = validate_definition(raw)
        if row["key"] in seen:
            raise DefinitionError(f"duplicate definition key {row['key']!r}")
        seen.add(row["key"])
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Canonical form, version digest, signature
# --------------------------------------------------------------------------- #
def canonical_json(obj: Any) -> str:
    """Deterministic JSON. Sorted keys, no whitespace, ASCII-escaped.

    Both ends compute this from the *parsed* object rather than trusting the
    bytes on the wire, so a proxy that re-serialises the response does not
    invalidate the signature -- and a payload whose ordering was shuffled to
    hide a field still hashes the same as what was actually parsed.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def feed_version(definitions: List[Dict[str, Any]]) -> str:
    """A content digest of the feed, used as its version.

    A digest rather than a counter, because the agent only ever compares for
    equality and a counter has to survive deletion of its own high-water row.
    Truncated to 16 hex characters: this is a change detector, not a
    security boundary (the signature is), and it travels in a query string.
    """
    payload = canonical_json(sorted(definitions, key=lambda d: d["key"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def payload_signature(api_key_hash: str, version: str, definitions: List[Dict[str, Any]]) -> str:
    """HMAC-SHA256 binding this feed to this agent's credential.

    See the module docstring for exactly what this does and does not prove.
    """
    message = canonical_json({"version": version, "definitions": definitions})
    return hmac.new(
        (api_key_hash or "").encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def signature_ok(api_key_hash: str, version: str, definitions: Any, signature: Any) -> bool:
    """Constant-time check of a received feed's signature."""
    if not _is_str(signature) or not isinstance(definitions, list):
        return False
    try:
        expected = payload_signature(api_key_hash, version, definitions)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)
