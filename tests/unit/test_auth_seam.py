"""Identity comes only from the verified JWT authorizer context; without it, Orders refuses (fail closed)."""
import itertools
import json
import time

import pytest

from events import TEST_CLAIMS, http_api_event
from fakes import CURRENCY, FakeCatalog, IDEMPOTENCY_TTL_SECONDS, InMemoryOrderRepository
from orders_service.api.auth import principal_from_event
from orders_service.application.service import OrderService
from orders_service.domain.errors import OrderNotFound, Unauthenticated
from orders_service.domain.principal import ANONYMOUS, Principal
from orders_service.handler import handle

PRODUCT_ID = "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"
BODY = {"items": [{"productId": PRODUCT_ID, "quantity": 1}]}
ISSUER = TEST_CLAIMS["iss"]


def orders_events(claims=None, headers=None, body=BODY):
    path = {"orderId": "CC-0000-0001"}
    return [
        http_api_event("POST /orders", "/orders", body=body, claims=claims,
                       headers={"Idempotency-Key": "demo-order-001", **(headers or {})}),
        http_api_event("GET /orders/{orderId}", "/orders/CC-0000-0001", path_parameters=path,
                       claims=claims, headers=headers),
        http_api_event("POST /orders/{orderId}/cancel", "/orders/CC-0000-0001/cancel", path_parameters=path,
                       claims=claims, headers=headers),
    ]


@pytest.fixture
def repository():
    return InMemoryOrderRepository()


@pytest.fixture
def catalog():
    return FakeCatalog()


@pytest.fixture
def service(repository, catalog):
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    return OrderService(repository, catalog, id_factory=lambda: next(ids), currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)


@pytest.mark.parametrize("claims", [
    None,                                        # no authorizer context at all
    {},                                          # authorizer present, no claims
    {"sub": "auth0|student-1"},                  # issuer missing
    {"iss": ISSUER},                             # subject missing
    {"iss": "", "sub": "auth0|student-1"},       # empty values
    {"iss": ISSUER, "sub": 12345},               # wrong type
])
def test_every_orders_route_fails_closed_without_a_verified_principal(service, repository, catalog, claims):
    for event in orders_events(claims=claims):
        response = handle(event, service, time.monotonic() + 5)
        body = json.loads(response["body"])
        assert response["statusCode"] == 401
        assert body["code"] == "UNAUTHENTICATED" and set(body) == {"code", "message", "requestId"}
        assert response["headers"]["www-authenticate"] == "Bearer"
    assert catalog.calls == 0 and repository.orders == {} and repository.records == {}


def test_identity_claimed_in_headers_or_body_is_ignored(service, repository):
    spoof_headers = {"x-user": "auth0|admin", "x-customer-id": "auth0|admin", "authorization": "Bearer forged"}
    spoof_body = {**BODY, "customerId": "auth0|admin"}
    for event in orders_events(claims=None, headers=spoof_headers, body=spoof_body):
        assert handle(event, service, time.monotonic() + 5)["statusCode"] == 401
    assert repository.orders == {} and repository.records == {}


def test_ownership_fields_in_create_input_are_rejected(service):
    for field in ("customerId", "ownerId", "ownerSub"):
        event = orders_events(claims=TEST_CLAIMS, body={**BODY, field: "auth0|someone-else"})[0]
        response = handle(event, service, time.monotonic() + 5)
        assert (response["statusCode"], json.loads(response["body"])["code"]) == (400, "INVALID_REQUEST")


def test_verified_claims_become_the_principal():
    event = http_api_event("GET /orders/{orderId}", "/orders/x", claims=TEST_CLAIMS)
    assert principal_from_event(event) == Principal(issuer=ISSUER, subject="auth0|student-1",
                                                    scopes=frozenset({"orders:create", "orders:read", "orders:cancel"}))


def test_missing_context_raises_instead_of_defaulting_to_anonymous():
    with pytest.raises(Unauthenticated):
        principal_from_event(http_api_event("GET /orders/{orderId}", "/orders/x"))


def test_owned_orders_are_invisible_and_uncancellable_for_others(service):
    alice, bob = Principal(ISSUER, "alice"), Principal(ISSUER, "bob")
    order_id = service.create_order("demo-order-001", BODY, time.monotonic() + 5, alice).order_id
    assert service.get_order(order_id, alice).owner_id == "alice"
    for action in (service.get_order, service.cancel_order):
        with pytest.raises(OrderNotFound):
            action(order_id, bob)
        with pytest.raises(OrderNotFound):
            action(order_id, ANONYMOUS)
    assert service.get_order(order_id, alice).status == "CREATED"


def test_idempotency_keys_are_scoped_per_caller(service):
    a = service.create_order("demo-order-001", BODY, time.monotonic() + 5, Principal(ISSUER, "alice"))
    b = service.create_order("demo-order-001", BODY, time.monotonic() + 5, Principal(ISSUER, "bob"))
    assert a.order_id != b.order_id and not b.replayed


def test_handler_scopes_orders_to_the_token_subject(service):
    create = http_api_event("POST /orders", "/orders", body=BODY, headers={"Idempotency-Key": "demo-order-002"},
                            claims={**TEST_CLAIMS, "sub": "alice"})
    order_id = json.loads(handle(create, service, time.monotonic() + 5)["body"])["orderId"]
    path = {"orderId": order_id}
    as_bob = http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters=path,
                            claims={**TEST_CLAIMS, "sub": "bob"})
    as_alice = http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters=path,
                              claims={**TEST_CLAIMS, "sub": "alice"})
    assert handle(as_bob, service, time.monotonic() + 5)["statusCode"] == 404
    assert handle(as_alice, service, time.monotonic() + 5)["statusCode"] == 200
    assert "ownerId" not in json.loads(handle(as_alice, service, time.monotonic() + 5)["body"])
