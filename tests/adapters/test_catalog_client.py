"""Runs the real HTTP Catalog client against a local HTTP server that misbehaves on purpose."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orders_service.adapters.catalog_http_client import HttpCatalogClient
from orders_service.adapters.catalog_http_client import correlation_id
from orders_service.domain.errors import CatalogUnavailable
from orders_service.domain.order import CatalogProduct

COFFEE = {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "name": "Coffee", "priceCents": 350, "currency": "COP", "available": True}


class Behaviour:
    def __init__(self):
        self.calls = {}
        self.headers = []
        self.plan = {}  # productId -> list of (status, body, delay) consumed per call


@pytest.fixture
def server():
    behaviour = Behaviour()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            product_id = self.path.rsplit("/", 1)[-1]
            behaviour.calls[product_id] = behaviour.calls.get(product_id, 0) + 1
            behaviour.headers.append(self.headers.get("x-correlation-id"))
            steps = behaviour.plan.get(product_id) or [(404, {"code": "PRODUCT_NOT_FOUND"}, 0)]
            status, body, delay = steps.pop(0) if len(steps) > 1 else steps[0]
            time.sleep(delay)
            payload = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", behaviour
    httpd.shutdown()
    httpd.server_close()


def client(base_url, **kwargs):
    return HttpCatalogClient(base_url, **{"timeout_seconds": 0.3, "max_attempts": 2, "backoff_seconds": 0.01, **kwargs})


def test_reads_products_and_unknowns(server):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(200, COFFEE, 0)]
    result = client(base_url).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e"], time.monotonic() + 2)
    assert result == {"0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b": CatalogProduct("0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "Coffee", 350, "COP", True), "3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e": None}


def test_retries_a_5xx_once_then_succeeds(server):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(502, {}, 0), (200, COFFEE, 0)]
    assert client(base_url).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"], time.monotonic() + 2)["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"].price_cents == 350
    assert behaviour.calls["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] == 2


def test_gives_up_after_max_attempts(server):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(503, {}, 0)]
    with pytest.raises(CatalogUnavailable):
        client(base_url).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"], time.monotonic() + 2)
    assert behaviour.calls["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] == 2


def test_slow_catalog_is_bounded_by_per_request_timeout(server):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(200, COFFEE, 2.0)]
    started = time.monotonic()
    with pytest.raises(CatalogUnavailable):
        client(base_url).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"], time.monotonic() + 10)
    assert time.monotonic() - started < 1.2  # 2 attempts x 0.3 s + backoff, not 2 x 2 s


def test_overall_deadline_wins_over_attempts(server):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(200, COFFEE, 2.0)]
    started = time.monotonic()
    with pytest.raises(CatalogUnavailable):
        client(base_url, timeout_seconds=5, max_attempts=3).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"], time.monotonic() + 0.4)
    assert time.monotonic() - started < 0.8


def test_unreachable_catalog_is_unavailable():
    with pytest.raises(CatalogUnavailable):
        client("http://127.0.0.1:9").get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"], time.monotonic() + 1)


@pytest.mark.parametrize("payload", [
    {**COFFEE, "priceCents": 3.5},
    {**COFFEE, "productId": "5a8c1f6d-7b9c-4d34-8f6a-8b0c2d4e6f7a"},
    {"productId": "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"},
])
def test_unexpected_payloads_are_dependency_failures(server, payload):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(200, payload, 0)]
    with pytest.raises(CatalogUnavailable):
        client(base_url).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"], time.monotonic() + 2)


def test_rejects_non_http_base_url():
    with pytest.raises(ValueError):
        HttpCatalogClient("file:///etc/passwd", timeout_seconds=1, max_attempts=1)


def test_forwards_the_gateway_request_id_as_correlation_id(server):
    base_url, behaviour = server
    behaviour.plan["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b"] = [(200, COFFEE, 0)]
    token = correlation_id.set("gw-req-42")
    try:
        client(base_url).get_products(["0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b", "3e6a9d4b-5f7a-4b12-8d4e-6f8a0b2c4d5e"],
                                      time.monotonic() + 2)
    finally:
        correlation_id.reset(token)
    assert behaviour.headers == ["gw-req-42", "gw-req-42"]  # also from the worker threads
