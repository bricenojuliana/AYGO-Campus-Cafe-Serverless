import itertools
import threading
import time
from datetime import datetime, timezone

import pytest

from fakes import CURRENCY, FakeCatalog, IDEMPOTENCY_TTL_SECONDS, InMemoryOrderRepository, OWNER
from orders_service.application.service import OrderService
from orders_service.domain.errors import (CatalogUnavailable, IdempotencyConflict, MissingIdempotencyKey,
                                          OrderNotFound, ProductUnavailable, UnknownProduct)

BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]}
KEY = "demo-order-001"


def make_service(catalog=None, repository=None):
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    clock = lambda: datetime(2026, 10, 24, 14, 0, 0, tzinfo=timezone.utc)
    return OrderService(repository or InMemoryOrderRepository(), catalog or FakeCatalog(),
                        id_factory=lambda: next(ids), clock=clock, currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)


def deadline():
    return time.monotonic() + 5


def test_creates_order_matching_contract_example():
    result = make_service().create_order(KEY, BODY, deadline(), OWNER)
    assert not result.replayed
    assert result.body == {
        "orderId": "CC-0000-0001", "status": "CREATED",
        "items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "name": "Coffee", "quantity": 2, "unitPriceCents": 350}],
        "totalCents": 700, "currency": "COP",
        "createdAt": "2026-10-24T14:00:00Z", "updatedAt": "2026-10-24T14:00:00Z",
    }


def test_same_key_same_request_replays_without_calling_catalog_or_repricing():
    catalog = FakeCatalog()
    repository = InMemoryOrderRepository()
    service = make_service(catalog, repository)
    first = service.create_order(KEY, BODY, deadline(), OWNER)
    catalog.products["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = catalog.products["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"].__class__("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "Coffee", 999, "COP", True)
    second = service.create_order(KEY, {"items": [{"quantity": 2, "productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"}]}, deadline(), OWNER)
    assert second.replayed and second.order_id == first.order_id and second.body == first.body
    assert catalog.calls == 1 and len(repository.orders) == 1


def test_replay_returns_creation_result_even_after_cancellation():
    service = make_service()
    first = service.create_order(KEY, BODY, deadline(), OWNER)
    service.cancel_order(first.order_id, OWNER)
    assert service.create_order(KEY, BODY, deadline(), OWNER).body["status"] == "CREATED"
    assert service.get_order(first.order_id, OWNER).status == "CANCELLED"


def test_same_key_different_request_conflicts():
    service = make_service()
    service.create_order(KEY, BODY, deadline(), OWNER)
    with pytest.raises(IdempotencyConflict):
        service.create_order(KEY, {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 3}]}, deadline(), OWNER)


def test_new_key_creates_new_order():
    service = make_service()
    assert service.create_order(KEY, BODY, deadline(), OWNER).order_id != \
        service.create_order("demo-order-002", BODY, deadline(), OWNER).order_id


def test_concurrent_identical_submissions_persist_exactly_one_order():
    repository = InMemoryOrderRepository()
    repository.create_delay = 0.05  # widen the race window: all threads pass the pre-check first
    service = make_service(repository=repository)
    results, errors = [], []

    def submit():
        try:
            results.append(service.create_order(KEY, BODY, deadline(), OWNER))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(repository.orders) == 1
    assert len({r.order_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1


def test_catalog_failure_creates_nothing_and_key_can_be_retried():
    catalog = FakeCatalog(fail=True)
    repository = InMemoryOrderRepository()
    service = make_service(catalog, repository)
    with pytest.raises(CatalogUnavailable):
        service.create_order(KEY, BODY, deadline(), OWNER)
    assert repository.orders == {} and repository.records == {}
    catalog.fail = False
    assert not service.create_order(KEY, BODY, deadline(), OWNER).replayed


def test_product_errors_create_nothing():
    repository = InMemoryOrderRepository()
    service = make_service(repository=repository)
    with pytest.raises(UnknownProduct):
        service.create_order(KEY, {"items": [{"productId": "3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e", "quantity": 1}]}, deadline(), OWNER)
    with pytest.raises(ProductUnavailable):
        service.create_order(KEY, {"items": [{"productId": "2d5f8c3a-4e6f-4a01-9c3d-5e7f9a1b3c4d", "quantity": 1}]}, deadline(), OWNER)
    assert repository.orders == {} and repository.records == {}


def test_missing_key_is_rejected_before_anything_else():
    catalog = FakeCatalog()
    with pytest.raises(MissingIdempotencyKey):
        make_service(catalog).create_order(None, BODY, deadline(), OWNER)
    assert catalog.calls == 0


def test_cancel_is_persisted_and_repeatable():
    repository = InMemoryOrderRepository()
    service = make_service(repository=repository)
    order_id = service.create_order(KEY, BODY, deadline(), OWNER).order_id
    first = service.cancel_order(order_id, OWNER)
    second = service.cancel_order(order_id, OWNER)
    assert first.status == second.status == "CANCELLED"
    assert first == second == repository.orders[order_id]
    assert repository.orders[order_id].version == 2


def test_cancel_or_get_of_missing_order_creates_nothing():
    repository = InMemoryOrderRepository()
    service = make_service(repository=repository)
    for action in (service.cancel_order, service.get_order):
        with pytest.raises(OrderNotFound):
            action("does-not-exist", OWNER)
    with pytest.raises(OrderNotFound):
        service.get_order("../etc/passwd", OWNER)
    assert repository.orders == {}
