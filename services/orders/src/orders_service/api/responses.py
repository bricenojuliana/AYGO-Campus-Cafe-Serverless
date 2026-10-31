"""HTTP side of Orders: payload 2.0 responses and how each business error is expressed over HTTP.

The domain raises business errors (domain/errors.py) without knowing about HTTP; this module is the
single place that maps them to standard status codes and headers.
"""
import json
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Mapping, Optional

from ..domain.errors import (CatalogUnavailable, IdempotencyConflict, InsufficientScope, InvalidRequest,
                             MissingIdempotencyKey, OrderError, OrderNotFound, ProductUnavailable, StoreUnavailable,
                             Unauthenticated, UnknownProduct)

JSON_HEADERS = {"content-type": "application/json"}


@dataclass(frozen=True)
class ErrorStatus:
    """How a business error is expressed over HTTP."""
    status: HTTPStatus
    headers: Mapping[str, str] = field(default_factory=dict)


ERROR_STATUS: Mapping[type, ErrorStatus] = {
    Unauthenticated: ErrorStatus(HTTPStatus.UNAUTHORIZED, {"www-authenticate": "Bearer"}),
    # RFC 6750: a valid token that lacks the scope the operation needs.
    InsufficientScope: ErrorStatus(HTTPStatus.FORBIDDEN, {"www-authenticate": 'Bearer error="insufficient_scope"'}),
    InvalidRequest: ErrorStatus(HTTPStatus.BAD_REQUEST),
    MissingIdempotencyKey: ErrorStatus(HTTPStatus.BAD_REQUEST),
    UnknownProduct: ErrorStatus(HTTPStatus.BAD_REQUEST),
    OrderNotFound: ErrorStatus(HTTPStatus.NOT_FOUND),
    IdempotencyConflict: ErrorStatus(HTTPStatus.CONFLICT),
    ProductUnavailable: ErrorStatus(HTTPStatus.CONFLICT),
    CatalogUnavailable: ErrorStatus(HTTPStatus.SERVICE_UNAVAILABLE),
    StoreUnavailable: ErrorStatus(HTTPStatus.SERVICE_UNAVAILABLE),
}
UNMAPPED_ERROR = ErrorStatus(HTTPStatus.INTERNAL_SERVER_ERROR)


def error_status_of(error: OrderError) -> ErrorStatus:
    return ERROR_STATUS.get(type(error), UNMAPPED_ERROR)


def request_id_of(event: dict) -> str:
    return str((event.get("requestContext") or {}).get("requestId", "unknown"))


def json_response(status: HTTPStatus, body: Any, headers: Optional[Mapping[str, str]] = None) -> dict:
    return {
        "statusCode": int(status),
        "headers": {**JSON_HEADERS, **(headers or {})},
        "body": json.dumps(body, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def error_response(status: HTTPStatus, code: str, message: str, request_id: str,
                   headers: Optional[Mapping[str, str]] = None) -> dict:
    return json_response(status, {"code": code, "message": message, "requestId": request_id}, headers)
