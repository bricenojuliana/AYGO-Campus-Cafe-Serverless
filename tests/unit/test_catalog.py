import json
from decimal import Decimal

import pytest

from catalog_service.domain import (InvalidProduct, InvalidProductId, Product, product_from_record,
                                    product_to_record, sort_for_menu, validate_product_id)
from catalog_service.handler import handle
from catalog_service.repository import StoreUnavailable
from events import http_api_event

COFFEE = Product("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "Coffee", 350, True)
MUFFIN = Product("4f7b0e5c-6a8b-4c23-9e5f-7a9b1c3d5e6f", "Blueberry muffin", 425, False)


class FakeRepository:
    def __init__(self, products):
        self.products = {p.product_id: p for p in products}

    def get(self, product_id):
        return self.products.get(product_id)

    def list_all(self):
        return list(self.products.values())


class BrokenRepository:
    def get(self, product_id):
        raise StoreUnavailable("boom")

    def list_all(self):
        raise StoreUnavailable("boom")


def body(response):
    return json.loads(response["body"])


def get_product_event(product_id):
    return http_api_event("GET /products/{productId}", f"/products/{product_id}",
                          path_parameters={"productId": product_id})


@pytest.mark.parametrize("value", ["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "00000000-0000-4000-8000-000000000000"])
def test_valid_product_ids(value):
    assert validate_product_id(value) == value


@pytest.mark.parametrize("value", ["", "coffee", "0B3F6A1E-2C4D-4E8F-9A1B-3C5D7E9F1A2B",
                                   "0b3f6a1e2c4d4e8f9a1b3c5d7e9f1a2b", "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b-x",
                                   "../0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", None])
def test_invalid_product_ids(value):
    with pytest.raises(InvalidProductId):
        validate_product_id(value)


def test_record_round_trip_with_dynamodb_decimals():
    record = {**product_to_record(COFFEE), "priceCents": Decimal("350")}
    assert product_from_record(record) == COFFEE


@pytest.mark.parametrize("override", [
    {"priceCents": -1},
    {"priceCents": Decimal("3.5")},
    {"currency": "EUR"},
    {"name": "  "},
    {"available": None, "productId": "BAD"},
])
def test_records_breaking_invariants_are_rejected(override):
    with pytest.raises(InvalidProduct):
        product_from_record({**product_to_record(COFFEE), **override})


def test_record_without_availability_is_rejected():
    record = product_to_record(COFFEE)
    del record["available"]
    with pytest.raises(InvalidProduct):
        product_from_record(record)


def test_menu_is_sorted_by_name():
    assert [p.product_id for p in sort_for_menu([COFFEE, MUFFIN])] == ["4f7b0e5c-6a8b-4c23-9e5f-7a9b1c3d5e6f", "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"]


def test_list_products_route():
    response = handle(http_api_event("GET /products", "/products"), FakeRepository([COFFEE, MUFFIN]))
    assert response["statusCode"] == 200
    assert response["headers"] == {"content-type": "application/json"}
    assert body(response) == {"items": [
        {"productId": "4f7b0e5c-6a8b-4c23-9e5f-7a9b1c3d5e6f", "name": "Blueberry muffin", "priceCents": 425,
         "currency": "COP", "available": False},
        {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "name": "Coffee", "priceCents": 350, "currency": "COP", "available": True},
    ]}


def test_get_product_route_returns_the_product_itself():
    response = handle(get_product_event("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"), FakeRepository([COFFEE]))
    assert response["statusCode"] == 200
    assert body(response) == {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "name": "Coffee", "priceCents": 350,
                              "currency": "COP", "available": True}


def test_unknown_product_is_404_with_application_error():
    response = handle(get_product_event("3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e"), FakeRepository([COFFEE]))
    assert response["statusCode"] == 404
    assert body(response) == {"code": "PRODUCT_NOT_FOUND", "message": "This product does not exist.",
                              "requestId": "req-1"}


def test_malformed_product_id_is_an_unknown_product():
    assert handle(get_product_event("BAD"), FakeRepository([]))["statusCode"] == 404


@pytest.mark.parametrize("event", [http_api_event("GET /products", "/products"), get_product_event("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b")])
def test_store_failure_is_503(event):
    response = handle(event, BrokenRepository())
    assert response["statusCode"] == 503
    assert body(response)["code"] == "STORE_UNAVAILABLE"


def test_rejects_non_v2_payloads():
    event = http_api_event("GET /products", "/products")
    event["version"] = "1.0"
    assert body(handle(event, FakeRepository([])))["code"] == "UNSUPPORTED_PAYLOAD"
