"""TEST-ONLY harness: the real handlers behind a minimal HTTP API (payload 2.0) emulator with moto DynamoDB.

It exists to exercise the browser UI and the real adapters without AWS credentials. It is never
deployed and is not part of the application; the deployed stack uses API Gateway, Lambda and DynamoDB.

Usage: python tests/e2e/local_gateway.py [--api-port 3001] [--ui-port 8080] [--catalog-delay 0]
"""
import argparse
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

    os.environ.update({"CATALOG_TABLE_NAME": "catalog", "ORDERS_TABLE_NAME": "orders",
                       "CATALOG_BASE_URL": f"http://127.0.0.1:{args.api_port}"})
    from catalog_service import handler as catalog_handler
    from orders_service import handler as orders_handler
    handlers = {"catalog": catalog_handler.lambda_handler, "orders": orders_handler.lambda_handler}

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
                self.send_header("access-control-allow-headers", "content-type,idempotency-key")
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
            event = {
                "version": "2.0", "routeKey": route_key, "rawPath": path, "rawQueryString": "",
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "requestContext": {"requestId": str(uuid.uuid4()), "routeKey": route_key, "stage": "$default",
                                   "http": {"method": method, "path": path}},
                "pathParameters": match.groupdict() or None, "body": body, "isBase64Encoded": False,
            }
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
                payload = f'window.CAMPUS_CAFE_CONFIG = {{ apiBaseUrl: "http://127.0.0.1:{args.api_port}" }};'.encode()
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
