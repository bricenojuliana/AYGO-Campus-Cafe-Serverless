"""Orders use cases. Coordinates domain rules with the repository and Catalog ports."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from .auth import ANONYMOUS, Caller
from .domain import (IdempotencyConflict, IdempotencyRecord, Order, OrderNotFound, StoreUnavailable,
                     build_order, can_access, cancelled, parse_create_request,
                     request_fingerprint, validate_idempotency_key)
from .ids import normalize_order_id
from .ports import CatalogClient, IdempotencyKeyTaken, OrderIdTaken, OrderRepository, VersionConflict

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
CANCEL_ATTEMPTS = 3
ORDER_ID_ATTEMPTS = 3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CreateOrderResult:
    order_id: str
    body: Mapping[str, Any]
    replayed: bool


class OrderService:
    def __init__(self, repository: OrderRepository, catalog: CatalogClient,
                 id_factory: Callable[[], str], clock: Callable[[], datetime] = utc_now):
        self._repository = repository
        self._catalog = catalog
        self._new_id = id_factory
        self._clock = clock

    def create_order(self, idempotency_key: Optional[str], body: Any, deadline: float,
                     caller: Caller = ANONYMOUS) -> CreateOrderResult:
        key = validate_idempotency_key(idempotency_key)
        if caller.subject is not None:
            key = f"{caller.subject}|{key}"  # with authentication, keys are scoped per caller
        lines = parse_create_request(body)
        fingerprint = request_fingerprint(lines)
        now = self._clock()

        existing = self._repository.get_idempotency_record(key, int(now.timestamp()))
        if existing is not None:
            return self._replay(existing, fingerprint)

        products = self._catalog.get_products([line.product_id for line in lines], deadline)
        for _ in range(ORDER_ID_ATTEMPTS):
            order = build_order(self._new_id(), lines, products, _iso(now), owner_id=caller.subject)
            body_to_store = order.to_api()
            record = IdempotencyRecord(key, fingerprint, order.order_id, body_to_store,
                                       int(now.timestamp()) + IDEMPOTENCY_TTL_SECONDS)
            try:
                self._repository.create_order(order, record, int(now.timestamp()))
            except OrderIdTaken:
                continue  # 40-bit code collided with an existing order; draw a new code
            except IdempotencyKeyTaken:
                # A concurrent request with the same key committed first; answer as that request did.
                winner = self._repository.get_idempotency_record(key, int(now.timestamp()))
                if winner is None:
                    raise StoreUnavailable()
                return self._replay(winner, fingerprint)
            return CreateOrderResult(order.order_id, body_to_store, replayed=False)
        raise StoreUnavailable()

    def get_order(self, order_id: str, caller: Caller = ANONYMOUS) -> Order:
        canonical = normalize_order_id(order_id)
        order = self._repository.get_order(canonical) if canonical else None
        # A foreign order is reported as absent so its existence is not revealed.
        if order is None or not can_access(caller.subject, order):
            raise OrderNotFound()
        return order

    def cancel_order(self, order_id: str, caller: Caller = ANONYMOUS) -> Order:
        for _ in range(CANCEL_ATTEMPTS):
            current = self.get_order(order_id, caller)  # normalizes the code
            updated = cancelled(current, _iso(self._clock()))
            if updated is current:
                return current
            try:
                self._repository.replace_order(updated, expected_version=current.version)
                return updated
            except VersionConflict:
                continue
        raise StoreUnavailable()

    @staticmethod
    def _replay(record: IdempotencyRecord, fingerprint: str) -> CreateOrderResult:
        if record.fingerprint != fingerprint:
            raise IdempotencyConflict()
        return CreateOrderResult(record.order_id, record.response_body, replayed=True)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
