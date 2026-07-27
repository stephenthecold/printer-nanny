"""Throttled IPP capability probing, attached to the regular poll cycle.

``ipp.probe`` answers whether a printer needs a driver installed at all. This
decides *when* to ask.

WHY THROTTLED
-------------
A probe is several HTTP round-trips per device (it tries multiple endpoint
paths), and the answer changes only when firmware changes or somebody toggles
IPP on the device. Running it every poll cycle would multiply agent network
traffic across a whole subnet to re-learn a constant. So each IP is probed at
most once per ``interval_s`` (default 24h), tracked in memory.

In-memory rather than persisted on purpose: an agent restart re-probing once per
device is cheap and self-correcting, whereas a stale on-disk cache surviving a
firmware upgrade is a wrong answer that nothing would ever refresh. The reading
simply omits the driver fields when the probe is skipped, and central treats
absent as "no new information" rather than "unknown".

Failures never propagate. A probe that raises must not cost the SNMP reading it
rides along with -- the poll is the agent's primary job.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from printer_nanny_agent import ipp

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 24 * 60 * 60


class DriverProbeCache:
    """Decides which IPs are due a probe, and remembers when each last ran."""

    def __init__(self, interval_s: int = DEFAULT_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._last: Dict[str, float] = {}

    def due(self, ip: str, now: Optional[float] = None) -> bool:
        if self.interval_s <= 0:          # 0 or negative disables probing
            return False
        now = time.monotonic() if now is None else now
        last = self._last.get(ip)
        return last is None or (now - last) >= self.interval_s

    def mark(self, ip: str, now: Optional[float] = None) -> None:
        self._last[ip] = time.monotonic() if now is None else now

    def forget(self, ip: str) -> None:
        self._last.pop(ip, None)


def probe_fields(ip: str, timeout: float = 5.0) -> dict:
    """Probe ``ip`` and return ReadingIn-shaped driver fields.

    Returns ``{}`` when the probe itself blows up, so a bad device can never
    cost the SNMP reading this is merged into.
    """
    try:
        result = ipp.probe(ip, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a probe must never fail a poll
        log.warning("IPP probe failed for %s: %s", ip, exc)
        return {}

    fields = {
        "driver_tier": result.status,
        "driver_tier_reason": result.reason,
    }
    if result.endpoint:
        fields["ipp_endpoint"] = result.endpoint
    if result.attributes:
        fields["ipp_capabilities"] = result.as_dict()
    return fields


def attach(reading: dict, cache: DriverProbeCache, ip: str, timeout: float = 5.0) -> dict:
    """Merge driver fields into ``reading`` if ``ip`` is due a probe.

    Mutates and returns ``reading`` -- the caller already owns it.
    """
    if not cache.due(ip):
        return reading
    fields = probe_fields(ip, timeout=timeout)
    # Mark regardless of outcome: a device that refuses IPP would otherwise be
    # re-probed on every single poll, which is the cost this class exists to
    # avoid, and "it refused" is itself a durable answer worth caching.
    cache.mark(ip)
    reading.update(fields)
    return reading
