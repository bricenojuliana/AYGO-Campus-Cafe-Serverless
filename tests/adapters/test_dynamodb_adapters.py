"""Runs the real DynamoDB adapters against moto's DynamoDB implementation."""
import itertools
import threading
import time
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from catalog_service.domain import Product, product_to_record
from catalog_service.repository import DynamoProductRepository
from fakes import FakeCatalog
from orders_service.domain import IdempotencyRecord, OrderLine, build_order
from orders_service.ports import IdempotencyKeyTaken, OrderIdTaken, VersionConflict
from orders_service.repository import DynamoOrderRepository
from orders_service.service import OrderService

NOW = 1_800_000_000
BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.client("dynamodb")


@pytest.fixture
def orders_repo(aws):
    aws.create_table(TableName="orders", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
    return DynamoOrderRepository(aws, "orders")


def sample_order(order_id="CC-0000-0001"):
    return build_order(order_id, (OrderLine("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", 2),), FakeCatalog().products, "2026-10-24T14:00:00Z")


def record_for(order, key="demo-order-001", fingerprint="fp", expires_at=NOW + 100):
    return IdempotencyRecord(key, fingerprint, order.order_id, order.to_api(), expires_at)


def test_catalog_repository_reads_seeded_products(aws):
    aws.create_table(TableName="catalog", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "productId", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "productId", "AttributeType": "S"}])
    table = boto3.resource("dynamodb").Table("catalog")
    coffee = Product("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "Coffee", 350, True)
    table.put_item(Item=product_to_record(coffee))
    table.put_item(Item={"productId": "broken", "name": "", "priceCents": 1, "available": True})
    repository = DynamoProductRepository(table)
    assert repository.get("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b") == coffee
    assert repository.get("3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e") is None
    assert repository.list_all() == [coffee]  # invalid records are skipped, not served


def test_order_and_idempotency_record_round_trip(orders_repo):
    order = sample_order()
    orders_repo.create_order(order, record_for(order), NOW)
    assert orders_repo.get_order("CC-0000-0001") == order
    stored = orders_repo.get_idempotency_record("demo-order-001", NOW)
    assert stored.order_id == "CC-0000-0001" and stored.response_body == order.to_api()


def test_second_transaction_with_same_key_writes_nothing(orders_repo, aws):
    first, second = sample_order("CC-0000-0001"), sample_order("CC-0000-0002")
    orders_repo.create_order(first, record_for(first), NOW)
    with pytest.raises(IdempotencyKeyTaken):
        orders_repo.create_order(second, record_for(second), NOW)
    assert orders_repo.get_order("CC-0000-0002") is None  # the transaction rolled back the order put too
    assert aws.scan(TableName="orders")["Count"] == 2


def test_existing_order_code_is_reported_as_taken(orders_repo):
    first = sample_order("CC-0000-0001")
    orders_repo.create_order(first, record_for(first), NOW)
    clash = sample_order("CC-0000-0001")
    with pytest.raises(OrderIdTaken):
        orders_repo.create_order(clash, record_for(clash, key="demo-order-002"), NOW)
    assert orders_repo.get_idempotency_record("demo-order-002", NOW) is None


def test_expired_record_is_ignored_and_can_be_replaced(orders_repo):
    old, new = sample_order("CC-0000-0001"), sample_order("CC-0000-0002")
    orders_repo.create_order(old, record_for(old, expires_at=NOW - 1), NOW - 10)
    assert orders_repo.get_idempotency_record("demo-order-001", NOW) is None
    orders_repo.create_order(new, record_for(new), NOW)
    assert orders_repo.get_idempotency_record("demo-order-001", NOW).order_id == "CC-0000-0002"


def test_conditional_replace_never_creates_and_detects_conflicts(orders_repo):
    order = sample_order()
    with pytest.raises(VersionConflict):
        orders_repo.replace_order(order, expected_version=1)
    assert orders_repo.get_order("CC-0000-0001") is None
    orders_repo.create_order(order, record_for(order), NOW)
    with pytest.raises(VersionConflict):
        orders_repo.replace_order(order, expected_version=7)


def test_service_end_to_end_on_dynamodb(orders_repo):
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    service = OrderService(orders_repo, FakeCatalog(), id_factory=lambda: next(ids),
                           clock=lambda: datetime(2026, 10, 24, 14, tzinfo=timezone.utc))
    created = service.create_order("demo-order-001", BODY, time.monotonic() + 5)
    replay = service.create_order("demo-order-001", BODY, time.monotonic() + 5)
    assert replay.replayed and replay.body == created.body
    assert service.cancel_order(created.order_id).status == "CANCELLED"
    assert service.cancel_order(created.order_id).status == "CANCELLED"
    assert orders_repo.get_order(created.order_id).version == 2


class SerializedClient:
    """moto's transact_write_items snapshots and restores whole tables without a lock, so overlapping
    transactions from several threads can undo each other (real DynamoDB isolates them). Calls are
    therefore serialized here; the race itself is forced by the barrier below, which makes every thread
    pass the idempotency pre-check before any transaction runs. Real concurrency is checked by
    scripts/smoke_test.py against the deployed API."""

    def __init__(self, client):
        self._client = client
        self._lock = threading.Lock()

    def __getattr__(self, name):
        method = getattr(self._client, name)

        def call(*args, **kwargs):
            with self._lock:
                return method(*args, **kwargs)
        return call


def test_racing_identical_submissions_persist_one_order(aws, orders_repo):
    repository = DynamoOrderRepository(SerializedClient(aws), "orders")
    threads_count = 5
    after_precheck = threading.Barrier(threads_count)
    original_precheck = repository.get_idempotency_record
    calls = {"n": 0}

    def precheck_then_wait(key, now_epoch):
        calls["n"] += 1
        if calls["n"] <= threads_count:  # only the initial pre-checks, not the post-conflict re-read
            record = original_precheck(key, now_epoch)
            after_precheck.wait()
            return record
        return original_precheck(key, now_epoch)

    repository.get_idempotency_record = precheck_then_wait
    ids = iter(f"CC-0000-{n:04d}" for n in range(threads_count))
    id_lock = threading.Lock()

    def next_id():
        with id_lock:
            return next(ids)

    service = OrderService(repository, FakeCatalog(), id_factory=next_id)
    results = []

    def submit():
        results.append(service.create_order("demo-order-001", BODY, time.monotonic() + 5))

    threads = [threading.Thread(target=submit) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    orders = [i for i in aws.scan(TableName="orders")["Items"] if i["entityType"]["S"] == "ORDER"]
    assert len(orders) == 1
    assert len(results) == threads_count and len({r.order_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1  # the other four lost the transaction and replayed
