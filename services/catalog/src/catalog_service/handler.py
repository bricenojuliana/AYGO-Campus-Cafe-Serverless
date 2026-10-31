"""Catalog Lambda entry point: parses HTTP API payload 2.0 events and maps results to responses."""
import json
import logging
import os
import re
import time

from .domain import InvalidProduct, InvalidProductId, sort_for_menu, validate_product_id
from .http import error_response, json_response, request_id_of
from .repository import StoreUnavailable

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_repository = None


def _default_repository():
    global _repository
    if _repository is None:
        from .repository import DynamoProductRepository

        _repository = DynamoProductRepository.from_table_name(os.environ["CATALOG_TABLE_NAME"])
    return _repository


# Build the AWS client in the Lambda init phase (reported as Init Duration) instead of inside the
# first request. Skipped when the environment is not configured, e.g. when unit tests import this module.
if os.environ.get("CATALOG_TABLE_NAME"):
    _default_repository()


def handle(event: dict, repository) -> dict:
    request_id = request_id_of(event)
    if event.get("version") != "2.0":
        return error_response(400, "UNSUPPORTED_PAYLOAD", "Expected API Gateway payload format 2.0.", request_id)
    route_key = event.get("routeKey", "")
    path_parameters = event.get("pathParameters") or {}

    try:
        if route_key == "GET /products":
            products = sort_for_menu(repository.list_all())
            return json_response(200, {"items": [p.to_api() for p in products]})

        if route_key == "GET /products/{productId}":
            try:
                product_id = validate_product_id(path_parameters.get("productId", ""))
            except InvalidProductId:
                return error_response(404, "PRODUCT_NOT_FOUND", "This product does not exist.", request_id)
            product = repository.get(product_id)
            if product is None:
                return error_response(404, "PRODUCT_NOT_FOUND", "This product does not exist.", request_id)
            return json_response(200, product.to_api())
    except (StoreUnavailable, InvalidProduct):
        logger.exception(json.dumps({"msg": "catalog store unavailable", "requestId": request_id}))
        return error_response(503, "STORE_UNAVAILABLE", "The catalog cannot be read right now.", request_id)

    return error_response(404, "ROUTE_NOT_FOUND", "No such route.", request_id)


OPERATIONS = {"GET /products": "ListProducts", "GET /products/{productId}": "GetProduct"}
CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9=_-]{1,64}$")


def request_log(event: dict, context, response: dict, started: float) -> dict:
    status = response["statusCode"]
    result = "OK"
    if status >= 400:
        try:
            result = json.loads(response["body"]).get("code", "ERROR")
        except ValueError:
            result = "ERROR"
    # Set by Orders when it calls Catalog; untrusted input, so only logged when well-formed.
    correlation = (event.get("headers") or {}).get("x-correlation-id", "")
    return {
        "msg": "request",
        "service": "catalog",
        "operation": OPERATIONS.get(event.get("routeKey", ""), event.get("routeKey") or "Unknown"),
        "requestId": request_id_of(event),
        "lambdaRequestId": getattr(context, "aws_request_id", None),
        "correlationId": correlation if CORRELATION_PATTERN.match(correlation) else None,
        "productId": (event.get("pathParameters") or {}).get("productId"),
        "status": status,
        "result": result,
        "durationMs": round((time.monotonic() - started) * 1000, 1),
    }


def lambda_handler(event, context):
    started = time.monotonic()
    request_id = request_id_of(event)
    try:
        response = handle(event, _default_repository())
    except Exception:
        logger.exception(json.dumps({"msg": "unhandled error", "requestId": request_id}))
        response = error_response(500, "INTERNAL_ERROR", "Unexpected error.", request_id)
    logger.info(json.dumps(request_log(event, context, response, started)))
    return response
