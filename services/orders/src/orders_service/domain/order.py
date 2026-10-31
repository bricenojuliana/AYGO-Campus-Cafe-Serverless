"""Order model and rules: pricing from Catalog data, ownership and cancellation. No AWS or HTTP here."""
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

from .errors import CatalogUnavailable, InvalidRequest, ProductUnavailable, UnknownProduct
from .principal import Principal

STATUS_CREATED = "CREATED"
STATUS_CANCELLED = "CANCELLED"
CANCELLABLE_STATUSES = frozenset({STATUS_CREATED})


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
    # Who created the order: the verified (iss, sub) of the token. Stored by the server, never taken from
    # the request and never returned by the API. None only for legacy orders created before authentication.
    owner_issuer: Optional[str] = None
    owner_subject: Optional[str] = None

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
    key: str                         # the caller-scoped storage key, not the raw Idempotency-Key header
    fingerprint: str                 # hash of the normalized request it was first used with
    order_id: str
    response_body: Mapping[str, Any]  # the original creation response, returned on replay
    expires_at: int
    owner_issuer: str                # who sent it; a replay is answered only to the same principal
    owner_subject: str

    def belongs_to(self, caller: Principal) -> bool:
        return (self.owner_issuer, self.owner_subject) == (caller.issuer, caller.subject)


def build_order(order_id: str, lines: tuple[OrderLine, ...],
                products: Mapping[str, Optional[CatalogProduct]], now_iso: str, *,
                currency: str, owner: Principal) -> Order:
    """Prices the order from Catalog data only; client input never carries prices.

    `currency` is the application's single currency (deployment setting); Catalog prices in any
    other currency are rejected instead of being mixed into one total."""
    items = []
    for line in lines:
        product = products.get(line.product_id)
        if product is None:
            raise UnknownProduct(line.product_id)
        if not product.available:
            raise ProductUnavailable(line.product_id)
        if product.currency != currency or product.price_cents < 0:
            raise CatalogUnavailable("Catalog returned a price this application cannot accept.")
        items.append(OrderItem(product.product_id, product.name, line.quantity, product.price_cents))
    total = sum(item.unit_price_cents * item.quantity for item in items)
    return Order(order_id, STATUS_CREATED, tuple(items), total, currency, now_iso, now_iso,
                 owner_issuer=owner.issuer, owner_subject=owner.subject)


def can_access(caller: Principal, order: Order) -> bool:
    """Ownership rule: only the principal that created the order may read or cancel it.

    Both parts must match: a subject is unique only within its issuer. Legacy orders without an owner
    are accessible to nobody (they are not deleted)."""
    return (order.owner_issuer is not None and order.owner_subject is not None
            and order.owner_issuer == caller.issuer and order.owner_subject == caller.subject)


def cancelled(order: Order, now_iso: str) -> Order:
    """Returns the order after cancellation. Cancelling a cancelled order is a no-op."""
    if order.status == STATUS_CANCELLED:
        return order
    if order.status not in CANCELLABLE_STATUSES:
        raise InvalidRequest(f"An order in status {order.status} cannot be cancelled.")
    return replace(order, status=STATUS_CANCELLED, updated_at=now_iso, version=order.version + 1)
