"""The JWT seam: claims are read from the authorizer context and ownership is enforced in the service."""
import itertools
import json
import time

import pytest

from events import http_api_event
from fakes import FakeCatalog, InMemoryOrderRepository
from orders_service.auth import ANONYMOUS, Caller, caller_from_event
from orders_service.domain import OrderNotFound
from orders_service.handler import handle
from orders_service.service import OrderService

BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}]}


def with_claims(event, sub):
    event["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": sub}, "scopes": None}}
    return event


@pytest.fixture
def service():
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    return OrderService(InMemoryOrderRepository(), FakeCatalog(), id_factory=lambda: next(ids))


def test_no_authorizer_means_anonymous():
    assert caller_from_event(http_api_event("GET /orders/{orderId}", "/orders/x")) == ANONYMOUS


def test_claims_become_the_caller():
    event = with_claims(http_api_event("GET /orders/{orderId}", "/orders/x"), "student-1")
    assert caller_from_event(event) == Caller("student-1")


def test_owned_orders_are_invisible_and_uncancellable_for_others(service):
    alice, bob = Caller("alice"), Caller("bob")
    order_id = service.create_order("demo-order-001", BODY, time.monotonic() + 5, alice).order_id
    assert service.get_order(order_id, alice).owner_id == "alice"
    for action in (service.get_order, service.cancel_order):
        with pytest.raises(OrderNotFound):
            action(order_id, bob)
        with pytest.raises(OrderNotFound):
            action(order_id, ANONYMOUS)
    assert service.get_order(order_id, alice).status == "CREATED"


def test_idempotency_keys_are_scoped_per_caller(service):
    a = service.create_order("demo-order-001", BODY, time.monotonic() + 5, Caller("alice"))
    b = service.create_order("demo-order-001", BODY, time.monotonic() + 5, Caller("bob"))
    assert a.order_id != b.order_id and not b.replayed


def test_anonymous_baseline_orders_stay_shared(service):
    order_id = service.create_order("demo-order-001", BODY, time.monotonic() + 5).order_id
    assert service.get_order(order_id, Caller("anyone")).order_id == order_id


def test_handler_passes_claims_through(service):
    create = with_claims(http_api_event("POST /orders", "/orders", body=BODY,
                                        headers={"Idempotency-Key": "demo-order-001"}), "alice")
    order_id = json.loads(handle(create, service, time.monotonic() + 5)["body"])["orderId"]
    path = {"orderId": order_id}
    as_bob = with_claims(http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters=path), "bob")
    as_alice = with_claims(http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters=path), "alice")
    assert handle(as_bob, service, time.monotonic() + 5)["statusCode"] == 404
    assert handle(as_alice, service, time.monotonic() + 5)["statusCode"] == 200
    assert "ownerId" not in json.loads(handle(as_alice, service, time.monotonic() + 5)["body"])
