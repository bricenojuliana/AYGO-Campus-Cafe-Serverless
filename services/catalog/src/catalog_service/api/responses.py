"""Helpers for API Gateway HTTP API payload format 2.0 responses."""
import json
from http import HTTPStatus
from typing import Any, Mapping, Optional

JSON_HEADERS = {"content-type": "application/json"}


def request_id_of(event: dict) -> str:
    return str((event.get("requestContext") or {}).get("requestId", "unknown"))


def json_response(status: HTTPStatus, body: Any, headers: Optional[Mapping[str, str]] = None) -> dict:
    return {
        "statusCode": int(status),
        "headers": {**JSON_HEADERS, **(headers or {})},
        "body": json.dumps(body, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def error_response(status: HTTPStatus, code: str, message: str, request_id: str) -> dict:
    return json_response(status, {"code": code, "message": message, "requestId": request_id})
