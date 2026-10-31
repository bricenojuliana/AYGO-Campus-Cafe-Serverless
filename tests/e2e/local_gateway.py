"""TEST-ONLY harness: the real handlers behind a minimal HTTP API (payload 2.0) emulator with moto DynamoDB.

It exists to exercise the browser UI and the real adapters without AWS credentials. It is never
deployed and is not part of the application; the deployed stack uses API Gateway, Lambda and DynamoDB.

The Orders routes go through an EMULATED JWT authorizer: it reads the Auth0 access token's claims and
applies the gateway's checks (issuer, audience, expiry, route scope) but does NOT verify the signature.
That is acceptable only here, on a local test harness; in AWS the real authorizer verifies it.
Auth0 settings come from frontend/config.js (or config.example.js).

Usage: python tests/e2e/local_gateway.py [--api-port 3001] [--ui-port 8080] [--catalog-delay 0]
"""
import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "services/catalog/src"), str(ROOT / "services/orders/src")]
for name, value in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing",
                    "AWS_SECRET_ACCESS_KEY": "testing"}.items():
    os.environ.setdefault(name, value)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

ROUTES = [
    ("GET", re.compile(r"^/products$"), "GET /products", "catalog"),
    ("GET", re.compile(r"^/products/(?P<productId>[^/]+)$"), "GET /products/{productId}", "catalog"),
    ("POST", re.compile(r"^/orders$"), "POST /orders", "orders"),
    ("GET", re.compile(r"^/orders/(?P<orderId>[^/]+)$"), "GET /orders/{orderId}", "orders"),
    ("POST", re.compile(r"^/orders/(?P<orderId>[^/]+)/cancel$"), "POST /orders/{orderId}/cancel", "orders"),
]


def frontend_config() -> dict:
    """The public values of frontend/config.js (falls back to config.example.js)."""
    for name in ("config.js", "config.example.js"):
        path = ROOT / "frontend" / name
        if path.exists():
            return dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', path.read_text()))
    return {}


def emulated_authorizer(headers: dict, route_key: str, config: dict, route_scopes: dict):
    """TEST ONLY. Returns (status, claims): 401/403 like API Gateway, or 200 with the token's claims.
    The signature is NOT verified."""
    auth = headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not config.get("auth0Domain") or not config.get("auth0Audience"):
        return 401, None
    try:
        payload = auth[len("Bearer "):].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return 401, None
    audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
    if (claims.get("iss") != f"https://{config['auth0Domain']}/" or config["auth0Audience"] not in audiences
            or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= time.time()):
        return 401, None
    if route_scopes[route_key] not in str(claims.get("scope", "")).split():
        return 403, None
    return 200, claims


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, default=3001)
    parser.add_argument("--ui-port", type=int, default=8080)
    parser.add_argument("--catalog-delay", type=float, default=0.0, help="seconds to stall Catalog (timeout demo)")
    args = parser.parse_args()
    origin = f"http://localhost:{args.ui_port}"

    mock = mock_aws()
    mock.start()
    ddb = boto3.client("dynamodb")
    ddb.create_table(TableName="catalog", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "productId", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "productId", "AttributeType": "S"}])
    ddb.create_table(TableName="orders", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
    table = boto3.resource("dynamodb").Table("catalog")
    for product in json.loads((ROOT / "seed/products.json").read_text()):
        table.put_item(Item=product)

    # Same settings template.yaml gives the deployed functions (template defaults).
    os.environ.update({"CATALOG_TABLE_NAME": "catalog", "ORDERS_TABLE_NAME": "orders",
                       "CATALOG_BASE_URL": f"http://127.0.0.1:{args.api_port}",
                       "CATALOG_TIMEOUT_SECONDS": "2", "CATALOG_MAX_ATTEMPTS": "2", "CATALOG_BUDGET_SECONDS": "5",
                       "CURRENCY": "COP", "IDEMPOTENCY_TTL_HOURS": "24"})
    from catalog_service import handler as catalog_handler
    from orders_service import handler as orders_handler
    from orders_service.api.auth import ROUTE_SCOPES
    handlers = {"catalog": catalog_handler.lambda_handler, "orders": orders_handler.lambda_handler}
    config = frontend_config()
    print("WARNING: test harness. Orders tokens are checked WITHOUT signature verification.", file=sys.stderr)

    class Context:
        def __init__(self):
            self.deadline = time.monotonic() + 12

        def get_remaining_time_in_millis(self):
            return int((self.deadline - time.monotonic()) * 1000)

    class Api(BaseHTTPRequestHandler):
        def _cors(self):
            if self.headers.get("Origin") == origin:
                self.send_header("access-control-allow-origin", origin)

        def do_OPTIONS(self):
            self.send_response(204)
            if self.headers.get("Origin") == origin:
                self._cors()
                self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
                self.send_header("access-control-allow-headers", "content-type,idempotency-key,authorization")
            self.end_headers()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            path = urlsplit(self.path).path
            for route_method, pattern, route_key, target in ROUTES:
                match = pattern.match(path)
                if route_method == method and match:
                    break
            else:
                return self._send(404, {"content-type": "application/json"}, b'{"message":"Not Found"}')
            if target == "catalog" and args.catalog_delay:
                time.sleep(args.catalog_delay)
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length).decode() if length else None
            headers = {k.lower(): v for k, v in self.headers.items()}
            event = {
                "version": "2.0", "routeKey": route_key, "rawPath": path, "rawQueryString": "",
                "headers": headers,
                "requestContext": {"requestId": str(uuid.uuid4()), "routeKey": route_key, "stage": "$default",
                                   "http": {"method": method, "path": path}},
                "pathParameters": match.groupdict() or None, "body": body, "isBase64Encoded": False,
            }
            if target == "orders":
                status, claims = emulated_authorizer(headers, route_key, config, ROUTE_SCOPES)
                if status != 200:
                    message = b'{"message":"Unauthorized"}' if status == 401 else b'{"message":"Forbidden"}'
                    return self._send(status, {"content-type": "application/json"}, message)
                event["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
            result = handlers[target](event, Context())
            self._send(result["statusCode"], result.get("headers", {}), result.get("body", "").encode())

        def _send(self, status, headers, payload):
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self._cors()
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *a):
            sys.stderr.write("api  " + fmt % a + "\n")

    class Ui(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/config.js":
                local = {**config, "apiBaseUrl": f"http://127.0.0.1:{args.api_port}"}
                payload = f"window.CAMPUS_CAFE_CONFIG = {json.dumps(local)};".encode()
                ctype = "text/javascript"
            else:
                file = (ROOT / "frontend" / (path.lstrip("/") or "index.html")).resolve()
                if not file.is_file() or (ROOT / "frontend") not in file.parents:
                    self.send_error(404)
                    return
                payload = file.read_bytes()
                ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}.get(file.suffix, "text/plain")
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    api = ThreadingHTTPServer(("127.0.0.1", args.api_port), Api)
    ui = ThreadingHTTPServer(("127.0.0.1", args.ui_port), Ui)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    print(f"TEST HARNESS (not AWS): API http://127.0.0.1:{args.api_port}  UI {origin}", flush=True)
    ui.serve_forever()


if __name__ == "__main__":
    main()
