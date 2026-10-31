"""Structured request log for Catalog: one JSON line per request, correlated with the gateway and Orders."""
import json
import re
import time

from .responses import request_id_of

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
