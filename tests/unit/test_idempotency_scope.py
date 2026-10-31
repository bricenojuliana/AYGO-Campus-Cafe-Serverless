"""Idempotency keys are scoped to the caller: same request replays, a changed request is 409, and one
user's stored response is never returned to another."""
import itertools
import time

import pytest

from fakes import CURRENCY, IDEMPOTENCY_TTL_SECONDS, OWNER, FakeCatalog, InMemoryOrderRepository
from orders_service.application.service import OrderService
from orders_service.domain.errors import IdempotencyConflict
from orders_service.domain.order import IdempotencyRecord
from orders_service.domain.principal import Principal
from orders_service.domain.requests import idempotency_scope_key

ISS = OWNER.issuer
BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 1}]}
CHANGED = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]}
KEY = "retry-key-001"


# ---------- the scoped key itself ----------

def test_same_inputs_give_the_same_key():
    assert idempotency_scope_key(ISS, "auth0|a", KEY) == idempotency_scope_key(ISS, "auth0|a", KEY)


@pytest.mark.parametrize("left, right", [
    ((ISS, "auth0|a", KEY), (ISS, "auth0|b", KEY)),                        # another user
    ((ISS, "auth0|a", KEY), ("https://other.example.com/", "auth0|a", KEY)),  # same sub, another issuer
    ((ISS, "auth0|a", KEY), (ISS, "auth0|a", "retry-key-002")),            # another key
    ((ISS, "auth0|a", "b|k"), (ISS, "auth0|a|b", "k")),                    # "|" moved between sub and key
    (("a|b", "c", KEY), ("a", "b|c", KEY)),                                # "|" moved between iss and sub
    ((ISS, "auth0|a", "x:y"), (ISS, "auth0|a:x", "y")),                    # other separators too
    ((ISS, "auth0|a", "k"), (ISS + "auth0|a", "", "k")),                   # plain concatenation would match
])
def test_different_inputs_never_collide(left, right):
    assert idempotency_scope_key(*left) != idempotency_scope_key(*right)


def test_key_is_a_short_string_whatever_the_input_length():
    long_sub = "auth0|" + "x" * 1500
    scoped = idempotency_scope_key(ISS, long_sub, "k" * 128)
    assert isinstance(scoped, str) and 0 < len(scoped.encode()) <= 512


# ---------- the service using it ----------

@pytest.fixture
def repository():
    return InMemoryOrderRepository()


@pytest.fixture
def service(repository):
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    return OrderService(repository, FakeCatalog(), id_factory=lambda: next(ids),
                        currency=CURRENCY, idempotency_ttl_seconds=IDEMPOTENCY_TTL_SECONDS)


def deadline():
    return time.monotonic() + 5


def test_same_user_same_request_replays_the_original_result(service):
    first = service.create_order(KEY, BODY, deadline(), OWNER)
    again = service.create_order(KEY, BODY, deadline(), OWNER)
    assert again.replayed and again.order_id == first.order_id and again.body == first.body


def test_same_user_changed_request_is_a_conflict(service):
    service.create_order(KEY, BODY, deadline(), OWNER)
    with pytest.raises(IdempotencyConflict):
        service.create_order(KEY, CHANGED, deadline(), OWNER)


def test_two_users_with_the_same_key_get_separate_orders(service, repository):
    other = Principal(ISS, "auth0|someone-else")
    mine = service.create_order(KEY, BODY, deadline(), OWNER)
    theirs = service.create_order(KEY, BODY, deadline(), other)
    assert not theirs.replayed and theirs.order_id != mine.order_id
    assert len(repository.orders) == 2 and len(repository.records) == 2


def test_a_record_of_another_user_is_never_replayed(service, repository):
    """Even if a stored record were found under this caller's key, its response is not handed out."""
    other = Principal(ISS, "auth0|someone-else")
    theirs = service.create_order(KEY, BODY, deadline(), other)
    planted = repository.records[idempotency_scope_key(ISS, other.subject, KEY)]
    repository.records[idempotency_scope_key(ISS, OWNER.subject, KEY)] = planted
    with pytest.raises(IdempotencyConflict):
        service.create_order(KEY, BODY, deadline(), OWNER)
    assert theirs.order_id not in {o.order_id for o in repository.orders.values() if o.owner_subject == OWNER.subject}


def test_stored_record_is_scoped_and_owned(service, repository):
    service.create_order(KEY, BODY, deadline(), OWNER)
    (stored_key, record), = repository.records.items()
    assert stored_key == idempotency_scope_key(OWNER.issuer, OWNER.subject, KEY) != KEY
    assert isinstance(record, IdempotencyRecord) and record.belongs_to(OWNER)
