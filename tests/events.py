"""Builds API Gateway HTTP API payload format 2.0 events with the real field layout."""
import json
from typing import Optional


def http_api_event(route_key: str, path: str, *, path_parameters: Optional[dict] = None,
                   body: Optional[object] = None, headers: Optional[dict] = None) -> dict:
    method = route_key.split(" ", 1)[0]
    return {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json", **{k.lower(): v for k, v in (headers or {}).items()}},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "api123",
            "domainName": "api123.execute-api.us-east-1.amazonaws.com",
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1",
                     "sourceIp": "127.0.0.1", "userAgent": "pytest"},
            "requestId": "req-1",
            "routeKey": route_key,
            "stage": "$default",
            "timeEpoch": 1767225600000,
        },
        "pathParameters": path_parameters,
        "body": json.dumps(body) if body is not None and not isinstance(body, str) else body,
        "isBase64Encoded": False,
    }
