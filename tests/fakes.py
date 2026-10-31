"""Test doubles for the Orders ports. Never imported by application code."""
import copy
import threading
import time

from orders_service.application.ports import IdempotencyKeyTaken, OrderIdTaken, VersionConflict
from orders_service.domain.errors import CatalogUnavailable
from orders_service.domain.order import CatalogProduct

PRODUCTS = {
    "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b": CatalogProduct("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "Coffee", 350, "COP", True),
    "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c": CatalogProduct("1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "Croissant", 275, "COP", True),
    "2d5f8c3a-4e6f-4a01-9c3d-5e7f9a1b3c4d": CatalogProduct("2d5f8c3a-4e6f-4a01-9c3d-5e7f9a1b3c4d", "Matcha latte", 525, "COP", False),
}


class FakeCatalog:
    def __init__(self, products=None, fail=False):
        self.products = dict(PRODUCTS if products is None else products)
        self.fail = fail
        self.calls = 0

    def get_products(self, product_ids, deadline):
        self.calls += 1
        if self.fail:
            raise CatalogUnavailable()
        return {pid: self.products.get(pid) for pid in product_ids}


class InMemoryOrderRepository:
    """Mirrors the DynamoDB adapter's conditional semantics under a lock."""

    def __init__(self):
        self.orders = {}
        self.records = {}
        self.lock = threading.Lock()
        self.create_delay = 0.0

    def get_idempotency_record(self, key, now_epoch):
        record = self.records.get(key)
        return record if record and record.expires_at > now_epoch else None

    def create_order(self, order, record, now_epoch):
        time.sleep(self.create_delay)
        with self.lock:
            if self.get_idempotency_record(record.key, now_epoch):
                raise IdempotencyKeyTaken(record.key)
            if order.order_id in self.orders:
                raise OrderIdTaken(order.order_id)
            self.orders[order.order_id] = order
            self.records[record.key] = copy.deepcopy(record)

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def replace_order(self, order, expected_version):
        with self.lock:
            current = self.orders.get(order.order_id)
            if current is None or current.version != expected_version:
                raise VersionConflict(order.order_id)
            self.orders[order.order_id] = order
