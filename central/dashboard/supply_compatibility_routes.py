"""Staff UI for the manual supply compatibility catalogue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from central import models as m
from central import supply_compatibility as compatibility
from central.audit import record
from central.dashboard.manage import _flash, _manager, _pop_flash, _redirect, _tpl
from central.db import get_db

router = APIRouter(prefix="/manage/supply-compatibility", tags=["manage"])


@router.get("", response_class=HTMLResponse)
def catalogue(request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    return _tpl(
        request,
        "supply_compatibility.html",
        db,
        user=user,
        products=compatibility.catalogue_products(db),
        supply_types=sorted(compatibility.VALID_SUPPLY_TYPES),
        model_tags_text=compatibility.model_tags_text,
        flash=_pop_flash(request),
    )


def _save(
    *,
    request: Request,
    db: Session,
    user: m.User,
    product: m.SupplyProduct,
    manufacturer: str,
    sku: str,
    description: str,
    supply_type: str,
    color: str,
    is_oem: bool,
    notes: str,
    model_tags: str,
    is_new: bool,
):
    error = compatibility.validate_product(
        manufacturer=manufacturer,
        sku=sku,
        supply_type=supply_type,
        model_tags=model_tags,
    )
    if error:
        _flash(request, error)
        return _redirect("/manage/supply-compatibility")

    compatibility.set_product_fields(
        product,
        manufacturer=manufacturer,
        sku=sku,
        description=description,
        supply_type=supply_type,
        color=color,
        is_oem=is_oem,
        notes=notes,
        model_tags=model_tags,
    )
    if is_new:
        db.add(product)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        _flash(request, "That manufacturer and SKU already exist in the catalogue.")
        return _redirect("/manage/supply-compatibility")
    record(
        db,
        request,
        user,
        "supply_compatibility.create" if is_new else "supply_compatibility.update",
        target=f"supply_product:{product.id}",
        detail=(
            f"manufacturer={product.manufacturer!r}; sku={product.sku!r}; "
            f"slot={product.supply_type}/{product.color or 'unspecified'}; "
            f"models={len(product.model_mappings)}"
        ),
    )
    db.commit()
    _flash(request, "Compatibility product saved.")
    return _redirect("/manage/supply-compatibility")


@router.post("")
def create_product(
    request: Request,
    manufacturer: str = Form(""),
    sku: str = Form(""),
    description: str = Form(""),
    supply_type: str = Form(""),
    color: str = Form(""),
    is_oem: bool = Form(False),
    notes: str = Form(""),
    model_tags: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    return _save(
        request=request,
        db=db,
        user=user,
        product=m.SupplyProduct(),
        manufacturer=manufacturer,
        sku=sku,
        description=description,
        supply_type=supply_type,
        color=color,
        is_oem=is_oem,
        notes=notes,
        model_tags=model_tags,
        is_new=True,
    )


@router.post("/{product_id}")
def update_product(
    product_id: int,
    request: Request,
    manufacturer: str = Form(""),
    sku: str = Form(""),
    description: str = Form(""),
    supply_type: str = Form(""),
    color: str = Form(""),
    is_oem: bool = Form(False),
    notes: str = Form(""),
    model_tags: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    product = db.get(m.SupplyProduct, product_id)
    if product is None:
        _flash(request, "That compatibility product no longer exists.")
        return _redirect("/manage/supply-compatibility")
    # Ensure deletes are issued before replacement mappings are inserted, so a
    # tag that remains on the edited product cannot hit the unique constraint.
    product.model_mappings.clear()
    db.flush()
    return _save(
        request=request,
        db=db,
        user=user,
        product=product,
        manufacturer=manufacturer,
        sku=sku,
        description=description,
        supply_type=supply_type,
        color=color,
        is_oem=is_oem,
        notes=notes,
        model_tags=model_tags,
        is_new=False,
    )


@router.post("/{product_id}/delete")
def delete_product(
    product_id: int, request: Request, db: Session = Depends(get_db)
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    product = db.get(m.SupplyProduct, product_id)
    if product is None:
        _flash(request, "That compatibility product was already removed.")
        return _redirect("/manage/supply-compatibility")
    target = f"supply_product:{product.id}"
    record(db, request, user, "supply_compatibility.delete", target=target)
    db.delete(product)
    db.commit()
    _flash(request, "Compatibility product removed. Existing order history is unchanged.")
    return _redirect("/manage/supply-compatibility")
