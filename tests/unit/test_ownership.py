"""Every order belongs to the verified (iss, sub) that created it; nobody else can see or cancel it."""
import itertools
import json
import time
from dataclasses import replace

import pytest

from events import TEST_CLAIMS, http_api_event
from fakes import CURRENCY, IDEMPOTENCY_TTL_SECONDS, OWNER, FakeCatalog, InMemoryOrderRepository
from orders_service.application.service import OrderService
from orders_service.domain.errors import OrderNotFound
from orders_service.domain.principal import Principal
from orders_service.handler import handle

BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}]}
OTHER_USER = {**TEST_CLAIMS, "sub": "auth0|someone-else"}
OTHER_ISSUER = {**TEST_CLAIMS, "iss": "https://other-tenant.example.com/"}  # same sub, different issuer


@pytest.fixture
def repository():
    return InMemoryOrderRepository()


@pytest.fixture
def service(repository):
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    return OrderService(repository, FakeCatalog(), id_factory=lambda: next(ids),
                        currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)


def call(event, service):
    return handle(event, service, time.monotonic() + 5)


def create(service, claims=TEST_CLAIMS, key="owner-test-001", headers=None):
    event = http_api_event("POST /orders", "/orders", body=BODY, claims=claims,
                           headers={"Idempotency-Key": key, **(headers or {})})
    return json.loads(call(event, service)["body"])["orderId"]


def get(order_id, claims):
    return http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters={"orderId": order_id},
                          claims=claims)


def cancel(order_id, claims):
    return http_api_event("POST /orders/{orderId}/cancel", f"/orders/{order_id}/cancel",
                          path_parameters={"orderId": order_id}, claims=claims)


def test_owner_is_the_verified_token_principal_and_is_never_returned(service, repository):
    order_id = create(service, headers={"x-user": "auth0|admin"})  # spoofed header has no effect
    stored = repository.orders[order_id]
    assert (stored.owner_issuer, stored.owner_subject) == (TEST_CLAIMS["iss"], TEST_CLAIMS["sub"])
    for event in (get(order_id, TEST_CLAIMS), cancel(order_id, TEST_CLAIMS)):
        body = json.loads(call(event, service)["body"])
        assert not {"ownerIss", "ownerSub", "ownerId", "owner"} & set(body)


def test_owner_can_read_and_cancel(service):
    order_id = create(service)
    assert call(get(order_id, TEST_CLAIMS), service)["statusCode"] == 200
    assert json.loads(call(cancel(order_id, TEST_CLAIMS), service)["body"])["status"] == "CANCELLED"


def test_missing_foreign_and_legacy_orders_get_the_same_404(service, repository):
    order_id = create(service)
    legacy_id = "CC-0000-9999"
    repository.orders[legacy_id] = replace(repository.orders[order_id], order_id=legacy_id,
                                           owner_issuer=None, owner_subject=None)
    cases = {
        "missing": ("CC-0000-0404", TEST_CLAIMS),
        "other user": (order_id, OTHER_USER),
        "same sub, other issuer": (order_id, OTHER_ISSUER),
        "legacy without owner": (legacy_id, TEST_CLAIMS),
    }
    for build in (get, cancel):
        responses = {name: call(build(oid, claims), service) for name, (oid, claims) in cases.items()}
        expected = responses["missing"]
        assert expected["statusCode"] == 404
        assert json.loads(expected["body"])["code"] == "ORDER_NOT_FOUND"
        for name, response in responses.items():
            assert response == expected, name  # identical status, headers and body


def test_someone_else_cannot_cancel_and_the_order_is_unchanged(service, repository):
    order_id = create(service)
    before = repository.orders[order_id]
    for claims in (OTHER_USER, OTHER_ISSUER):
        assert call(cancel(order_id, claims), service)["statusCode"] == 404
    assert repository.orders[order_id] == before


def test_each_user_sees_only_their_own_orders(service):
    mine = create(service, key="owner-test-mine")
    theirs = create(service, claims=OTHER_USER, key="owner-test-theirs")
    assert call(get(mine, TEST_CLAIMS), service)["statusCode"] == 200
    assert call(get(theirs, TEST_CLAIMS), service)["statusCode"] == 404
    assert call(get(theirs, OTHER_USER), service)["statusCode"] == 200


def test_service_level_rule_matches_on_issuer_and_subject(service):
    order_id = service.create_order("owner-test-002", BODY, time.monotonic() + 5, OWNER).order_id
    for intruder in (Principal(OWNER.issuer, "auth0|someone-else"), Principal("https://other/", OWNER.subject)):
        for action in (service.get_order, service.cancel_order):
            with pytest.raises(OrderNotFound):
                action(order_id, intruder)
    assert service.get_order(order_id, OWNER).status == "CREATED"
