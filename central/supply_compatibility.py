"""Conservative matching for the technician-maintained supply catalogue."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from central import models as m
from central.supply_orders import normalized, one_line

MIN_MODEL_TAG_LENGTH = 3
VALID_SUPPLY_TYPES = frozenset(item.value for item in m.SupplyType)


def product_key(manufacturer: str, sku: str) -> str:
    return f"{normalized(manufacturer)}|{normalized(sku)}"


def clean_model_tags(raw: str) -> list[str]:
    """Return unique, normalized display tags from newline/comma input."""
    seen: set[str] = set()
    tags: list[str] = []
    for candidate in raw.replace(",", "\n").splitlines():
        tag = one_line(candidate, 200)
        key = normalized(tag)
        if len(key) < MIN_MODEL_TAG_LENGTH or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def validate_product(
    *, manufacturer: str, sku: str, supply_type: str, model_tags: str
) -> Optional[str]:
    if not one_line(manufacturer, 100):
        return "Manufacturer is required."
    if not one_line(sku, 120):
        return "SKU is required."
    if supply_type not in VALID_SUPPLY_TYPES:
        return "Choose a valid supply type."
    if not clean_model_tags(model_tags):
        return "Add at least one printer model family (3 characters or longer)."
    return None


def catalogue_products(db: Session) -> list[m.SupplyProduct]:
    return list(
        db.scalars(
            select(m.SupplyProduct)
            .options(selectinload(m.SupplyProduct.model_mappings))
            .order_by(
                m.SupplyProduct.manufacturer,
                m.SupplyProduct.sku,
                m.SupplyProduct.id,
            )
        )
    )


def set_product_fields(
    product: m.SupplyProduct,
    *,
    manufacturer: str,
    sku: str,
    description: str,
    supply_type: str,
    color: str,
    is_oem: bool,
    notes: str,
    model_tags: str,
) -> None:
    """Apply already-validated form data and replace the model mapping set."""
    product.manufacturer = one_line(manufacturer, 100)
    product.sku = one_line(sku, 120)
    product.product_key = product_key(product.manufacturer, product.sku)
    product.description = one_line(description, 200)
    product.supply_type = supply_type
    product.color = one_line(color, 40)
    product.is_oem = bool(is_oem)
    product.notes = one_line(notes, 500)
    product.updated_at = datetime.now(timezone.utc)
    product.model_mappings = [
        m.SupplyProductModel(model_tag=tag, model_key=normalized(tag))
        for tag in clean_model_tags(model_tags)
    ]


def resolve_order_product(
    products: Iterable[m.SupplyProduct], order: m.SupplyOrder
) -> Optional[m.SupplyProduct]:
    """Resolve an order's SKU to one catalogue product, or refuse ambiguity."""
    sku = normalized(order.sku)
    if not sku:
        return None
    product_rows = list(products)
    manufacturer = normalized(order.manufacturer)
    if manufacturer:
        exact = [
            product
            for product in product_rows
            if product.product_key == product_key(manufacturer, sku)
        ]
        if len(exact) == 1:
            return exact[0]
        return None
    sku_matches = [
        product for product in product_rows if normalized(product.sku) == sku
    ]
    return sku_matches[0] if len(sku_matches) == 1 else None


def product_for_order(
    db: Session, order: m.SupplyOrder
) -> Optional[m.SupplyProduct]:
    return resolve_order_product(catalogue_products(db), order)


def matching_model_tag(product: m.SupplyProduct, model: str) -> Optional[str]:
    """Return the product's most-specific matching model tag."""
    model_key = normalized(model)
    matches = [
        mapping.model_tag
        for mapping in product.model_mappings
        if mapping.model_key and mapping.model_key in model_key
    ]
    if not matches:
        return None
    return max(matches, key=lambda tag: len(normalized(tag)))


def product_fits(
    product: m.SupplyProduct,
    *,
    model: str,
    supply_type: str,
    color: str,
) -> bool:
    return (
        product.supply_type == supply_type
        and normalized(product.color) == normalized(color)
        and matching_model_tag(product, model) is not None
    )


def order_fits(
    db: Session,
    order: m.SupplyOrder,
    *,
    site_id: int,
    model: str,
    supply_type: str,
    color: str,
    products: Optional[Iterable[m.SupplyProduct]] = None,
) -> bool:
    """Prove order compatibility from catalogue data, else exact-model fallback."""
    if order.site_id != site_id:
        return False
    product = resolve_order_product(
        catalogue_products(db) if products is None else products, order
    )
    if product is not None:
        return product_fits(
            product, model=model, supply_type=supply_type, color=color
        )
    return (
        normalized(order.model) == normalized(model)
        and order.supply_type == supply_type
        and normalized(order.color) == normalized(color)
    )


def model_tags_text(product: m.SupplyProduct) -> str:
    return "\n".join(
        mapping.model_tag
        for mapping in sorted(
            product.model_mappings, key=lambda item: item.model_tag.casefold()
        )
    )


def products_for_printer(
    products: Iterable[m.SupplyProduct],
    *,
    model: str,
    supply_type: str,
    color: str,
) -> list[m.SupplyProduct]:
    """Rank compatible catalogue products without claiming one was consumed."""
    matched = [
        product
        for product in products
        if product_fits(
            product, model=model, supply_type=supply_type, color=color
        )
    ]
    return sorted(
        matched,
        key=lambda product: (
            not product.is_oem,
            -len(normalized(matching_model_tag(product, model) or "")),
            product.manufacturer.casefold(),
            product.sku.casefold(),
        ),
    )
