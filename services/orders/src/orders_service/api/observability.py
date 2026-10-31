"""Structured request logs and CloudWatch Embedded Metric Format metrics."""
import json
import os
import time

OPERATIONS = {
    "POST /orders": "CreateOrder",
    "GET /orders/{orderId}": "GetOrder",
    "POST /orders/{orderId}/cancel": "CancelOrder",
}



def request_log(event: dict, context, response: dict, started: float) -> dict:
    """One JSON line per request: correlates the gateway access log (requestId) with this Lambda."""
    body = {}
    try:
        body = json.loads(response.get("body") or "{}")
    except ValueError:
        pass
    status = response["statusCode"]
    route_key = event.get("routeKey", "")
    order_id = (event.get("pathParameters") or {}).get("orderId") or (body.get("orderId") if status < 400 else None)
    return {
        "msg": "request",
        "service": "orders",
        "operation": OPERATIONS.get(route_key, route_key or "Unknown"),
        "requestId": (event.get("requestContext") or {}).get("requestId"),
        "lambdaRequestId": getattr(context, "aws_request_id", None),
        "orderId": order_id,
        "status": status,
        "result": "OK" if status < 400 else body.get("code", "ERROR"),
        "durationMs": round((time.monotonic() - started) * 1000, 1),
    }


def emit_metric(name: str, value: float = 1, unit: str = "Count") -> None:
    namespace = os.environ.get("METRICS_NAMESPACE", "CampusCafe")
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{"Namespace": namespace, "Dimensions": [["Service"]],
                                   "Metrics": [{"Name": name, "Unit": unit}]}],
        },
        "Service": "orders",
        name: value,
    }))
