"""Derived location stock and conservative cartridge-use reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from central import models as m
from central import supply_orders as order_mod


@dataclass(frozen=True)
class StockItem:
    order: m.SupplyOrder
    used: int
    on_hand: int


@dataclass(frozen=True)
class StockGroup:
    order: m.SupplyOrder
    received: int
    used: int
    on_hand: int
    order_count: int


def _product_identity(order: m.SupplyOrder) -> tuple[str, str, str]:
    """The exact stocked product, separate from printer compatibility.

    An OEM and an aftermarket cartridge may both fit the same slot but should
    not be silently treated as interchangeable stock records. A SKU is the
    strongest identity; description is the fallback when no SKU was recorded.
    """
    sku = order_mod.normalized(order.sku)
    return (
        order_mod.normalized(order.manufacturer),
        sku,
        "" if sku else order_mod.normalized(order.description),
    )


def delivered_orders(
    db: Session, client_id: Optional[int] = None
) -> list[m.SupplyOrder]:
    stmt = (
        select(m.SupplyOrder)
        .options(joinedload(m.SupplyOrder.site).joinedload(m.Site.client))
        .where(m.SupplyOrder.status == m.SupplyOrderStatus.delivered)
        .order_by(
            m.SupplyOrder.delivered_at.asc().nullsfirst(),
            m.SupplyOrder.ordered_at.asc(),
            m.SupplyOrder.id.asc(),
        )
    )
    if client_id is not None:
        stmt = stmt.join(m.SupplyOrder.site).where(m.Site.client_id == client_id)
    return list(db.scalars(stmt).unique())


def _usage_counts(db: Session, order_ids: Iterable[int]) -> dict[int, int]:
    ids = list(order_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(m.SupplyUsage.supply_order_id, func.count(m.SupplyUsage.id))
        .where(m.SupplyUsage.supply_order_id.in_(ids))
        .group_by(m.SupplyUsage.supply_order_id)
    )
    return {int(order_id): int(count) for order_id, count in rows if order_id is not None}


def stock_items(db: Session, client_id: Optional[int] = None) -> list[StockItem]:
    orders = delivered_orders(db, client_id=client_id)
    counts = _usage_counts(db, (order.id for order in orders))
    return [
        StockItem(
            order=order,
            used=counts.get(order.id, 0),
            on_hand=max(0, order.quantity - counts.get(order.id, 0)),
        )
        for order in orders
    ]


def stock_groups(db: Session, client_id: Optional[int] = None) -> list[StockGroup]:
    grouped: dict[tuple, list[StockItem]] = {}
    for item in stock_items(db, client_id=client_id):
        order = item.order
        key = (
            order_mod.signature(
                order.site_id, order.model, order.supply_type, order.color
            ),
            _product_identity(order),
        )
        grouped.setdefault(key, []).append(item)

    result = []
    for items in grouped.values():
        result.append(
            StockGroup(
                order=items[0].order,
                received=sum(item.order.quantity for item in items),
                used=sum(item.used for item in items),
                on_hand=sum(item.on_hand for item in items),
                order_count=len(items),
            )
        )
    return sorted(
        result,
        key=lambda item: (
            item.order.site.client.name.casefold(),
            item.order.site.name.casefold(),
            item.on_hand,
            item.order.model.casefold(),
            item.order.supply_type,
            item.order.color.casefold(),
        ),
    )


def compatible_stock(
    db: Session,
    *,
    site_id: int,
    model: str,
    supply_type: str,
    color: str,
) -> list[StockItem]:
    wanted = order_mod.signature(site_id, model, supply_type, color)
    return [
        item
        for item in stock_items(db)
        if item.on_hand > 0
        and order_mod.signature(
            item.order.site_id,
            item.order.model,
            item.order.supply_type,
            item.order.color,
        )
        == wanted
    ]


def reconcile_replacements(
    db: Session,
    printer: m.Printer,
    cycles: Iterable[m.SupplyCycle],
    *,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Create one inventory event per newly closed cycle.

    Stock is automatically consumed only when every compatible positive-stock
    order describes one exact product identity. Multiple product identities are
    intentionally left ambiguous for a person to resolve.
    """
    closed = list(cycles)
    if not closed:
        return {}
    now = now or datetime.now(timezone.utc)
    db.flush()
    cycle_ids = [cycle.id for cycle in closed]
    existing = set(
        db.scalars(
            select(m.SupplyUsage.supply_cycle_id).where(
                m.SupplyUsage.supply_cycle_id.in_(cycle_ids)
            )
        )
    )
    counts = {"auto_assigned": 0, "ambiguous": 0, "no_stock": 0}
    for cycle in closed:
        if cycle.id in existing:
            continue
        candidates = compatible_stock(
            db,
            site_id=printer.site_id,
            model=printer.model or "",
            supply_type=cycle.supply_type,
            color=cycle.color,
        )
        products = {_product_identity(item.order) for item in candidates}
        chosen = candidates[0].order if len(products) == 1 and candidates else None
        if chosen is not None:
            status = m.SupplyUsageStatus.auto_assigned
        elif candidates:
            status = m.SupplyUsageStatus.ambiguous
        else:
            status = m.SupplyUsageStatus.no_stock
        db.add(
            m.SupplyUsage(
                supply_cycle_id=cycle.id,
                site_id=printer.site_id,
                printer_id=printer.id,
                supply_order_id=chosen.id if chosen is not None else None,
                status=status,
                model=order_mod.one_line(printer.model, 200) or "Unknown model",
                supply_type=order_mod.one_line(cycle.supply_type, 40),
                color=order_mod.one_line(cycle.color, 40),
                detected_at=cycle.ended_at or now,
                assigned_at=now if chosen is not None else None,
            )
        )
        # A single poll may reveal several replacements. Persist this deduction
        # before matching the next one so the same one-count order cannot be
        # consumed twice inside this transaction.
        db.flush()
        counts[status.value] += 1
    return counts


def unresolved_usages(
    db: Session, client_id: Optional[int] = None
) -> list[m.SupplyUsage]:
    stmt = (
        select(m.SupplyUsage)
        .options(
            joinedload(m.SupplyUsage.site).joinedload(m.Site.client),
            joinedload(m.SupplyUsage.printer),
        )
        .where(
            m.SupplyUsage.status.in_(
                [m.SupplyUsageStatus.no_stock, m.SupplyUsageStatus.ambiguous]
            )
        )
        .order_by(m.SupplyUsage.detected_at.desc(), m.SupplyUsage.id.desc())
    )
    if client_id is not None:
        stmt = stmt.join(m.SupplyUsage.site).where(m.Site.client_id == client_id)
    return list(db.scalars(stmt).unique())


def assignment_options(
    db: Session, usages: Iterable[m.SupplyUsage]
) -> dict[int, list[StockItem]]:
    return {
        usage.id: compatible_stock(
            db,
            site_id=usage.site_id,
            model=usage.model,
            supply_type=usage.supply_type,
            color=usage.color,
        )
        for usage in usages
    }


def assign_usage(
    db: Session,
    usage: m.SupplyUsage,
    order: m.SupplyOrder,
    *,
    user_id: int,
    now: Optional[datetime] = None,
) -> bool:
    """Assign an unresolved use if the selected order is compatible and stocked."""
    if usage.supply_order_id is not None:
        return False
    wanted = order_mod.signature(
        usage.site_id, usage.model, usage.supply_type, usage.color
    )
    offered = order_mod.signature(
        order.site_id, order.model, order.supply_type, order.color
    )
    if order.status != m.SupplyOrderStatus.delivered or offered != wanted:
        return False
    counts = _usage_counts(db, [order.id])
    if counts.get(order.id, 0) >= order.quantity:
        return False
    usage.supply_order_id = order.id
    usage.status = m.SupplyUsageStatus.manually_assigned
    usage.assigned_at = now or datetime.now(timezone.utc)
    usage.assigned_by_user_id = user_id
    return True
