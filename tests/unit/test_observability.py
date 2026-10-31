import json
import time

from catalog_service.handler import request_log as catalog_request_log
from events import http_api_event
from orders_service.http import error_response, json_response
from orders_service.observability import request_log as orders_request_log

ORDER_ID = "CC-7K3Q-9XMD"


class Context:
    aws_request_id = "lambda-req-1"


def test_create_log_has_operation_order_id_result_and_duration():
    event = http_api_event("POST /orders", "/orders", body={})
    line = orders_request_log(event, Context(), json_response(201, {"orderId": ORDER_ID}), time.monotonic() - 0.05)
    assert line["operation"] == "CreateOrder" and line["orderId"] == ORDER_ID
    assert line["requestId"] == "req-1" and line["lambdaRequestId"] == "lambda-req-1"
    assert line["status"] == 201 and line["result"] == "OK" and line["durationMs"] >= 50
    json.dumps(line)  # must be serializable as one log line


def test_order_id_comes_from_path_and_errors_report_their_code():
    event = http_api_event("POST /orders/{orderId}/cancel", f"/orders/{ORDER_ID}/cancel", path_parameters={"orderId": ORDER_ID})
    line = orders_request_log(event, Context(), error_response(404, "ORDER_NOT_FOUND", "x", "req-1"), time.monotonic())
    assert (line["operation"], line["orderId"], line["result"]) == ("CancelOrder", ORDER_ID, "ORDER_NOT_FOUND")


def test_failed_create_has_no_order_id():
    event = http_api_event("POST /orders", "/orders", body={})
    line = orders_request_log(event, Context(), error_response(503, "CATALOG_UNAVAILABLE", "x", "req-1"), time.monotonic())
    assert line["orderId"] is None and line["result"] == "CATALOG_UNAVAILABLE"


def test_catalog_log_records_well_formed_correlation_ids_only():
    good = http_api_event("GET /products", "/products", headers={"x-correlation-id": "Abc123=="})
    bad = http_api_event("GET /products", "/products", headers={"x-correlation-id": "line\nbreak"})
    response = json_response(200, {"items": []})
    assert catalog_request_log(good, Context(), response, time.monotonic())["correlationId"] == "Abc123=="
    assert catalog_request_log(bad, Context(), response, time.monotonic())["correlationId"] is None
    assert catalog_request_log(good, Context(), response, time.monotonic())["operation"] == "ListProducts"
