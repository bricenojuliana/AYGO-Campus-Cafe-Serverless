"""Keeps docs/openapi.yaml in step with the deployed routes and with what the code actually returns."""
import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate as validate_openapi

from catalog_service.domain.product import product_from_record
from fakes import CURRENCY, OWNER
from orders_service.api.responses import ERROR_STATUS
from orders_service.domain import requests
from orders_service.domain.ids import new_order_id
from orders_service.domain.order import CatalogProduct, OrderLine, build_order, cancelled
from test_template import CfnLoader

ROOT = Path(__file__).resolve().parents[2]
METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load((ROOT / "docs/openapi.yaml").read_text())


def schema(spec, name):
    """Component schema with local $refs resolvable by jsonschema."""
    return {**spec["components"]["schemas"][name], "components": spec["components"]}


def test_spec_is_valid_openapi(spec):
    validate_openapi(spec)


def test_spec_routes_equal_template_routes(spec):
    template = yaml.load((ROOT / "template.yaml").read_text(), Loader=CfnLoader)
    template_routes = {r["Properties"]["RouteKey"] for r in template["Resources"].values()
                       if r["Type"] == "AWS::ApiGatewayV2::Route"}
    spec_routes = {f"{method.upper()} {path}" for path, item in spec["paths"].items()
                   for method in item if method in METHODS}
    assert spec_routes == template_routes


def test_contract_status_codes_are_documented(spec):
    def statuses(path, method):
        return set(spec["paths"][path][method]["responses"])

    assert statuses("/products", "get") >= {"200", "503"}
    assert statuses("/products/{productId}", "get") >= {"200", "404"}
    assert statuses("/orders", "post") >= {"201", "200", "400", "409", "503"}
    assert statuses("/orders/{orderId}", "get") >= {"200", "404"}
    assert statuses("/orders/{orderId}/cancel", "post") >= {"200", "404"}
    assert "Location" in spec["paths"]["/orders"]["post"]["responses"]["201"]["headers"]


def test_seed_products_match_the_product_schema(spec):
    for record in json.loads((ROOT / "seed/products.json").read_text()):
        jsonschema.validate(product_from_record(record, CURRENCY).to_api(), schema(spec, "Product"))


def test_created_and_cancelled_orders_match_the_order_schema(spec):
    product = CatalogProduct("382501b6-8968-4ca8-add5-961d414d5bb9", "Café tinto", 200000, "COP", True)
    order = build_order(new_order_id(), [OrderLine(product.product_id, 2)], {product.product_id: product},
                        "2026-10-31T03:18:55Z", currency=CURRENCY, owner=OWNER)
    jsonschema.validate(order.to_api(), schema(spec, "Order"))
    jsonschema.validate(cancelled(order, "2026-10-31T03:20:00Z").to_api(), schema(spec, "Order"))


def test_request_schema_matches_the_domain_validation(spec):
    request = schema(spec, "CreateOrderRequest")
    assert request["properties"]["items"]["maxItems"] == requests.MAX_DISTINCT_PRODUCTS
    line = request["properties"]["items"]["items"]["properties"]
    assert (line["quantity"]["minimum"], line["quantity"]["maximum"]) == (requests.MIN_QUANTITY, requests.MAX_QUANTITY)
    key = spec["paths"]["/orders"]["post"]["parameters"][0]
    assert re.compile(key["schema"]["pattern"]).pattern == requests.IDEMPOTENCY_KEY_PATTERN.pattern
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"items": [{"productId": "382501b6-8968-4ca8-add5-961d414d5bb9", "quantity": 1}],
                             "totalCents": 1}, request)


def test_every_orders_error_code_is_documented(spec):
    documented = set(spec["components"]["schemas"]["Error"]["properties"]["code"]["enum"])
    assert {error.code for error in ERROR_STATUS} <= documented
