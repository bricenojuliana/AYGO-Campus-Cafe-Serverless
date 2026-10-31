"""Orders Lambda entry point: parses HTTP API payload 2.0 events and maps results to responses."""
import base64
import json
import logging
import os
import time

from .auth import caller_from_event
from .domain import (CatalogUnavailable, IdempotencyConflict, InvalidRequest, MissingIdempotencyKey,
                     OrderError, OrderNotFound, ProductUnavailable, StoreUnavailable, UnknownProduct)
from .http import error_response, json_response, request_id_of
from .observability import correlation_id, emit_metric, request_log

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ERROR_STATUS = {
    InvalidRequest: 400,
    MissingIdempotencyKey: 400,
    UnknownProduct: 400,
    OrderNotFound: 404,
    IdempotencyConflict: 409,
    ProductUnavailable: 409,
    CatalogUnavailable: 503,
    StoreUnavailable: 503,
}
SAFETY_MARGIN_SECONDS = 1.0

_service = None


def _default_service():
    global _service
    if _service is None:
        from .catalog_client import HttpCatalogClient
        from .ids import new_order_id
        from .repository import DynamoOrderRepository
        from .service import OrderService

        catalog = HttpCatalogClient(
            os.environ["CATALOG_BASE_URL"],
            timeout_seconds=float(os.environ.get("CATALOG_TIMEOUT_SECONDS", "2")),
            max_attempts=int(os.environ.get("CATALOG_MAX_ATTEMPTS", "2")),
        )
        repository = DynamoOrderRepository.from_table_name(os.environ["ORDERS_TABLE_NAME"])
        _service = OrderService(repository, catalog, id_factory=new_order_id)
    return _service


# Build the AWS clients in the Lambda init phase (reported as Init Duration) instead of inside the
# first request. Skipped when the environment is not configured, e.g. when unit tests import this module.
if os.environ.get("ORDERS_TABLE_NAME") and os.environ.get("CATALOG_BASE_URL"):
    _default_service()


def catalog_deadline(context) -> float:
    """Monotonic deadline for Catalog calls: the configured budget, never past the Lambda timeout."""
    budget = float(os.environ.get("CATALOG_BUDGET_SECONDS", "5"))
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        budget = min(budget, context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_SECONDS)
    return time.monotonic() + max(budget, 0)


def _json_body(event: dict):
    raw = event.get("body")
    if raw is None:
        raise InvalidRequest("The request body must be a JSON object.")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise InvalidRequest("The request body is not valid JSON.") from exc


def handle(event: dict, service, deadline: float) -> dict:
    request_id = request_id_of(event)
    if event.get("version") != "2.0":
        return error_response(400, "UNSUPPORTED_PAYLOAD", "Expected API Gateway payload format 2.0.", request_id)
    route_key = event.get("routeKey", "")
    order_id = (event.get("pathParameters") or {}).get("orderId", "")
    headers = event.get("headers") or {}  # HTTP API 2.0 delivers header names in lowercase
    caller = caller_from_event(event)

    try:
        if route_key == "POST /orders":
            result = service.create_order(headers.get("idempotency-key"), _json_body(event), deadline, caller)
            if result.replayed:
                emit_metric("IdempotentReplays")
                return json_response(200, result.body)
            emit_metric("OrdersCreated")
            return json_response(201, result.body, {"location": f"/orders/{result.order_id}"})
        if route_key == "GET /orders/{orderId}":
            return json_response(200, service.get_order(order_id, caller).to_api())
        if route_key == "POST /orders/{orderId}/cancel":
            return json_response(200, service.cancel_order(order_id, caller).to_api())
    except OrderError as exc:
        status = ERROR_STATUS.get(type(exc), 500)
        if status == 503:
            emit_metric("DependencyFailures")
            logger.warning(json.dumps({"msg": "dependency failure", "code": exc.code, "requestId": request_id,
                                       "orderId": order_id or None}))
        return error_response(status, exc.code, exc.message, request_id)

    return error_response(404, "ROUTE_NOT_FOUND", "No such route.", request_id)


def lambda_handler(event, context):
    started = time.monotonic()
    request_id = request_id_of(event)
    token = correlation_id.set(request_id)
    try:
        response = handle(event, _default_service(), catalog_deadline(context))
    except Exception:
        logger.exception(json.dumps({"msg": "unhandled error", "requestId": request_id}))
        response = error_response(500, "INTERNAL_ERROR", "Unexpected error.", request_id)
    finally:
        correlation_id.reset(token)
    logger.info(json.dumps(request_log(event, context, response, started)))
    return response
