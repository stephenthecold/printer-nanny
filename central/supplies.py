"""What a supply's ``level_pct`` actually means, in one place.

RFC 3805 gives every marker supply a *class* (``prtMarkerSuppliesClass``) and
that class reverses the reading of the level column:

  ``supplyThatIsConsumed(3)``     level == how much REMAINS. 5% is nearly dead.
  ``receptacleThatIsFilled(4)``   level == how FULL it is.   5% is nearly empty,
                                  which for a waste box is the healthiest it
                                  ever gets.

Nothing in this codebase distinguished the two. ``queries.low_supplies``, the
overview and portal "Low supplies" panels, ``level_bar`` and the reorder page
all read ``level_pct`` as "remaining", so a freshly emptied waste container
reporting 5 was rendered as a red bar, counted as a low supply, and recommended
for reorder with the reason "level 5% is at or below the 5% order-now level" --
about a part that had just been serviced. The inverse error is the one that
costs money: a waste box at 95% is about to stop the printer and was reported as
comfortably full.

WHY THE FALLBACK IS BY TYPE, AND WHY THAT MAKES OLD ROWS SAFE
-------------------------------------------------------------
``supply_class`` is NULL on every row written before this shipped, and on rows
from devices that omit the column. The fallback is the supply *type*: our
``waste`` type is only ever produced from ``wasteToner(4)`` or from the
description keywords "waste" / "toner collection", and every one of those IS a
receptacle. So a stored row is reinterpreted with no data migration and no
rewrite of ``level_pct``.

That is sound because **no stored number changes meaning**. A device reporting a
receptacle has always been reporting fullness; ``parse_supply_level`` has always
stored what the device said. Only our *reading* of it was wrong. There is
therefore nothing to backfill and nothing that can be half-migrated -- the fix
applies to history and to new readings identically, which is why it is a read
rule and not a migration.

What the class column buys on top of the type fallback is the cases the type
cannot reach: a hole-punch chip box, a staple waste bin, a separator-pad
catcher. Those come back with a generic type and an explicit
``receptacleThatIsFilled``, and only the device can tell us.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from central import models as m
from central.snmp_parse import (
    SUPPLY_CLASS_CONSUMED,
    SUPPLY_CLASS_OTHER,
    SUPPLY_CLASS_RECEPTACLE,
)

__all__ = [
    "REFILL_TOLERANCE_PCT",
    "SUPPLY_CLASS_CONSUMED",
    "SUPPLY_CLASS_OTHER",
    "SUPPLY_CLASS_RECEPTACLE",
    "is_receptacle",
    "level_label",
    "receptacle_supply_types",
    "refill_boundaries",
    "remaining_pct",
]

#: How far a level has to CLIMB before we call it a fresh cartridge rather than
#: measurement noise. Devices round, some report in 5% or 10% buckets, and a
#: toner cartridge shaken by a passing technician genuinely reads a point or two
#: higher for a poll or two. 5 points is the value the depletion forecast has
#: always used; it lives here now so the yield measurement cannot pick a
#: different one -- see ``refill_boundaries``.
REFILL_TOLERANCE_PCT = 5.0


def refill_boundaries(
    levels: "Sequence[Optional[float]]",
    refill_tolerance: float = REFILL_TOLERANCE_PCT,
) -> "List[int]":
    """Indices at which a fresh cartridge was fitted, in a level series.

    ``levels`` is one supply slot's ``level_pct`` readings in observation order.
    An index ``i`` is returned when ``levels[i]`` is more than
    ``refill_tolerance`` points ABOVE ``levels[i-1]``: consumables do not refill
    themselves, so a rise that large is somebody putting a new cartridge in.
    This is LibreNMS's cheap trick, and it is the only cartridge-change signal
    this system has -- devices do not report "replaced".

    **One rule, two callers, deliberately.** The depletion forecast
    (``worker.jobs._fit_depleting_segment``) needs the LAST boundary, so it does
    not average a spent cartridge's slope against its replacement's;
    ``central.supply_yield`` needs EVERY boundary, because the interval between
    two of them is exactly the interval whose pages it divides. Those must never
    be able to disagree about what a replacement is -- a forecast that has reset
    its baseline while the yield measurement is still accumulating pages from the
    previous cartridge would produce a number that is wrong in a way nothing
    reports.

    ``None`` levels are skipped rather than treated as a value: a poll that
    reported no level is silence, and a level of ``None`` between 4% and 100%
    must not read as two separate steps. Skipping means the comparison is always
    against the last level actually observed.
    """
    out: List[int] = []
    prev: Optional[float] = None
    for i, level in enumerate(levels):
        if level is None:
            continue
        if prev is not None and level > prev + refill_tolerance:
            out.append(i)
        prev = level
    return out

# Supply types that ARE receptacles when the device did not say. See the module
# docstring: every path that produces ``waste`` produces it from a container.
_RECEPTACLE_TYPES = frozenset({m.SupplyType.waste})


def receptacle_supply_types() -> frozenset:
    """The type fallback, for the SQL twin of ``is_receptacle`` to reuse.

    Exported so ``queries.receptacle_supply_clause`` cannot drift from this
    module by spelling the set a second time -- the two must agree, or a row
    counts as low in one place and full in another.
    """
    return _RECEPTACLE_TYPES


def is_receptacle(supply: m.Supply) -> bool:
    """True when this supply's level means "how full", not "how much is left".

    The device's own class wins when it reported one -- including an explicit
    ``consumed`` on a row our keyword matcher called waste, because the device
    knows its own hardware and we were guessing from a name.
    """
    cls = (supply.supply_class or "").strip().lower()
    if cls == SUPPLY_CLASS_RECEPTACLE:
        return True
    if cls == SUPPLY_CLASS_CONSUMED:
        return False
    # NULL (not reported) or "other" (reported, but the device declines to say):
    # fall back to what the supply is.
    return supply.type in receptacle_supply_types()


def remaining_pct(supply: m.Supply) -> Optional[float]:
    """Headroom left before this supply needs attention, 0-100.

    For a cartridge that is the level itself. For a receptacle it is the space
    left in the container (100 - fullness), so a single number is comparable
    across both and "lower is worse" holds everywhere.

    ``None`` stays ``None``: a supply with no reported level has no headroom
    estimate, and inventing 100 would report an unread device as healthy.
    """
    if supply.level_pct is None:
        return None
    if is_receptacle(supply):
        return max(0.0, min(100.0, 100.0 - supply.level_pct))
    return supply.level_pct


def level_label(supply: m.Supply) -> str:
    """The word that makes a rendered percentage unambiguous: "full" / "left"."""
    return "full" if is_receptacle(supply) else "left"
