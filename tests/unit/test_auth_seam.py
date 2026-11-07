"""Identity comes only from the verified JWT authorizer context; without it, Orders refuses (fail closed).
Ownership of orders is tested in test_ownership.py."""
import itertools
import json
import time

import pytest

from events import TEST_CLAIMS, http_api_event
from fakes import CURRENCY, FakeCatalog, IDEMPOTENCY_TTL_SECONDS, InMemoryOrderRepository
from orders_service.api.auth import principal_from_event
from orders_service.application.service import OrderService
from orders_service.domain.errors import Unauthenticated
from orders_service.domain.principal import Principal
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


# S1: ownership is never accepted from the client. The create body allows only "items", so these (and any
# other unknown field) get a validation error; the list documents the ones the workshop names explicitly.
FORBIDDEN_OWNER_FIELDS = ("owner", "ownerSub", "ownerIssuer", "ownerIss", "ownerId", "customerId", "sub", "iss")


@pytest.mark.parametrize("field", FORBIDDEN_OWNER_FIELDS)
def test_ownership_fields_in_create_input_are_rejected_and_nothing_is_written(service, repository, catalog, field):
    event = orders_events(claims=TEST_CLAIMS, body={**BODY, field: "auth0|someone-else"})[0]
    response = handle(event, service, time.monotonic() + 5)
    assert (response["statusCode"], json.loads(response["body"])["code"]) == (400, "INVALID_REQUEST")
    assert field in json.loads(response["body"])["message"]
    assert catalog.calls == 0 and repository.orders == {} and repository.records == {}


def test_ownership_inside_an_item_line_is_rejected_too(service, repository):
    body = {"items": [{**BODY["items"][0], "owner": "auth0|someone-else"}]}
    response = handle(orders_events(claims=TEST_CLAIMS, body=body)[0], service, time.monotonic() + 5)
    assert response["statusCode"] == 400 and repository.orders == {}


def test_verified_claims_become_the_principal():
    event = http_api_event("GET /orders/{orderId}", "/orders/x", claims=TEST_CLAIMS)
    assert principal_from_event(event) == Principal(issuer=ISSUER, subject="auth0|student-1",
                                                    scopes=frozenset({"orders:create", "orders:read", "orders:cancel"}))


def test_missing_context_raises_instead_of_defaulting_to_anonymous():
    with pytest.raises(Unauthenticated):
        principal_from_event(http_api_event("GET /orders/{orderId}", "/orders/x"))


def test_missing_context_leaves_a_safe_diagnostic_without_the_token(service, caplog):
    event = orders_events(claims=None, headers={"authorization": "Bearer secret-token-value"})[0]
    with caplog.at_level("WARNING"):
        assert handle(event, service, time.monotonic() + 5)["statusCode"] == 401
    lines = [json.loads(r.getMessage()) for r in caplog.records if "rejected inside Orders" in r.getMessage()]
    assert lines == [{"msg": "request rejected inside Orders", "code": "UNAUTHENTICATED",
                      "requestId": "req-1", "routeKey": "POST /orders"}]
    assert "secret-token-value" not in caplog.text
