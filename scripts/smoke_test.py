"""End-to-end checks against the DEPLOYED HTTP API (real API Gateway, JWT authorizer, Lambdas and DynamoDB).

Usage: python scripts/smoke_test.py [--api-url URL | --stack campus-cafe] [--origin http://localhost:8080]

Access tokens come only from environment variables (never arguments, files or output):
  CAFE_TOKEN_ALICE   API access token of Alice (orders:create orders:read orders:cancel)   required
  CAFE_TOKEN_BOB     API access token of Bob (same scopes)                                   cross-user checks
  CAFE_TOKEN_READER  API access token of a user with only orders:read                        G5 (optional)
  CAFE_ID_TOKEN      an ID token (not an access token) of any test user                      G6 (optional)
Read each one without echoing it, e.g. `read -rs CAFE_TOKEN_ALICE && export CAFE_TOKEN_ALICE`.
Token values are masked in everything this script prints.

It creates and cancels real (synthetic) orders. Every check prints PASS, FAIL or BLOCKED (a token it needs
was not provided) with its workshop ID; the exit code is non-zero if any check failed.
"""
import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TOKENS = {name: os.environ.get(f"CAFE_{name}", "").strip() for name in
          ("TOKEN_ALICE", "TOKEN_BOB", "TOKEN_READER", "ID_TOKEN")}


def redact(text) -> str:
    text = str(text)
    for value in TOKENS.values():
        if value:
            text = text.replace(value, "<redacted>")
    return text


def request(method, url, body=None, headers=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    sent = {"content-type": "application/json", **(headers or {})}
    if token:
        sent["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return response.status, {k.lower(): v for k, v in response.headers.items()}, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"raw": raw.decode(errors="replace")[:200]}
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, parsed


class Checks:
    def __init__(self):
        self.rows = []

    def check(self, check_id, name, condition, observed=""):
        result = "PASS" if condition else "FAIL"
        self.rows.append((check_id, name, result, redact(observed)[:160]))
        print(f"{result:<8}{check_id:<5}{name}" + ("" if condition else f"  observed: {redact(observed)[:300]}"))
        return condition

    def blocked(self, check_id, name, reason):
        self.rows.append((check_id, name, "BLOCKED", reason))
        print(f"BLOCKED {check_id:<5}{name}  ({reason})")

    def table(self):
        lines = ["| ID | Check | Result | Observed |", "|---|---|---|---|"]
        lines += [f"| {i} | {n} | {r} | {o.replace('|', '/')} |" for i, n, r, o in self.rows]
        return "\n".join(lines)

    @property
    def failed(self):
        return sum(r == "FAIL" for _, _, r, _ in self.rows)


def tampered(token: str) -> str:
    """Same header and claims, broken signature: a tampering test, not an expiry/audience test."""
    head, _, signature = token.rpartition(".")
    return f"{head}.{signature[::-1] if len(signature) > 1 else 'x'}"


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
    alice, bob, reader, id_token = (TOKENS[k] for k in ("TOKEN_ALICE", "TOKEN_BOB", "TOKEN_READER", "ID_TOKEN"))
    if not alice:
        sys.exit("CAFE_TOKEN_ALICE is required (an API access token of Alice). See the docstring.")
    print(f"API: {api}\n")
    c = Checks()

    # ---------- G1: public catalog ----------
    status, _, body = request("GET", f"{api}/products")
    c.check("G1", "GET /products without a token -> 200 {items}",
            status == 200 and isinstance((body or {}).get("items"), list), status)
    items = body["items"]
    c.check("G1", ">= 5 products, one unavailable, one currency",
            len(items) >= 5 and any(not p["available"] for p in items) and len({p["currency"] for p in items}) == 1)
    available = next(p for p in items if p["available"])
    unavailable = next(p for p in items if not p["available"])
    status, _, body = request("GET", f"{api}/products/{available['productId']}")
    c.check("G1", "GET /products/{id} without a token -> 200", status == 200 and body["productId"] == available["productId"])
    status, _, body = request("GET", f"{api}/products/{uuid.uuid4()}")
    c.check("G1", "GET unknown product -> 404", status == 404, status)

    # ---------- G2: no token / tampered token ----------
    order_body = {"items": [{"productId": available["productId"], "quantity": 2}]}
    some_order = f"{api}/orders/CC-0000-0000"
    for method, url, body in (("POST", f"{api}/orders", order_body), ("GET", some_order, None),
                              ("POST", f"{some_order}/cancel", None)):
        status, _, _ = request(method, url, body, {"Idempotency-Key": f"smoke-{uuid.uuid4()}"})
        c.check("G2", f"{method} {url.replace(api, '')} without a token -> 401", status == 401, status)
    status, _, _ = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": f"smoke-{uuid.uuid4()}"},
                           token=tampered(alice))
    c.check("G2", "POST /orders with a tampered token (broken signature) -> 401", status == 401, status)

    # ---------- G3 / S5: Alice creates, reads, replays ----------
    key = f"smoke-{uuid.uuid4()}"
    status, headers, created = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key}, token=alice)
    if not c.check("G3", "Alice POST /orders -> 201 CREATED with Location",
                   status == 201 and created["status"] == "CREATED"
                   and headers.get("location") == f"/orders/{created['orderId']}", (status, created)):
        print("\nCannot continue without Alice's order.\n\n" + c.table())
        sys.exit(1)
    c.check("S8", "server-side total", created["totalCents"] == available["priceCents"] * 2
            and created["currency"] == available["currency"])
    alice_url = f"{api}/orders/{created['orderId']}"
    status, _, body = request("GET", alice_url, token=alice)
    c.check("G3", "Alice GET her order -> 200, same order", status == 200 and body == created, status)

    status, _, replay = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key}, token=alice)
    c.check("S5", "Alice same key + same body -> 200 original result", status == 200 and replay == created, status)
    status, _, body = request("POST", f"{api}/orders", {"items": [{"productId": available["productId"], "quantity": 3}]},
                              {"Idempotency-Key": key}, token=alice)
    c.check("S5", "Alice same key + different body -> 409", status == 409, (status, body))

    # ---------- validation (S1, S8) ----------
    status, _, body = request("POST", f"{api}/orders", order_body, token=alice)
    c.check("S1", "missing Idempotency-Key -> 400", status == 400, status)
    status, _, body = request("POST", f"{api}/orders", {"items": [{"productId": str(uuid.uuid4()), "quantity": 1}]},
                              {"Idempotency-Key": f"smoke-{uuid.uuid4()}"}, token=alice)
    c.check("S8", "unknown product -> 400", status == 400, status)
    status, _, body = request("POST", f"{api}/orders", {"items": [{"productId": unavailable["productId"], "quantity": 1}]},
                              {"Idempotency-Key": f"smoke-{uuid.uuid4()}"}, token=alice)
    c.check("S8", "unavailable product -> 409 PRODUCT_UNAVAILABLE",
            status == 409 and body["code"] == "PRODUCT_UNAVAILABLE", (status, body))
    pid = available["productId"]
    invalid_bodies = {
        ("S8", "price override in line"): {"items": [{"productId": pid, "quantity": 1, "priceCents": 1}]},
        ("S8", "total override"): {"items": [{"productId": pid, "quantity": 1}], "totalCents": 1},
        ("S1", "forged owner field"): {"items": [{"productId": pid, "quantity": 1}], "ownerSub": "auth0|bob"},
        ("S1", "forged customerId"): {"items": [{"productId": pid, "quantity": 1}], "customerId": "bob"},
        ("S8", "quantity 0"): {"items": [{"productId": pid, "quantity": 0}]},
        ("S8", "quantity 11"): {"items": [{"productId": pid, "quantity": 11}]},
        ("S8", "fractional quantity"): {"items": [{"productId": pid, "quantity": 1.5}]},
        ("S8", "duplicate productId"): {"items": [{"productId": pid, "quantity": 1}, {"productId": pid, "quantity": 2}]},
        ("S8", "11 distinct lines"): {"items": [{"productId": str(uuid.uuid4()), "quantity": 1} for _ in range(11)]},
        ("S8", "empty items"): {"items": []},
        ("S8", "malformed productId"): {"items": [{"productId": "../products", "quantity": 1}]},
    }
    for (check_id, name), invalid in invalid_bodies.items():
        status, _, body = request("POST", f"{api}/orders", invalid, {"Idempotency-Key": f"smoke-{uuid.uuid4()}"},
                                  token=alice)
        c.check(check_id, f"rejects {name} -> 400", status == 400 and body["code"] == "INVALID_REQUEST", (status, body))

    # ---------- S7: concurrent identical submissions ----------
    concurrent_key = f"smoke-{uuid.uuid4()}"
    barrier = threading.Barrier(5)

    def concurrent_submit(_):
        barrier.wait()
        return request("POST", f"{api}/orders", order_body, {"Idempotency-Key": concurrent_key}, token=alice)

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(concurrent_submit, range(5)))
    statuses = sorted(status for status, _, _ in outcomes)
    order_ids = {(body or {}).get("orderId") for _, _, body in outcomes}
    c.check("S7", "5 concurrent identical submissions -> one 201, four 200, one orderId",
            statuses == [200, 200, 200, 200, 201] and len(order_ids) == 1, (statuses, len(order_ids)))

    # ---------- G3 / G4 / S6: Bob ----------
    if bob:
        status, _, bob_order = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key}, token=bob)
        c.check("S6", "Bob uses Alice's client key -> his own new order (201), never Alice's",
                status == 201 and bob_order["orderId"] != created["orderId"], status)
        bob_url = f"{api}/orders/{bob_order['orderId']}"
        status, _, body = request("GET", bob_url, token=bob)
        c.check("G3", "Bob GET his order -> 200", status == 200 and body == bob_order, status)

        status, _, body = request("GET", alice_url, token=bob)
        c.check("G4", "Bob GET Alice's order -> 404, no order data",
                status == 404 and set(body) == {"code", "message", "requestId"}, (status, body))
        status, _, body = request("POST", f"{alice_url}/cancel", token=bob)
        c.check("G4", "Bob cancels Alice's order -> 404", status == 404, (status, body))
        status, _, body = request("GET", alice_url, token=alice)
        c.check("G4", "Alice's order unchanged after Bob's attempt", status == 200 and body == created, (status, body))
        status, _, body = request("GET", bob_url, token=alice)
        c.check("G4", "Alice GET Bob's order -> 404", status == 404, status)
        missing = "CC-" + uuid.uuid4().hex[:4].upper().translate(str.maketrans("ILOU", "1100")) + "-ZZZZ"
        _, _, missing_body = request("GET", f"{api}/orders/{missing}", token=bob)
        same_shape = {k: v for k, v in (body or {}).items() if k != "requestId"} == \
            {k: v for k, v in (missing_body or {}).items() if k != "requestId"}
        c.check("S2", "foreign and missing orders get the same 404 body", same_shape, (body, missing_body))
    else:
        for check_id, name in (("S6", "Bob uses Alice's client key"), ("G3", "Bob creates/reads his order"),
                               ("G4", "Bob reads/cancels Alice's order; Alice reads Bob's")):
            c.blocked(check_id, name, "CAFE_TOKEN_BOB not set")

    # ---------- S3: Alice cancels her order ----------
    status, _, first = request("POST", f"{alice_url}/cancel", token=alice)
    status2, _, second = request("POST", f"{alice_url}/cancel", token=alice)
    c.check("S3", "Alice cancel -> 200 CANCELLED, repeat identical",
            status == status2 == 200 and first["status"] == "CANCELLED" and first == second, (status, status2))
    status, _, body = request("GET", alice_url, token=alice)
    c.check("S3", "cancellation persisted", status == 200 and body["status"] == "CANCELLED", status)
    status, _, replay = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": key}, token=alice)
    c.check("S5", "replay still returns the stored creation result", status == 200 and replay["status"] == "CREATED", status)
    missing = "CC-" + uuid.uuid4().hex[:4].upper().translate(str.maketrans("ILOU", "1100")) + "-ZZZZ"
    c.check("S2", "Alice GET missing order -> 404", request("GET", f"{api}/orders/{missing}", token=alice)[0] == 404)
    c.check("S3", "cancel missing order -> 404 and nothing created",
            request("POST", f"{api}/orders/{missing}/cancel", token=alice)[0] == 404
            and request("GET", f"{api}/orders/{missing}", token=alice)[0] == 404)

    # ---------- G5: valid token without the route's scope ----------
    if reader:
        status, _, _ = request("POST", f"{api}/orders", order_body, {"Idempotency-Key": f"smoke-{uuid.uuid4()}"},
                               token=reader)
        c.check("G5", "orders:read-only token POST /orders -> 403", status == 403, status)
        status, _, _ = request("POST", f"{alice_url}/cancel", token=reader)
        c.check("G5", "orders:read-only token cancel -> 403", status == 403, status)
        status, _, _ = request("GET", alice_url, token=reader)
        c.check("G5", "orders:read-only token GET someone else's order -> 404 (scope is not ownership)",
                status == 404, status)
    else:
        c.blocked("G5", "valid token lacking the route's scope", "CAFE_TOKEN_READER not set")

    # ---------- G6: ID token is not an API credential ----------
    if id_token:
        status, _, _ = request("GET", alice_url, token=id_token)
        c.check("G6", "ID token instead of the access token -> rejected (not 2xx)", not 200 <= status < 300, status)
    else:
        c.blocked("G6", "ID token instead of the access token", "CAFE_ID_TOKEN not set")

    # ---------- CORS (not authentication) ----------
    status, headers, _ = request("OPTIONS", f"{api}/orders", headers={
        "Origin": args.origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key"})
    allowed = headers.get("access-control-allow-headers", "").lower()
    c.check("CORS", "preflight allows the frontend origin and the Authorization header",
            status in (200, 204) and headers.get("access-control-allow-origin") == args.origin
            and "authorization" in allowed, (status, headers.get("access-control-allow-origin"), allowed))
    status, headers, _ = request("OPTIONS", f"{api}/orders", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    c.check("CORS", "preflight does not allow other origins",
            headers.get("access-control-allow-origin") not in ("*", "https://evil.example"), headers.get("access-control-allow-origin"))

    passed = sum(r == "PASS" for _, _, r, _ in c.rows)
    blocked = sum(r == "BLOCKED" for _, _, r, _ in c.rows)
    print(f"\n{passed} passed, {c.failed} failed, {blocked} blocked against {api}\n")
    print(c.table())
    sys.exit(1 if c.failed else 0)


if __name__ == "__main__":
    main()
