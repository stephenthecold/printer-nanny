"""Durable store-and-forward spool for readings the agent could not push.

Why this exists
---------------
Readings used to be POSTed fire-and-forget: a poll cycle gathered fresh
readings, pushed them once, and if that push raised (central down, network
blip, 5xx) the cycle's readings were silently lost. For a fleet/meter/billing
pipeline that is a correctness bug -- a dropped cycle is a hole in the
append-only ``readings`` time-series that nothing ever back-fills.

This module persists any reading whose push failed to a small append-only
JSON-lines file under the agent's data dir, then replays it -- in FIFO order,
removing each line only once central acknowledges it -- the next time central
is reachable. Each reading carries its original ``ts`` (stamped at poll time
by the poller), and central's ingest only falls back to "now" when ``ts`` is
absent, so a replayed reading lands in the time-series at the moment it was
actually taken, not at replay time. Central's reading ingest is append-only by
``(printer_id, ts)``, so replaying is safe even if a reading was partially
applied before the connection dropped.

Design choices
--------------
* **JSON-lines, append-only.** One reading per line. Appending is a single
  ``write`` of bytes ending in ``\\n``; a torn final line (process killed
  mid-write) is detected and skipped on read, so we never crash on a partial
  record. No external dependency, trivially inspectable by an operator.
* **Bounded.** ``max_readings`` caps the buffer. When appending would exceed
  it we drop the *oldest* readings (FIFO) and log how many -- a long outage can
  never grow the file unbounded. Losing the oldest few is the right trade: the
  freshest readings best reflect current supply/page state.
* **Partial replay is safe.** ``drain`` sends one reading at a time; if the
  send callback raises (central went away mid-drain), everything not yet
  acknowledged stays on disk for the next attempt. No reading is removed until
  central has accepted it.
* **Best-effort, never fatal.** Spooling is a safety net; a failure to write
  the spool must not take down the agent. Every disk op is guarded and logged
  rather than raised, so the worst case degrades to today's behavior (a lost
  cycle) instead of a crash loop.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Awaitable, Callable, List

log = logging.getLogger("printer_nanny_agent.spool")

# Type of the async callback ``drain`` uses to push a batch to central. It must
# raise on any failure (connection error / non-2xx) -- that's the signal to
# stop and leave the remainder spooled.
SendFn = Callable[[List[dict]], Awaitable[object]]


class ReadingSpool:
    """A bounded, durable FIFO spool of reading payloads.

    Not safe for concurrent writers across processes (one agent process owns
    its data dir), but every method tolerates a malformed/torn file so a crash
    mid-write never wedges the agent.
    """

    def __init__(self, path: str, max_readings: int = 10000):
        self._path = path
        # Guard against a misconfigured non-positive cap turning into "keep
        # nothing" (which would silently defeat the whole feature). Anything < 1
        # is treated as 1.
        self._max = max(1, int(max_readings))

    @property
    def path(self) -> str:
        return self._path

    # -- reading -----------------------------------------------------------

    def _read_all(self) -> List[dict]:
        """Load every spooled reading in FIFO (file) order.

        A trailing torn line (process killed mid-append) or any single
        un-parseable line is skipped with a warning rather than raising -- a
        corrupt record must not block replay of the good ones.
        """
        if not os.path.exists(self._path):
            return []
        out: List[dict] = []
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Almost always the final, half-written line after an
                        # abrupt kill. Drop it; the reading it represented was
                        # never acknowledged anyway.
                        log.warning("spool: skipping unparseable line in %s", self._path)
        except OSError as exc:
            log.warning("spool: could not read %s: %s", self._path, exc)
            return []
        return out

    def count(self) -> int:
        """Number of readings currently spooled."""
        return len(self._read_all())

    def peek(self) -> List[dict]:
        """Return a copy of all spooled readings (FIFO order). For tests/inspection."""
        return self._read_all()

    # -- writing -----------------------------------------------------------

    def _rewrite(self, readings: List[dict]) -> bool:
        """Atomically replace the spool file with ``readings`` (FIFO order).

        Writes a sibling temp file then ``os.replace`` so a crash never leaves
        a half-written spool. Returns True on success.
        """
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            log.warning("spool: could not create data dir %s: %s", directory, exc)
            return False
        if not readings:
            # Empty spool -> remove the file so ``count`` is cheap and the data
            # dir stays clean in the common (everything-acked) case.
            try:
                if os.path.exists(self._path):
                    os.remove(self._path)
            except OSError as exc:
                log.warning("spool: could not remove empty spool %s: %s", self._path, exc)
            return True
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".spool-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    for reading in readings:
                        fp.write(json.dumps(reading, separators=(",", ":")))
                        fp.write("\n")
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp, self._path)
            except BaseException:
                # Clean up the temp file on any failure so we don't litter.
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("spool: could not write %s: %s", self._path, exc)
            return False
        return True

    def append(self, readings: List[dict]) -> int:
        """Persist ``readings`` to the tail of the spool, enforcing the cap.

        Returns the number of readings DROPPED to honor the cap (0 in the
        normal case). When the combined size exceeds ``max_readings`` the
        OLDEST readings are dropped and the count is logged -- the spool never
        grows past the cap.
        """
        if not readings:
            return 0
        existing = self._read_all()
        combined = existing + list(readings)
        dropped = 0
        if len(combined) > self._max:
            dropped = len(combined) - self._max
            combined = combined[dropped:]  # keep the newest ``max`` (drop oldest)
            log.warning(
                "spool: cap %d exceeded, dropped %d oldest reading(s) from %s",
                self._max, dropped, self._path,
            )
        self._rewrite(combined)
        log.info(
            "spool: buffered %d reading(s) (now %d spooled%s)",
            len(readings), len(combined),
            f", dropped {dropped} oldest" if dropped else "",
        )
        return dropped

    # -- draining (replay) -------------------------------------------------

    async def drain(self, send: SendFn, batch_size: int = 100) -> int:
        """Replay spooled readings to central in FIFO order; remove each once
        acknowledged.

        ``send`` is called with successive batches (oldest first) and MUST
        raise on any failure. The moment it raises we stop and persist whatever
        has not yet been sent, so a partial replay (central dies mid-drain)
        simply leaves the remainder spooled for next time. Returns the number
        of readings successfully replayed and removed.

        Idempotent/safe to call before every push: a no-op when the spool is
        empty.
        """
        pending = self._read_all()
        if not pending:
            return 0
        log.info("spool: replaying %d spooled reading(s) from %s", len(pending), self._path)
        sent = 0
        try:
            while sent < len(pending):
                batch = pending[sent:sent + batch_size]
                await send(batch)  # raises on connection error / non-2xx
                sent += len(batch)
        finally:
            # Persist the un-replayed remainder regardless of how we exit -- a
            # clean full drain leaves nothing; a mid-drain failure leaves the
            # readings central never acknowledged.
            remainder = pending[sent:]
            self._rewrite(remainder)
            if sent:
                log.info(
                    "spool: replayed %d reading(s); %d remain spooled",
                    sent, len(remainder),
                )
        return sent
