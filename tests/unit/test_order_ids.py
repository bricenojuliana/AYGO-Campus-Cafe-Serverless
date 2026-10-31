import itertools
import time

import pytest

from fakes import CURRENCY, FakeCatalog, IDEMPOTENCY_TTL_SECONDS, InMemoryOrderRepository
from orders_service.application.service import OrderService
from orders_service.domain.errors import StoreUnavailable
from orders_service.domain.ids import ORDER_CODE_PATTERN, new_order_id, normalize_order_id

BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}]}


def test_generated_codes_are_readable_and_distinct():
    codes = {new_order_id() for _ in range(2000)}
    assert len(codes) == 2000
    assert all(ORDER_CODE_PATTERN.match(code) for code in codes)
    assert not any(ch in code[3:] for code in codes for ch in "ILOU")


@pytest.mark.parametrize("raw, expected", [
    ("CC-7K3Q-9XMD", "CC-7K3Q-9XMD"),
    ("cc-7k3q-9xmd", "CC-7K3Q-9XMD"),
    (" CC7K3Q9XMD ", "CC-7K3Q-9XMD"),
    ("CC-7K3Q-9XM0", "CC-7K3Q-9XM0"),
    ("CC-7K3Q-9XMO", "CC-7K3Q-9XM0"),   # letter O typed for zero
    ("CC-IL00-0000", "CC-1100-0000"),   # I and L typed for one
])
def test_normalization_accepts_what_people_type(raw, expected):
    assert normalize_order_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "7K3Q-9XMD", "CC-7K3Q-9XM", "CC-7K3Q-9XMDD", "XX-7K3Q-9XMD",
                                 "CC-7K3Q-9XMU", "../etc", "8cda513f-80ae-4a49-a5dd-9b491d715bc1", None])
def test_normalization_rejects_everything_else(raw):
    assert normalize_order_id(raw) == ""


def test_lookup_and_cancel_accept_the_typed_form():
    service = OrderService(InMemoryOrderRepository(), FakeCatalog(), id_factory=lambda: "CC-7K3Q-9XM0", currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)
    service.create_order("demo-order-001", BODY, time.monotonic() + 5)
    assert service.get_order("cc 7k3q 9xmo").order_id == "CC-7K3Q-9XM0"
    assert service.cancel_order("cc-7k3q-9xm0").status == "CANCELLED"


def test_code_collision_draws_a_new_code():
    repository = InMemoryOrderRepository()
    codes = iter(["CC-AAAA-AAAA", "CC-AAAA-AAAA", "CC-BBBB-BBBB"])
    service = OrderService(repository, FakeCatalog(), id_factory=lambda: next(codes), currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)
    first = service.create_order("demo-order-001", BODY, time.monotonic() + 5)
    second = service.create_order("demo-order-002", BODY, time.monotonic() + 5)
    assert (first.order_id, second.order_id) == ("CC-AAAA-AAAA", "CC-BBBB-BBBB")
    assert len(repository.orders) == 2 and len(repository.records) == 2


def test_persistent_collisions_fail_without_writing():
    repository = InMemoryOrderRepository()
    service = OrderService(repository, FakeCatalog(), id_factory=lambda: "CC-AAAA-AAAA", currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)
    service.create_order("demo-order-001", BODY, time.monotonic() + 5)
    with pytest.raises(StoreUnavailable):
        service.create_order("demo-order-002", BODY, time.monotonic() + 5)
    assert len(repository.orders) == 1 and len(repository.records) == 1
