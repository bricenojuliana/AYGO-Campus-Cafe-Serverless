"""End-to-end checks against the DEPLOYED HTTP API (real API Gateway, Lambdas and DynamoDB).

Usage: python scripts/smoke_test.py [--api-url URL | --stack campus-cafe] [--origin http://localhost:8080]
It creates and cancels real orders. Exits non-zero on the first failed check.
"""
import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def request(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return response.status, {k.lower(): v for k, v in response.headers.items()}, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, json.loads(raw) if raw else None


class Checks:
    def __init__(self):
        self.passed = 0

    def check(self, name, condition, detail=""):
        if not condition:
            print(f"FAIL  {name} {detail}")
            sys.exit(1)
        self.passed += 1
        print(f"ok    {name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url")
    parser.add_argument("--stack", default="campus-cafe")
    parser.add_argument("--region")
    parser.add_argument("--origin", default="http://localhost:8080")
    args = parser.parse_args()
    if args.api_url:
        api = args.api_url.rstrip("/")
    else:
        from stack_outputs import stack_outputs
        api = stack_outputs(args.stack, args.region)["ApiUrl"].rstrip("/")
    print(f"API: {api}")
    c = Checks()

    status, _, body = request("GET", f"{api}/products")
    c.check("GET /products -> 200 {items}", status == 200 and isinstance(body.get("items"), list), body)
    items = body["items"]
    c.check(">= 5 products, one unavailable, one currency",
            len(items) >= 5 and any(not p["available"] for p in items) and len({p["currency"] for p in items}) == 1)
    available = next(p for p in items if p["available"])
    unavailable = next(p for p in items if not p["available"])

    status, _, body = request("GET", f"{api}/products/{available['productId']}")
    c.check("GET /products/{id} -> 200 product", status == 200 and body["productId"] == available["productId"])
    status, _, body = request("GET", f"{api}/products/{uuid.uuid4()}")
    c.check("GET unknown product -> 404", status == 404 and body["code"] and body["requestId"])

    order_body = {"items": [{"productId": available["productId"], "quantity": 2}]}
    key = f"smoke-{uuid.uuid4()}"
    status, headers, created = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key})
    c.check("POST /orders -> 201 CREATED with Location",
            status == 201 and created["status"] == "CREATED" and headers.get("location") == f"/orders/{created['orderId']}",
            (status, created))
    c.check("server-side total", created["totalCents"] == available["priceCents"] * 2 and created["currency"] == available["currency"])

    status, _, replay = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key})
    c.check("replay same key -> 200 original result", status == 200 and replay == created, (status, replay))
    status, _, body = request("POST", f"{api}/orders", {"items": [{"productId": available["productId"], "quantity": 3}]},
                              {"Idempotency-Key": key})
    c.check("same key, different request -> 409", status == 409, (status, body))
    status, _, body = request("POST", f"{api}/orders", order_body)
    c.check("missing Idempotency-Key -> 400", status == 400, (status, body))
    status, _, body = request("POST", f"{api}/orders", {"items": [{"productId": str(uuid.uuid4()), "quantity": 1}]},
                              {"Idempotency-Key": f"smoke-{uuid.uuid4()}"})
    c.check("unknown product -> 400", status == 400, (status, body))
    status, _, body = request("POST", f"{api}/orders", {"items": [{"productId": unavailable["productId"], "quantity": 1}]},
                              {"Idempotency-Key": f"smoke-{uuid.uuid4()}"})
    c.check("unavailable product -> 409 PRODUCT_UNAVAILABLE", status == 409 and body["code"] == "PRODUCT_UNAVAILABLE", (status, body))

    pid = available["productId"]
    invalid_bodies = {
        "price override in line": {"items": [{"productId": pid, "quantity": 1, "priceCents": 1}]},
        "total override": {"items": [{"productId": pid, "quantity": 1}], "totalCents": 1},
        "quantity 0": {"items": [{"productId": pid, "quantity": 0}]},
        "quantity 11": {"items": [{"productId": pid, "quantity": 11}]},
        "fractional quantity": {"items": [{"productId": pid, "quantity": 1.5}]},
        "duplicate productId": {"items": [{"productId": pid, "quantity": 1}, {"productId": pid, "quantity": 2}]},
        "11 distinct lines": {"items": [{"productId": str(uuid.uuid4()), "quantity": 1} for _ in range(11)]},
        "empty items": {"items": []},
        "malformed productId": {"items": [{"productId": "../products", "quantity": 1}]},
    }
    for name, invalid in invalid_bodies.items():
        status, _, body = request("POST", f"{api}/orders", invalid, {"Idempotency-Key": f"smoke-{uuid.uuid4()}"})
        c.check(f"rejects {name} -> 400", status == 400 and body["code"] == "INVALID_REQUEST", (status, body))

    concurrent_key = f"smoke-{uuid.uuid4()}"
    barrier = threading.Barrier(5)

    def concurrent_submit(_):
        barrier.wait()
        return request("POST", f"{api}/orders", order_body, {"Idempotency-Key": concurrent_key})

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(concurrent_submit, range(5)))
    statuses = sorted(status for status, _, _ in outcomes)
    order_ids = {body.get("orderId") for _, _, body in outcomes}
    c.check("5 concurrent identical submissions -> one 201, four 200, one orderId",
            statuses == [200, 200, 200, 200, 201] and len(order_ids) == 1, (statuses, order_ids))

    order_url = f"{api}/orders/{created['orderId']}"
    status, _, body = request("GET", order_url)
    c.check("GET /orders/{id} -> 200", status == 200 and body == created)
    status, _, first = request("POST", f"{order_url}/cancel")
    status2, _, second = request("POST", f"{order_url}/cancel")
    c.check("cancel -> 200 CANCELLED, repeat identical", status == status2 == 200 and first["status"] == "CANCELLED" and first == second)
    status, _, body = request("GET", order_url)
    c.check("cancellation persisted", body["status"] == "CANCELLED")
    status, _, replay = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key})
    c.check("replay still returns stored creation result", status == 200 and replay["status"] == "CREATED")

    missing = "CC-" + uuid.uuid4().hex[:4].upper().translate(str.maketrans("ILOU", "1100")) + "-ZZZZ"
    c.check("GET missing order -> 404", request("GET", f"{api}/orders/{missing}")[0] == 404)
    c.check("cancel missing order -> 404", request("POST", f"{api}/orders/{missing}/cancel")[0] == 404)
    c.check("cancel did not create it", request("GET", f"{api}/orders/{missing}")[0] == 404)

    status, headers, _ = request("OPTIONS", f"{api}/orders", headers={
        "Origin": args.origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,idempotency-key"})
    c.check("CORS preflight allows the frontend origin",
            status in (200, 204) and headers.get("access-control-allow-origin") == args.origin, headers)
    status, headers, _ = request("OPTIONS", f"{api}/orders", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    c.check("CORS preflight does not allow other origins",
            headers.get("access-control-allow-origin") not in ("*", "https://evil.example"), headers)

    print(f"\n{c.passed} checks passed against {api}")


if __name__ == "__main__":
    main()
