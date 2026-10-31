"""Order business rules. No AWS and no API Gateway types in this module."""
import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

CURRENCY = "COP"
STATUS_CREATED = "CREATED"
STATUS_CANCELLED = "CANCELLED"
CANCELLABLE_STATUSES = frozenset({STATUS_CREATED})

MAX_DISTINCT_PRODUCTS = 10
MIN_QUANTITY, MAX_QUANTITY = 1, 10
PRODUCT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # canonical lowercase UUID
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


class OrderError(Exception):
    """Base for business errors; `code` and `message` are part of the public API."""

    code = "ORDER_ERROR"
    message = "The order could not be processed."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.message)
        self.message = message or self.message


class Unauthenticated(OrderError):
    code = "UNAUTHENTICATED"
    message = "Sign in to use orders."


class InvalidRequest(OrderError):
    code = "INVALID_REQUEST"


class MissingIdempotencyKey(OrderError):
    code = "MISSING_IDEMPOTENCY_KEY"
    message = "The Idempotency-Key header is required (8-128 chars of letters, digits, '_', '.', ':' or '-')."


class IdempotencyConflict(OrderError):
    code = "IDEMPOTENCY_KEY_CONFLICT"
    message = "This Idempotency-Key was already used with a different request."


class ProductUnavailable(OrderError):
    code = "PRODUCT_UNAVAILABLE"
    message = "This product cannot be ordered."

    def __init__(self, product_id: str):
        super().__init__(f"This product cannot be ordered: {product_id}.")
        self.product_id = product_id


class UnknownProduct(OrderError):
    code = "UNKNOWN_PRODUCT"
    message = "This product does not exist."

    def __init__(self, product_id: str):
        super().__init__(f"This product does not exist: {product_id}.")
        self.product_id = product_id


class OrderNotFound(OrderError):
    code = "ORDER_NOT_FOUND"
    message = "This order does not exist."


class CatalogUnavailable(OrderError):
    code = "CATALOG_UNAVAILABLE"
    message = "Product information is temporarily unavailable. Retry with the same Idempotency-Key."


class StoreUnavailable(OrderError):
    code = "STORE_UNAVAILABLE"
    message = "Orders are temporarily unavailable. Retry with the same Idempotency-Key."


@dataclass(frozen=True)
class OrderLine:
    product_id: str
    quantity: int


@dataclass(frozen=True)
class CatalogProduct:
    """What Orders needs to know about a product, as reported by Catalog at order time."""

    product_id: str
    name: str
    price_cents: int
    currency: str
    available: bool


@dataclass(frozen=True)
class OrderItem:
    product_id: str
    name: str
    quantity: int
    unit_price_cents: int

    def to_api(self) -> dict:
        return {"productId": self.product_id, "name": self.name,
                "quantity": self.quantity, "unitPriceCents": self.unit_price_cents}


@dataclass(frozen=True)
class Order:
    order_id: str
    status: str
    items: tuple[OrderItem, ...]
    total_cents: int
    currency: str
    created_at: str
    updated_at: str
    version: int = 1
    owner_id: Optional[str] = None  # JWT subject of the creator; None in the anonymous workshop baseline

    def to_api(self) -> dict:
        return {
            "orderId": self.order_id,
            "status": self.status,
            "items": [item.to_api() for item in self.items],
            "totalCents": self.total_cents,
            "currency": self.currency,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    fingerprint: str
    order_id: str
    response_body: Mapping[str, Any]
    expires_at: int


def validate_idempotency_key(key: Optional[str]) -> str:
    if not key or not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise MissingIdempotencyKey()
    return key


def parse_create_request(body: Any) -> tuple[OrderLine, ...]:
    """Validates the POST /orders body and returns its lines sorted by productId."""
    if not isinstance(body, dict):
        raise InvalidRequest("The request body must be a JSON object.")
    unknown = set(body) - {"items"}
    if unknown:
        raise InvalidRequest(f"Unknown fields: {', '.join(sorted(unknown))}.")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise InvalidRequest("items must be a non-empty array.")
    if len(items) > MAX_DISTINCT_PRODUCTS:
        raise InvalidRequest(f"An order can contain at most {MAX_DISTINCT_PRODUCTS} products.")

    lines: dict[str, OrderLine] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"productId", "quantity"}:
            raise InvalidRequest(f"items[{index}] must have exactly productId and quantity.")
        product_id, quantity = item["productId"], item["quantity"]
        if not isinstance(product_id, str) or not PRODUCT_ID_PATTERN.match(product_id):
            raise InvalidRequest(f"items[{index}].productId must be a lowercase UUID.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) \
                or not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
            raise InvalidRequest(f"items[{index}].quantity must be an integer from {MIN_QUANTITY} to {MAX_QUANTITY}.")
        if product_id in lines:
            raise InvalidRequest(f"productId '{product_id}' appears more than once.")
        lines[product_id] = OrderLine(product_id, quantity)
    return tuple(sorted(lines.values(), key=lambda line: line.product_id))


def request_fingerprint(lines: tuple[OrderLine, ...]) -> str:
    """Stable hash of the normalized request; equal requests produce equal fingerprints."""
    canonical = json.dumps([[line.product_id, line.quantity] for line in lines], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_order(order_id: str, lines: tuple[OrderLine, ...],
                products: Mapping[str, Optional[CatalogProduct]], now_iso: str,
                owner_id: Optional[str] = None) -> Order:
    """Prices the order from Catalog data only; client input never carries prices."""
    items = []
    for line in lines:
        product = products.get(line.product_id)
        if product is None:
            raise UnknownProduct(line.product_id)
        if not product.available:
            raise ProductUnavailable(line.product_id)
        if product.currency != CURRENCY or product.price_cents < 0:
            raise CatalogUnavailable("Catalog returned a price this application cannot accept.")
        items.append(OrderItem(product.product_id, product.name, line.quantity, product.price_cents))
    total = sum(item.unit_price_cents * item.quantity for item in items)
    return Order(order_id, STATUS_CREATED, tuple(items), total, CURRENCY, now_iso, now_iso, owner_id=owner_id)


def can_access(caller_subject: Optional[str], order: Order) -> bool:
    """Ownership rule. Orders created without an authenticated caller (the workshop baseline) are
    shared demo orders; an order with an owner is visible only to that owner."""
    return order.owner_id is None or order.owner_id == caller_subject


def cancelled(order: Order, now_iso: str) -> Order:
    """Returns the order after cancellation. Cancelling a cancelled order is a no-op."""
    if order.status == STATUS_CANCELLED:
        return order
    if order.status not in CANCELLABLE_STATUSES:
        raise InvalidRequest(f"An order in status {order.status} cannot be cancelled.")
    return replace(order, status=STATUS_CANCELLED, updated_at=now_iso, version=order.version + 1)
