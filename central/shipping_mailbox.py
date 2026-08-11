"""Microsoft 365 shared-mailbox ingestion for supply shipping notices."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Optional
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from central import models as m
from central import supply_orders as order_mod
from central.audit import record

_LOGIN = "https://login.microsoftonline.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = "https://graph.microsoft.com/.default"
_TIMEOUT = 30.0
_MAX_PAGES = 10
_MAX_MESSAGES = 500
_MARKER = "shipping.last_poll_at"


class ShippingMailboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedNotice:
    source_message_id: str
    internet_message_id: str
    sender: str
    vendor: str
    subject: str
    item_description: str
    sku: str
    quantity: Optional[int]
    ship_to: str
    tracking_number: str
    estimated_delivery_at: Optional[date]
    received_at: datetime


class _TextExtractor(HTMLParser):
    _BLOCKS = frozenset(
        {"br", "p", "div", "li", "tr", "td", "th", "table", "h1", "h2", "h3"}
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _message_text(body: dict) -> str:
    content = str((body or {}).get("content") or "")[:200_000]
    if str((body or {}).get("contentType") or "").casefold() != "html":
        return content
    parser = _TextExtractor()
    try:
        parser.feed(content)
    except Exception:  # noqa: BLE001 -- malformed supplier HTML becomes plain text
        return content
    return "".join(parser.parts)


def _lines(text: str) -> list[str]:
    return [_one_line(line, 500) for line in text.splitlines() if line.strip()]


def _label_value(lines: list[str], labels: Iterable[str], limit: int) -> str:
    wanted = tuple(
        sorted((label.casefold() for label in labels), key=len, reverse=True)
    )
    for index, line in enumerate(lines):
        lower = line.casefold()
        for label in wanted:
            match = re.match(rf"^{re.escape(label)}\s*[:#-]?\s*(.*)$", lower)
            if not match:
                continue
            # Slice the original line so casing and punctuation survive.
            value = line[match.start(1):].strip(" :-")
            if value:
                return _one_line(value, limit)
            if index + 1 < len(lines):
                return _one_line(lines[index + 1], limit)
    return ""


def _ship_to(lines: list[str]) -> str:
    labels = ("ship to", "shipping address", "delivery address")
    for index, line in enumerate(lines):
        lower = line.casefold()
        found = next((label for label in labels if lower.startswith(label)), None)
        if found is None:
            continue
        first = line[len(found):].strip(" :-")
        parts = [first] if first else []
        for following in lines[index + 1:index + 4]:
            if re.match(
                r"^(tracking|estimated|delivery date|item|product|sku|part|qty|quantity)\b",
                following,
                flags=re.IGNORECASE,
            ):
                break
            parts.append(following)
        return _one_line(", ".join(parts), 500)
    return ""


def _parse_date(text: str) -> Optional[date]:
    patterns = (
        (r"\b(20\d{2}-\d{1,2}-\d{1,2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2}/\d{1,2}/20\d{2})\b", "%m/%d/%Y"),
        (
            r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
            r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+20\d{2})\b",
            None,
        ),
    )
    for pattern, fmt in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        formats = [fmt] if fmt else ["%B %d %Y", "%b %d %Y"]
        for candidate in formats:
            try:
                return datetime.strptime(raw, candidate).date()
            except ValueError:
                continue
    return None


def _received_at(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_message(message: dict) -> Optional[ParsedNotice]:
    """Extract only operational shipping fields; never return/store raw content."""
    source_id = _one_line(message.get("id"), 512)
    if not source_id:
        return None
    subject = _one_line(message.get("subject"), 500)
    text = _message_text(message.get("body") or {})
    lines = _lines(text)
    joined = "\n".join(lines)
    tracking = _label_value(
        lines, ("tracking", "tracking number", "tracking #"), 120
    )
    eta_text = _label_value(
        lines,
        ("estimated delivery", "estimated delivery date", "delivery date", "eta"),
        120,
    )
    eta = _parse_date(eta_text) or _parse_date(
        " ".join(line for line in lines if "deliver" in line.casefold())
    )
    shipping_signal = re.search(
        r"\b(shipped|shipping|tracking|out for delivery|estimated delivery)\b",
        f"{subject}\n{joined}",
        flags=re.IGNORECASE,
    )
    if not shipping_signal and not tracking and eta is None:
        return None

    sender_info = ((message.get("from") or {}).get("emailAddress") or {})
    sender = _one_line(sender_info.get("address"), 320)
    vendor = _one_line(sender_info.get("name"), 160) or (
        sender.split("@", 1)[1] if "@" in sender else sender
    )
    sku = _label_value(lines, ("sku", "part number", "part #", "item number"), 120)
    quantity_raw = _label_value(lines, ("quantity", "qty"), 20)
    quantity_match = re.search(r"\d+", quantity_raw)
    quantity = int(quantity_match.group()) if quantity_match else None
    if quantity is not None and not 1 <= quantity <= 10_000:
        quantity = None
    item = _label_value(lines, ("item", "product", "description"), 500)
    if not item:
        item = subject
    return ParsedNotice(
        source_message_id=source_id,
        internet_message_id=_one_line(message.get("internetMessageId"), 512),
        sender=sender,
        vendor=vendor,
        subject=subject,
        item_description=item,
        sku=sku,
        quantity=quantity,
        ship_to=_ship_to(lines),
        tracking_number=tracking,
        estimated_delivery_at=eta,
        received_at=_received_at(message.get("receivedDateTime")),
    )


def _allowed(sender: str, configured: Any) -> bool:
    domains = {
        item.strip().casefold().lstrip("@")
        for item in str(configured or "").split(",")
        if item.strip()
    }
    if not domains:
        return True
    domain = sender.rsplit("@", 1)[-1].casefold() if "@" in sender else ""
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in domains)


def _normalized_location(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def match_site(db: Session, parsed: ParsedNotice) -> Optional[m.Site]:
    haystack = _normalized_location(parsed.ship_to)
    if not haystack:
        return None
    sites = list(
        db.scalars(select(m.Site).options(joinedload(m.Site.client))).unique()
    )
    address_matches = [
        site
        for site in sites
        if len(_normalized_location(site.address)) >= 8
        and _normalized_location(site.address) in haystack
    ]
    if len(address_matches) == 1:
        return address_matches[0]
    named = [
        site
        for site in sites
        if _normalized_location(site.client.name) in haystack
        and _normalized_location(site.name) in haystack
    ]
    return named[0] if len(named) == 1 else None


def _matching_order(
    db: Session, site: Optional[m.Site], parsed: ParsedNotice
) -> Optional[m.SupplyOrder]:
    if site is None:
        return None
    orders = order_mod.active_orders(db)
    candidates = [order for order in orders if order.site_id == site.id]
    sku = order_mod.normalized(parsed.sku)
    if sku:
        exact = [order for order in candidates if order_mod.normalized(order.sku) == sku]
        return exact[0] if len(exact) == 1 else None
    item = order_mod.normalized(parsed.item_description)
    identified = [
        order
        for order in candidates
        if order_mod.normalized(order.sku)
        and order_mod.normalized(order.sku) in item
    ]
    return identified[0] if len(identified) == 1 else None


def _safe_next_link(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "graph.microsoft.com"


def _token(http: httpx.Client, settings: dict) -> str:
    tenant = quote(str(settings.get("shipping.tenant_id") or "").strip(), safe="")
    try:
        response = http.post(
            f"{_LOGIN}/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(settings.get("shipping.client_id") or "").strip(),
                "client_secret": str(settings.get("shipping.client_secret") or ""),
                "scope": _SCOPE,
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ShippingMailboxError(
            f"cannot reach Microsoft login: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise ShippingMailboxError(
            f"Microsoft token request failed (HTTP {response.status_code})"
        )
    token = (response.json() or {}).get("access_token")
    if not token:
        raise ShippingMailboxError("Microsoft returned no access token")
    return str(token)


def _messages(
    http: httpx.Client, settings: dict, since: datetime
) -> list[dict]:
    token = _token(http, settings)
    mailbox = quote(str(settings.get("shipping.mailbox") or "").strip(), safe="")
    url = f"{_GRAPH}/users/{mailbox}/mailFolders/inbox/messages"
    params: Optional[dict] = {
        "$select": (
            "id,internetMessageId,receivedDateTime,subject,from,body"
        ),
        "$filter": f"receivedDateTime ge {since.astimezone(timezone.utc).isoformat()}",
        "$orderby": "receivedDateTime desc",
        "$top": "50",
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.body-content-type="text"',
    }
    found: list[dict] = []
    for _page in range(_MAX_PAGES):
        try:
            response = http.get(
                url, params=params, headers=headers, timeout=_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise ShippingMailboxError(
                f"cannot reach Microsoft Graph: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise ShippingMailboxError(
                f"Microsoft Graph request failed (HTTP {response.status_code})"
            )
        body = response.json() or {}
        found.extend(item for item in body.get("value") or [] if isinstance(item, dict))
        if len(found) >= _MAX_MESSAGES:
            return found[:_MAX_MESSAGES]
        next_link = str(body.get("@odata.nextLink") or "")
        if not next_link:
            return found
        if not _safe_next_link(next_link):
            raise ShippingMailboxError("Microsoft Graph returned an unsafe next page URL")
        url = next_link
        params = None
    raise ShippingMailboxError("Microsoft Graph mailbox result exceeded the paging limit")


def sync_mailbox(
    db: Session,
    *,
    settings: Optional[dict] = None,
    client: Optional[httpx.Client] = None,
    now: Optional[datetime] = None,
) -> dict:
    from central.runtime import load_settings

    values = settings or load_settings(db)
    if not values.get("shipping.enabled", False):
        return {"shipping_mailbox": "disabled"}
    required = ("shipping.tenant_id", "shipping.client_id", "shipping.client_secret", "shipping.mailbox")
    missing = [key for key in required if not str(values.get(key) or "").strip()]
    if missing:
        return {"shipping_mailbox": {"error": "configuration incomplete"}}
    now = now or datetime.now(timezone.utc)
    marker = db.get(m.AppSetting, _MARKER)
    interval = max(1, int(values.get("shipping.poll_interval_min") or 30))
    last = None
    if marker is not None and marker.value:
        try:
            last = datetime.fromisoformat(marker.value)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            last = None
    if last is not None and last > now - timedelta(minutes=interval):
        return {}
    lookback = min(90, max(1, int(values.get("shipping.initial_lookback_days") or 14)))
    since = (last - timedelta(minutes=5)) if last is not None else (now - timedelta(days=lookback))

    http = client or httpx.Client()
    close = client is None
    try:
        messages = _messages(http, values, since)
    finally:
        if close:
            http.close()

    existing = set(
        db.scalars(
            select(m.ShippingNotice.source_message_id).where(
                m.ShippingNotice.source_message_id.in_(
                    [_one_line(item.get("id"), 512) for item in messages]
                )
            )
        )
    ) if messages else set()
    imported = linked = ignored = 0
    mailbox = _one_line(values.get("shipping.mailbox"), 320)
    for message in messages:
        parsed = parse_message(message)
        if parsed is None or parsed.source_message_id in existing:
            ignored += 1
            continue
        if not _allowed(parsed.sender, values.get("shipping.allowed_senders")):
            ignored += 1
            continue
        site = match_site(db, parsed)
        order = _matching_order(db, site, parsed)
        notice = m.ShippingNotice(
            source_message_id=parsed.source_message_id,
            internet_message_id=parsed.internet_message_id,
            mailbox=mailbox,
            sender=parsed.sender,
            subject=parsed.subject,
            vendor=parsed.vendor,
            item_description=parsed.item_description,
            sku=parsed.sku,
            quantity=parsed.quantity,
            ship_to=parsed.ship_to,
            tracking_number=parsed.tracking_number,
            estimated_delivery_at=parsed.estimated_delivery_at,
            received_at=parsed.received_at,
            site_id=site.id if site is not None else None,
            supply_order_id=order.id if order is not None else None,
        )
        db.add(notice)
        db.flush()
        if order is not None:
            linked += 1
            if parsed.estimated_delivery_at is not None:
                order.estimated_delivery_at = parsed.estimated_delivery_at
        record(
            db,
            None,
            None,
            "shipping_notice.import",
            target=(
                f"shipping_notice:{notice.id} site:{notice.site_id or 'unmatched'} "
                f"supply_order:{notice.supply_order_id or 'unmatched'}"
            ),
        )
        imported += 1
    if marker is None:
        db.add(m.AppSetting(key=_MARKER, value=now.isoformat()))
    else:
        marker.value = now.isoformat()
    db.commit()
    return {
        "shipping_mailbox": {
            "messages": len(messages),
            "imported": imported,
            "linked": linked,
            "needs_review": imported - linked,
            "ignored": ignored,
        }
    }


def pending_notices(db: Session, client_id: Optional[int] = None) -> list[m.ShippingNotice]:
    stmt = (
        select(m.ShippingNotice)
        .options(joinedload(m.ShippingNotice.site).joinedload(m.Site.client))
        .where(m.ShippingNotice.supply_order_id.is_(None))
        .order_by(m.ShippingNotice.received_at.desc(), m.ShippingNotice.id.desc())
    )
    if client_id is not None:
        stmt = stmt.where(m.ShippingNotice.site_id.in_(select(m.Site.id).where(m.Site.client_id == client_id)))
    return list(db.scalars(stmt).unique())


def assignment_options(
    db: Session, notices: Iterable[m.ShippingNotice], client_id: Optional[int] = None
) -> dict[int, list[m.SupplyOrder]]:
    active = order_mod.active_orders(db, client_id=client_id)
    return {
        notice.id: [
            order for order in active
            if notice.site_id is None or order.site_id == notice.site_id
        ]
        for notice in notices
    }


def shipping_by_order(
    db: Session, order_ids: Iterable[int]
) -> dict[int, list[m.ShippingNotice]]:
    ids = list(order_ids)
    if not ids:
        return {}
    result: dict[int, list[m.ShippingNotice]] = {}
    for notice in db.scalars(
        select(m.ShippingNotice)
        .where(m.ShippingNotice.supply_order_id.in_(ids))
        .order_by(m.ShippingNotice.received_at.desc())
    ):
        result.setdefault(notice.supply_order_id, []).append(notice)
    return result


def assign_notice(
    notice: m.ShippingNotice, order: m.SupplyOrder
) -> bool:
    if notice.supply_order_id is not None or order.status != m.SupplyOrderStatus.ordered:
        return False
    notice.site_id = order.site_id
    notice.supply_order_id = order.id
    notice.updated_at = datetime.now(timezone.utc)
    if notice.estimated_delivery_at is not None:
        order.estimated_delivery_at = notice.estimated_delivery_at
    return True


def assume_due_delivered(db: Session, *, today: Optional[date] = None) -> int:
    due = today or datetime.now(timezone.utc).date()
    rows = list(
        db.scalars(
            select(m.ShippingNotice)
            .join(m.ShippingNotice.supply_order)
            .where(
                m.ShippingNotice.estimated_delivery_at.is_not(None),
                m.ShippingNotice.estimated_delivery_at <= due,
                m.SupplyOrder.status == m.SupplyOrderStatus.ordered,
            )
            .order_by(m.ShippingNotice.estimated_delivery_at, m.ShippingNotice.id)
        )
    )
    delivered_orders: set[int] = set()
    for notice in rows:
        order = notice.supply_order
        if order is None or order.id in delivered_orders:
            continue
        order.status = m.SupplyOrderStatus.delivered
        order.delivered_at = datetime.now(timezone.utc)
        delivered_orders.add(order.id)
        record(
            db,
            None,
            None,
            "supply_order.assumed_delivered",
            target=f"supply_order:{order.id} shipping_notice:{notice.id} site:{order.site_id}",
            detail=f"estimated_delivery_at={notice.estimated_delivery_at.isoformat()}",
        )
    if delivered_orders:
        db.commit()
    return len(delivered_orders)
