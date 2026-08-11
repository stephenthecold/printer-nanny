"""Location-specific supply orders and conservative compatibility matching."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager

from central import models as m


def one_line(value: Optional[str], limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalized(value: Optional[str]) -> str:
    return one_line(value, 200).casefold()


def default_delivery_date(start: Optional[date] = None, business_days: int = 5) -> date:
    """Five weekdays after ordering; weekends never consume delivery time."""
    day = start or datetime.now(timezone.utc).date()
    remaining = max(1, business_days)
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day


def signature(site_id: int, model: str, supply_type: str, color: Optional[str]) -> tuple:
    return (site_id, normalized(model), supply_type, normalized(color))


def active_orders(db: Session, client_id: Optional[int] = None) -> list[m.SupplyOrder]:
    stmt = (
        select(m.SupplyOrder)
        .join(m.SupplyOrder.site)
        .join(m.Site.client)
        .options(
            contains_eager(m.SupplyOrder.site).contains_eager(m.Site.client)
        )
        .where(m.SupplyOrder.status == m.SupplyOrderStatus.ordered)
        .order_by(
            m.SupplyOrder.estimated_delivery_at.asc().nullslast(),
            m.SupplyOrder.ordered_at.desc(),
        )
    )
    if client_id is not None:
        stmt = stmt.where(m.Site.client_id == client_id)
    return list(db.scalars(stmt))


def orders_by_signature(orders: Iterable[m.SupplyOrder]) -> dict[tuple, list[m.SupplyOrder]]:
    out: dict[tuple, list[m.SupplyOrder]] = {}
    for order in orders:
        key = signature(order.site_id, order.model, order.supply_type, order.color)
        out.setdefault(key, []).append(order)
    return out


def compatible_printers(
    db: Session, orders: Iterable[m.SupplyOrder]
) -> dict[int, list[m.Printer]]:
    """Printers proven compatible by catalogue data or exact-model fallback."""
    from central import supply_compatibility as compatibility

    order_rows = list(orders)
    if not order_rows:
        return {}
    site_ids = {order.site_id for order in order_rows}
    printers = list(
        db.scalars(
            select(m.Printer)
            .where(
                m.Printer.site_id.in_(site_ids),
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
            .order_by(m.Printer.display_name, m.Printer.model, m.Printer.ip)
        )
    )
    supply_slots: dict[int, set[tuple[str, str]]] = {}
    for supply in db.scalars(
        select(m.Supply).where(m.Supply.printer_id.in_([p.id for p in printers]))
    ):
        supply_slots.setdefault(supply.printer_id, set()).add(
            (supply.type.value, normalized(supply.color))
        )
    result: dict[int, list[m.Printer]] = {}
    products = compatibility.catalogue_products(db)
    for order in order_rows:
        product = compatibility.resolve_order_product(products, order)
        result[order.id] = [
            printer
            for printer in printers
            if printer.site_id == order.site_id
            and (order.supply_type, normalized(order.color))
            in supply_slots.get(printer.id, set())
            and (
                compatibility.product_fits(
                    product,
                    model=printer.model or "",
                    supply_type=order.supply_type,
                    color=order.color,
                )
                if product is not None
                else normalized(printer.model) == normalized(order.model)
            )
        ]
    return result
