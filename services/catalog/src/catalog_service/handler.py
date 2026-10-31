"""Catalog Lambda entry point: parses HTTP API payload 2.0 events and maps results to responses."""
import json
import logging
import os
import time
from http import HTTPStatus

from .adapters.dynamodb_repository import StoreUnavailable
from .api.observability import request_log
from .api.responses import error_response, json_response, request_id_of
from .domain.product import InvalidProduct, InvalidProductId, sort_for_menu, validate_product_id

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_repository = None


def required_env(name: str) -> str:
    """Settings come only from template.yaml. A missing one stops the function instead of using a guess."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable {name}; it is set by template.yaml.")
    return value


def _default_repository():
    global _repository
    if _repository is None:
        from .adapters.dynamodb_repository import DynamoProductRepository

        _repository = DynamoProductRepository.from_table_name(required_env("CATALOG_TABLE_NAME"),
                                                              currency=required_env("CURRENCY"))
    return _repository


# Build the AWS client in the Lambda init phase (reported as Init Duration) instead of inside the
# first request, and fail there if a setting is missing. Skipped when unit tests import this module.
if os.environ.get("CATALOG_TABLE_NAME"):
    _default_repository()


def handle(event: dict, repository) -> dict:
    request_id = request_id_of(event)
    if event.get("version") != "2.0":
        return error_response(HTTPStatus.BAD_REQUEST, "UNSUPPORTED_PAYLOAD", "Expected API Gateway payload format 2.0.", request_id)
    route_key = event.get("routeKey", "")
    path_parameters = event.get("pathParameters") or {}

    try:
        if route_key == "GET /products":
            products = sort_for_menu(repository.list_all())
            return json_response(HTTPStatus.OK, {"items": [p.to_api() for p in products]})

        if route_key == "GET /products/{productId}":
            try:
                product_id = validate_product_id(path_parameters.get("productId", ""))
            except InvalidProductId:
                return error_response(HTTPStatus.NOT_FOUND, "PRODUCT_NOT_FOUND", "This product does not exist.", request_id)
            product = repository.get(product_id)
            if product is None:
                return error_response(HTTPStatus.NOT_FOUND, "PRODUCT_NOT_FOUND", "This product does not exist.", request_id)
            return json_response(HTTPStatus.OK, product.to_api())
    except (StoreUnavailable, InvalidProduct):
        logger.exception(json.dumps({"msg": "catalog store unavailable", "requestId": request_id}))
        return error_response(HTTPStatus.SERVICE_UNAVAILABLE, "STORE_UNAVAILABLE", "The catalog cannot be read right now.", request_id)

    return error_response(HTTPStatus.NOT_FOUND, "ROUTE_NOT_FOUND", "No such route.", request_id)


def lambda_handler(event, context):
    started = time.monotonic()
    request_id = request_id_of(event)
    try:
        response = handle(event, _default_repository())
    except Exception:
        logger.exception(json.dumps({"msg": "unhandled error", "requestId": request_id}))
        response = error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Unexpected error.", request_id)
    logger.info(json.dumps(request_log(event, context, response, started)))
    return response
