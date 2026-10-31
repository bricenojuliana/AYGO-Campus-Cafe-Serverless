import base64
import itertools
import json
import time
from datetime import datetime, timezone

import pytest

from events import http_api_event
from fakes import FakeCatalog, InMemoryOrderRepository
from orders_service.handler import catalog_deadline, handle
from orders_service.service import OrderService

BODY = {"items": [{"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "quantity": 2}]}


@pytest.fixture
def service():
    ids = (f"CC-0000-{n:04d}" for n in itertools.count(1))
    return OrderService(InMemoryOrderRepository(), FakeCatalog(), id_factory=lambda: next(ids),
                        clock=lambda: datetime(2026, 10, 24, 14, tzinfo=timezone.utc))


def create_event(body=BODY, key="demo-order-001"):
    headers = {"Idempotency-Key": key} if key else {}
    return http_api_event("POST /orders", "/orders", body=body, headers=headers)


def call(event, service):
    response = handle(event, service, time.monotonic() + 5)
    return response, json.loads(response["body"])


def test_first_creation_is_201_with_location(service):
    response, body = call(create_event(), service)
    assert response["statusCode"] == 201
    assert response["headers"] == {"content-type": "application/json", "location": "/orders/CC-0000-0001"}
    assert body["orderId"] == "CC-0000-0001" and body["status"] == "CREATED" and body["totalCents"] == 700


def test_replay_is_200_with_original_body(service):
    _, first = call(create_event(), service)
    response, body = call(create_event(), service)
    assert response["statusCode"] == 200 and body == first
    assert "location" not in response["headers"]


def test_base64_body_is_decoded(service):
    event = create_event()
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    assert call(event, service)[0]["statusCode"] == 201


@pytest.mark.parametrize("event, status, code", [
    (create_event(key=None), 400, "MISSING_IDEMPOTENCY_KEY"),
    (create_event(body="{not json"), 400, "INVALID_REQUEST"),
    (create_event(body={"items": [{"productId": "3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e", "quantity": 1}]}), 400, "UNKNOWN_PRODUCT"),
    (create_event(body={"items": [{"productId": "2d5f8c3a-4e6f-4a01-9c3d-5e7f9a1b3c4d", "quantity": 1}]}), 409, "PRODUCT_UNAVAILABLE"),
    (http_api_event("GET /orders/{orderId}", "/orders/nope", path_parameters={"orderId": "nope"}), 404, "ORDER_NOT_FOUND"),
    (http_api_event("POST /orders/{orderId}/cancel", "/orders/nope/cancel", path_parameters={"orderId": "nope"}),
     404, "ORDER_NOT_FOUND"),
])
def test_application_errors(service, event, status, code):
    response, body = call(event, service)
    assert response["statusCode"] == status
    assert body["code"] == code and body["requestId"] == "req-1" and body["message"]
    assert set(body) == {"code", "message", "requestId"}


def test_conflicting_key_is_409(service):
    call(create_event(), service)
    response, body = call(create_event(body={"items": [{"productId": "1c4e7b2f-3d5e-4f90-8b2c-4d6e8f0a2b3c", "quantity": 1}]}), service)
    assert (response["statusCode"], body["code"]) == (409, "IDEMPOTENCY_KEY_CONFLICT")


def test_catalog_failure_is_503():
    service = OrderService(InMemoryOrderRepository(), FakeCatalog(fail=True), id_factory=lambda: "CC-XXXX-XXXX")
    response, body = call(create_event(), service)
    assert (response["statusCode"], body["code"]) == (503, "CATALOG_UNAVAILABLE")


def test_get_and_cancel_flow(service):
    _, created = call(create_event(), service)
    order_id = created["orderId"]
    path = {"orderId": order_id}
    response, body = call(http_api_event("GET /orders/{orderId}", f"/orders/{order_id}", path_parameters=path), service)
    assert response["statusCode"] == 200 and body == created
    cancel = http_api_event("POST /orders/{orderId}/cancel", f"/orders/{order_id}/cancel", path_parameters=path)
    first_response, first = call(cancel, service)
    second_response, second = call(cancel, service)
    assert first_response["statusCode"] == second_response["statusCode"] == 200
    assert first["status"] == "CANCELLED" and first == second


def test_unknown_route_and_v1_payload(service):
    assert call(http_api_event("DELETE /orders", "/orders"), service)[0]["statusCode"] == 404
    event = create_event()
    event["version"] = "1.0"
    assert call(event, service)[1]["code"] == "UNSUPPORTED_PAYLOAD"


class FakeContext:
    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


def test_catalog_deadline_never_exceeds_lambda_time(monkeypatch):
    monkeypatch.setenv("CATALOG_BUDGET_SECONDS", "5")
    now = time.monotonic()
    assert catalog_deadline(FakeContext(3000)) - now == pytest.approx(2.0, abs=0.1)
    assert catalog_deadline(FakeContext(60000)) - now == pytest.approx(5.0, abs=0.1)
    assert catalog_deadline(FakeContext(500)) - now == pytest.approx(0.0, abs=0.1)
