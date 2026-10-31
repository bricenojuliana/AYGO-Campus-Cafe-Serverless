"""Orders Lambda entry point: parses HTTP API payload 2.0 events and maps results to responses."""
import base64
import json
import logging
import os
import time
from http import HTTPStatus

from .adapters.catalog_http_client import correlation_id
from .api.auth import principal_from_event
from .api.observability import emit_metric, request_log
from .api.responses import error_response, error_status_of, json_response, request_id_of
from .domain.errors import InvalidRequest, OrderError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SAFETY_MARGIN_SECONDS = 1.0

_service = None


def required_env(name: str) -> str:
    """Settings come only from template.yaml. A missing one stops the function instead of using a guess."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable {name}; it is set by template.yaml.")
    return value


def _default_service():
    global _service
    if _service is None:
        from .adapters.catalog_http_client import HttpCatalogClient
        from .adapters.dynamodb_repository import DynamoOrderRepository
        from .application.service import OrderService
        from .domain.ids import new_order_id

        catalog = HttpCatalogClient(
            required_env("CATALOG_BASE_URL"),
            timeout_seconds=float(required_env("CATALOG_TIMEOUT_SECONDS")),
            max_attempts=int(required_env("CATALOG_MAX_ATTEMPTS")),
        )
        repository = DynamoOrderRepository.from_table_name(required_env("ORDERS_TABLE_NAME"))
        _service = OrderService(repository, catalog, id_factory=new_order_id,
                                currency=required_env("CURRENCY"),
                                idempotency_ttl_seconds=int(required_env("IDEMPOTENCY_TTL_HOURS")) * 3600)
    return _service


# Build the AWS clients in the Lambda init phase (reported as Init Duration) instead of inside the
# first request, and fail there if a setting is missing. Skipped when unit tests import this module.
if os.environ.get("ORDERS_TABLE_NAME"):
    _default_service()


def catalog_deadline(context) -> float:
    """Monotonic deadline for Catalog calls: the configured budget, never past the Lambda timeout."""
    budget = float(required_env("CATALOG_BUDGET_SECONDS"))
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
        return error_response(HTTPStatus.BAD_REQUEST, "UNSUPPORTED_PAYLOAD", "Expected API Gateway payload format 2.0.", request_id)
    route_key = event.get("routeKey", "")
    order_id = (event.get("pathParameters") or {}).get("orderId", "")
    headers = event.get("headers") or {}  # HTTP API 2.0 delivers header names in lowercase

    try:
        caller = principal_from_event(event)  # before any route work: no verified identity, no access
        if route_key == "POST /orders":
            result = service.create_order(headers.get("idempotency-key"), _json_body(event), deadline, caller)
            if result.replayed:
                emit_metric("IdempotentReplays")
                return json_response(HTTPStatus.OK, result.body)
            emit_metric("OrdersCreated")
            return json_response(HTTPStatus.CREATED, result.body, {"location": f"/orders/{result.order_id}"})
        if route_key == "GET /orders/{orderId}":
            return json_response(HTTPStatus.OK, service.get_order(order_id, caller).to_api())
        if route_key == "POST /orders/{orderId}/cancel":
            return json_response(HTTPStatus.OK, service.cancel_order(order_id, caller).to_api())
    except OrderError as exc:
        mapped = error_status_of(exc)
        if mapped.status == HTTPStatus.SERVICE_UNAVAILABLE:
            emit_metric("DependencyFailures")
            logger.warning(json.dumps({"msg": "dependency failure", "code": exc.code, "requestId": request_id,
                                       "orderId": order_id or None}))
        return error_response(mapped.status, exc.code, exc.message, request_id, mapped.headers)

    return error_response(HTTPStatus.NOT_FOUND, "ROUTE_NOT_FOUND", "No such route.", request_id)


def lambda_handler(event, context):
    started = time.monotonic()
    request_id = request_id_of(event)
    token = correlation_id.set(request_id)
    try:
        response = handle(event, _default_service(), catalog_deadline(context))
    except Exception:
        logger.exception(json.dumps({"msg": "unhandled error", "requestId": request_id}))
        response = error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "Unexpected error.", request_id)
    finally:
        correlation_id.reset(token)
    logger.info(json.dumps(request_log(event, context, response, started)))
    return response
