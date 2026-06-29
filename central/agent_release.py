"""Agent-release version helpers: what version central SERVES vs. what each
agent currently REPORTS, and a correct semver-ish comparison between them.

Used by the Agents dashboard to flag outdated agents and to scope the
"update all outdated" action so we never push an update to an agent that is
already current.

The agent reports its version on heartbeat as ``"0.x.y+YYYYMMDD-HHMMSS"`` --
a SemVer base plus an install-time marker. The marker changes on every
self-update (so the operator can SEE files were replaced) but says nothing
about whether the *code* is newer; only the base before the ``+`` does. So we
compare bases, never the full string.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Fallback target if the agent package can't be imported in the central image
# (e.g. central deployed without the agent package on its PYTHONPATH). Keep
# this IN SYNC with agent/printer_nanny_agent/__init__.py __base_version__ on
# every agent-base bump. The import path below is preferred and wins whenever
# the package is importable, so this constant only matters for a central image
# that doesn't ship the agent code.
_FALLBACK_BUNDLED_AGENT_VERSION = "0.5.0"


def bundled_agent_version() -> str:
    """The agent base version this central server serves / targets for updates.

    Prefers the real package version (``printer_nanny_agent.__base_version__``)
    so it's always truthful when the agent package ships in the central image;
    falls back to a documented constant otherwise.
    """
    try:
        from printer_nanny_agent import __base_version__

        base = agent_base(__base_version__)
        if base:
            return base
    except Exception:  # noqa: BLE001 - missing/broken agent pkg -> use fallback
        pass
    return _FALLBACK_BUNDLED_AGENT_VERSION


def agent_base(reported: Optional[str]) -> str:
    """Strip the ``+YYYYMMDD-HHMMSS`` install marker, returning the SemVer base.

    ``None``/empty -> ``""`` (caller treats that as "unknown", not "current").
    """
    if not reported:
        return ""
    # Split on the first '+' (SemVer build metadata separator).
    return reported.split("+", 1)[0].strip()


def _version_tuple(base: str) -> Optional[Tuple[int, ...]]:
    """Parse a dotted-int version base into a tuple of ints for comparison.

    Tolerates a leading 'v' and trailing pre-release/garbage by reading only
    the leading run of dotted integers. Returns ``None`` if no numeric version
    can be recovered (malformed / empty).
    """
    if not base:
        return None
    base = base.strip()
    if base[:1] in ("v", "V"):
        base = base[1:]
    # Take the leading dotted-int run (e.g. "0.3.0-rc1" -> "0.3.0").
    match = re.match(r"\d+(?:\.\d+)*", base)
    if not match:
        return None
    parts = match.group(0).split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return None


def _pad(a: Tuple[int, ...], b: Tuple[int, ...]) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """Right-pad the shorter tuple with zeros so "0.3" == "0.3.0"."""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def compare_versions(reported: Optional[str], target: Optional[str]) -> Optional[int]:
    """Compare two version strings by their SemVer bases.

    Returns -1 if reported < target, 0 if equal, 1 if reported > target.
    Returns ``None`` when either side can't be parsed (unknown), so callers
    can distinguish "definitely current/outdated" from "can't tell".
    """
    rt = _version_tuple(agent_base(reported))
    tt = _version_tuple(agent_base(target))
    if rt is None or tt is None:
        return None
    ra, tb = _pad(rt, tt)
    if ra < tb:
        return -1
    if ra > tb:
        return 1
    return 0


def needs_update(reported: Optional[str], target: Optional[str]) -> bool:
    """True iff the agent's reported base is strictly OLDER than ``target``.

    An agent that never reported a version, or whose version we can't parse,
    is treated as UNKNOWN -> ``False`` here (not silently "up to date"): the
    dashboard surfaces it as an "unknown" state rather than queuing a blind
    update. A reported version newer than target (a canary/ahead agent) is
    likewise not "needs update".
    """
    cmp = compare_versions(reported, target)
    return cmp is not None and cmp < 0


def update_state(reported: Optional[str], target: Optional[str]) -> str:
    """Bucket an agent into one badge state for the dashboard.

    Returns one of: ``"unknown"`` (never reported / unparseable),
    ``"outdated"`` (reported base < target), ``"current"`` (==), or
    ``"ahead"`` (reported base > target -- a canary running newer code).
    """
    cmp = compare_versions(reported, target)
    if cmp is None:
        return "unknown"
    if cmp < 0:
        return "outdated"
    if cmp > 0:
        return "ahead"
    return "current"
