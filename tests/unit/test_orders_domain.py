import pytest

from fakes import CURRENCY, PRODUCTS
from orders_service.domain.errors import (CatalogUnavailable, InvalidRequest, MissingIdempotencyKey,
                                          ProductUnavailable, UnknownProduct)
from orders_service.domain.order import CatalogProduct, OrderLine, build_order, cancelled
from orders_service.domain.requests import parse_create_request, request_fingerprint, validate_idempotency_key

NOW = "2026-10-24T14:00:00Z"


def test_parses_and_sorts_lines():
    lines = parse_create_request({"items": [{"productId": "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "quantity": 1},
                                            {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]})
    assert lines == (OrderLine("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", 2), OrderLine("1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", 1))


@pytest.mark.parametrize("body", [
    None, [], "x", {}, {"items": []}, {"items": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 0}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 11}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1.5}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": True}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": "2"}]},
    {"items": [{"productId": "Coffee!", "quantity": 1}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1, "priceCents": 1}]},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}], "totalCents": 1},
    {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}, {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]},
    {"items": [{"productId": f"00000000-0000-4000-8000-{i:012d}", "quantity": 1} for i in range(11)]},
])
def test_rejects_invalid_requests(body):
    with pytest.raises(InvalidRequest):
        parse_create_request(body)


def test_accepts_boundaries():
    assert len(parse_create_request({"items": [{"productId": f"00000000-0000-4000-8000-{i:012d}", "quantity": 10} for i in range(10)]})) == 10


@pytest.mark.parametrize("key", [None, "", "short", "has space here", "x" * 129])
def test_rejects_bad_idempotency_keys(key):
    with pytest.raises(MissingIdempotencyKey):
        validate_idempotency_key(key)


def test_fingerprint_ignores_line_order_but_not_quantities():
    a = parse_create_request({"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}, {"productId": "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "quantity": 1}]})
    b = parse_create_request({"items": [{"productId": "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "quantity": 1}, {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]})
    c = parse_create_request({"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 3}, {"productId": "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "quantity": 1}]})
    assert request_fingerprint(a) == request_fingerprint(b) != request_fingerprint(c)


def test_build_order_prices_from_catalog_snapshot():
    order = build_order("CC-0000-0001", (OrderLine("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", 2), OrderLine("1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", 1)), PRODUCTS, NOW, currency=CURRENCY)
    assert order.to_api() == {
        "orderId": "CC-0000-0001", "status": "CREATED",
        "items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "name": "Coffee", "quantity": 2, "unitPriceCents": 350},
                  {"productId": "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "name": "Croissant", "quantity": 1, "unitPriceCents": 275}],
        "totalCents": 975, "currency": "COP", "createdAt": NOW, "updatedAt": NOW,
    }


def test_unknown_and_unavailable_products():
    with pytest.raises(UnknownProduct):
        build_order("CC-0000-0001", (OrderLine("3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e", 1),), PRODUCTS, NOW, currency=CURRENCY)
    with pytest.raises(ProductUnavailable):
        build_order("CC-0000-0001", (OrderLine("2d5f8c3a-4e6f-4a01-9c3d-5e7f9a1b3c4d", 1),), PRODUCTS, NOW, currency=CURRENCY)


def test_foreign_currency_from_catalog_is_a_dependency_failure():
    products = {"0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b": CatalogProduct("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "Coffee", 350, "EUR", True)}
    with pytest.raises(CatalogUnavailable):
        build_order("CC-0000-0001", (OrderLine("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", 1),), products, NOW, currency=CURRENCY)


def test_cancel_transition_and_repeat():
    order = build_order("CC-0000-0001", (OrderLine("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", 1),), PRODUCTS, NOW, currency=CURRENCY)
    once = cancelled(order, "2026-10-24T15:00:00Z")
    assert (once.status, once.version, once.updated_at, once.created_at) == ("CANCELLED", 2, "2026-10-24T15:00:00Z", NOW)
    assert cancelled(once, "2026-10-24T16:00:00Z") is once
