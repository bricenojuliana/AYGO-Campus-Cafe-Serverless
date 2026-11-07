"""Interfaces the Orders use cases depend on. Adapters implement them; tests may fake them."""
from typing import Iterable, Mapping, Optional, Protocol

from ..domain.order import CatalogProduct, IdempotencyRecord, Order
from ..domain.principal import Principal


class IdempotencyKeyTaken(Exception):
    """Another request committed this Idempotency-Key first."""


class OrderIdTaken(Exception):
    """The generated order code already exists; the caller should retry with a new one."""


class VersionConflict(Exception):
    """The conditional write failed: the order changed since it was read, or it is not the owner's."""


class CatalogClient(Protocol):
    def get_products(self, product_ids: Iterable[str], deadline: float) -> Mapping[str, Optional[CatalogProduct]]:
        """Returns product data keyed by id (None = unknown) or raises CatalogUnavailable.

        `deadline` is a time.monotonic() value after which no request may still be running.
        """


class OrderRepository(Protocol):
    def get_idempotency_record(self, key: str, now_epoch: int) -> Optional[IdempotencyRecord]:
        """Returns the unexpired record for `key`, if any."""

    def create_order(self, order: Order, record: IdempotencyRecord, now_epoch: int) -> None:
        """Atomically stores the order and its idempotency record.

        Raises IdempotencyKeyTaken if the key was committed first, OrderIdTaken if the code exists."""

    def get_order(self, order_id: str) -> Optional[Order]:
        ...

    def replace_order(self, order: Order, expected_version: int, owner: Principal, expected_status: str) -> None:
        """Stores `order` only if the stored order exists, still has `expected_version` and
        `expected_status`, and belongs to `owner` (issuer and subject); otherwise raises VersionConflict.
        Never creates an order."""
