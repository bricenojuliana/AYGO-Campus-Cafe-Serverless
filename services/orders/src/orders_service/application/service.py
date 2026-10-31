"""Orders use cases. Coordinates domain rules with the repository and Catalog ports."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from ..domain.errors import IdempotencyConflict, OrderNotFound, StoreUnavailable
from ..domain.ids import normalize_order_id
from ..domain.order import IdempotencyRecord, Order, build_order, can_access, cancelled
from ..domain.principal import Principal
from ..domain.requests import parse_create_request, request_fingerprint, validate_idempotency_key
from .ports import CatalogClient, IdempotencyKeyTaken, OrderIdTaken, OrderRepository, VersionConflict

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
                 id_factory: Callable[[], str], *, currency: str, idempotency_ttl_seconds: int,
                 clock: Callable[[], datetime] = utc_now):
        self._repository = repository
        self._catalog = catalog
        self._new_id = id_factory
        self._currency = currency
        self._idempotency_ttl = idempotency_ttl_seconds  # how long a retry with the same key is remembered
        self._clock = clock

    def create_order(self, idempotency_key: Optional[str], body: Any, deadline: float,
                     caller: Principal) -> CreateOrderResult:
        key = validate_idempotency_key(idempotency_key)
        key = f"{caller.subject}|{key}"  # keys are scoped per caller (made collision-proof in the next step)
        lines = parse_create_request(body)
        fingerprint = request_fingerprint(lines)
        now = self._clock()

        existing = self._repository.get_idempotency_record(key, int(now.timestamp()))
        if existing is not None:
            return self._replay(existing, fingerprint)

        products = self._catalog.get_products([line.product_id for line in lines], deadline)
        for _ in range(ORDER_ID_ATTEMPTS):
            order = build_order(self._new_id(), lines, products, _iso(now), currency=self._currency,
                                owner=caller)
            body_to_store = order.to_api()
            record = IdempotencyRecord(key, fingerprint, order.order_id, body_to_store,
                                       int(now.timestamp()) + self._idempotency_ttl)
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

    def get_order(self, order_id: str, caller: Principal) -> Order:
        canonical = normalize_order_id(order_id)
        order = self._repository.get_order(canonical) if canonical else None
        # Missing, foreign and legacy ownerless orders all raise the same error, so a caller cannot
        # tell whether an order code exists.
        if order is None or not can_access(caller, order):
            raise OrderNotFound()
        return order

    def cancel_order(self, order_id: str, caller: Principal) -> Order:
        for _ in range(CANCEL_ATTEMPTS):
            current = self.get_order(order_id, caller)  # normalizes the code
            updated = cancelled(current, _iso(self._clock()))
            if updated is current:
                return current
            try:
                # The write is conditional on the stored owner too, not only on the check above.
                self._repository.replace_order(updated, expected_version=current.version, owner=caller)
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
