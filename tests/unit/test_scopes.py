"""Each Orders route needs its own scope; a token without it gets 403 and nothing happens."""
import itertools
import json
import time

import pytest

from events import TEST_CLAIMS, http_api_event
from fakes import CURRENCY, IDEMPOTENCY_TTL_SECONDS, FakeCatalog, InMemoryOrderRepository
from orders_service.api.auth import ROUTE_SCOPES
from orders_service.application.service import OrderService
from orders_service.handler import handle

BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}]}


def with_scope(scope):
    claims = dict(TEST_CLAIMS)
    if scope is None:
        claims.pop("scope")
    else:
        claims["scope"] = scope
    return claims


def create(scope, key="scope-test-001"):
    return http_api_event("POST /orders", "/orders", body=BODY, headers={"Idempotency-Key": key},
                          claims=with_scope(scope))


def read(order_id, scope):
    return http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters={"orderId": order_id},
                          claims=with_scope(scope))


def cancel(order_id, scope):
    return http_api_event("POST /orders/{orderId}/cancel", f"/orders/{order_id}/cancel",
                          path_parameters={"orderId": order_id}, claims=with_scope(scope))


@pytest.fixture
def catalog():
    return FakeCatalog()


@pytest.fixture
def repository():
    return InMemoryOrderRepository()


@pytest.fixture
def service(repository, catalog):
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    return OrderService(repository, catalog, id_factory=lambda: next(ids),
                        currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)


def call(event, service):
    response = handle(event, service, time.monotonic() + 5)
    return response, json.loads(response["body"])


def assert_forbidden(response, body):
    assert response["statusCode"] == 403
    assert body["code"] == "INSUFFICIENT_SCOPE" and set(body) == {"code", "message", "requestId"}
    assert response["headers"]["www-authenticate"] == 'Bearer error="insufficient_scope"'


def test_each_route_requires_exactly_one_scope():
    assert ROUTE_SCOPES == {"POST /orders": "orders:create", "GET /orders/{orderId}": "orders:read",
                            "POST /orders/{orderId}/cancel": "orders:cancel"}


def test_each_route_works_with_only_its_own_scope(service):
    response, created = call(create("orders:create"), service)
    assert response["statusCode"] == 201
    assert call(read(created["orderId"], "orders:read"), service)[0]["statusCode"] == 200
    assert call(cancel(created["orderId"], "orders:cancel"), service)[1]["status"] == "CANCELLED"


def test_read_only_token_cannot_cancel_and_nothing_changes(service):
    _, created = call(create("orders:create orders:read"), service)
    assert_forbidden(*call(cancel(created["orderId"], "orders:read"), service))
    assert call(read(created["orderId"], "orders:read"), service)[1]["status"] == "CREATED"


def test_create_token_cannot_read_or_cancel(service):
    _, created = call(create("orders:create"), service)
    assert_forbidden(*call(read(created["orderId"], "orders:create"), service))
    assert_forbidden(*call(cancel(created["orderId"], "orders:create"), service))


@pytest.mark.parametrize("scope", [
    None,                                   # no scope claim
    "",                                     # empty
    "orders:read orders:cancel",            # other scopes only
    "orders:createx orders:create-all",     # look-alikes: scopes match exactly, never by prefix
    "ORDERS:CREATE",                        # scopes are case-sensitive
    ["orders:create"],                      # wrong type: the scope claim is a space-separated string
])
def test_create_without_its_scope_is_forbidden_before_any_work(service, repository, catalog, scope):
    assert_forbidden(*call(create(scope), service))
    assert catalog.calls == 0 and repository.orders == {} and repository.records == {}


def test_missing_identity_is_401_even_without_scopes(service):
    event = create(None)
    del event["requestContext"]["authorizer"]
    assert call(event, service)[0]["statusCode"] == 401  # identity is checked before permissions
