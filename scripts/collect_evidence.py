"""Collects quality-attribute evidence from the DEPLOYED stack and writes docs/evidence/<timestamp>.md.

Usage: python scripts/collect_evidence.py [--stack campus-cafe] [--region us-east-1]

Uses your local AWS profile, and Alice's API access token from CAFE_TOKEN_ALICE (see scripts/smoke_test.py;
never printed). It creates synthetic orders and TEMPORARILY changes the Orders function's
environment (a revision marker, then an unreachable Catalog URL); the original environment is always
restored at the end. Those changes are outside CloudFormation; the next `sam deploy` also resets them.
"""
import argparse
import json
import math
import re
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import TOKENS, redact, request  # noqa: E402
from stack_outputs import DEFAULT_STACK, stack_outputs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UNREACHABLE = {"dns failure": "https://catalog-unreachable.invalid", "timeout (blackhole)": "https://10.255.255.1"}


class Report:
    def __init__(self):
        self.lines = []
        self.failures = 0

    def h(self, title):
        self.lines.append(f"\n## {title}\n")
        print(f"\n== {title}")

    def line(self, text=""):
        text = redact(text)  # defense in depth: no bearer token can reach the report or the console
        self.lines.append(text)
        print(text)

    def check(self, name, ok, detail=""):
        self.failures += 0 if ok else 1
        self.line(f"- {'PASS' if ok else 'FAIL'} — {name}" + (f" — `{detail}`" if detail else ""))


def timed(method, url, body=None, headers=None, token=None):
    started = time.perf_counter()
    status, response_headers, payload = request(method, url, body, headers, token=token)
    return status, response_headers, payload, (time.perf_counter() - started) * 1000


def p95(values):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]  # nearest-rank


def update_env(lam, function_name, variables):
    lam.update_function_configuration(FunctionName=function_name, Environment={"Variables": variables})
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_name)


def count_orders_since(ddb, table, since_iso):
    total, kwargs = 0, {}
    while True:
        page = ddb.scan(TableName=table, ConsistentRead=True, Select="COUNT",
                        FilterExpression="entityType = :o AND createdAt >= :t",
                        ExpressionAttributeValues={":o": {"S": "ORDER"}, ":t": {"S": since_iso}}, **kwargs)
        total += page["Count"]
        if "LastEvaluatedKey" not in page:
            return total
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def find_log(logs, group, needle, since_ms, attempts=12):
    for _ in range(attempts):
        events = logs.filter_log_events(logGroupName=group, startTime=since_ms, filterPattern=f'"{needle}"').get("events", [])
        if events:
            return [e["message"].strip() for e in events]
        time.sleep(5)
    return []


# What the browser may receive: the API URL and the public Auth0 SPA settings. Never a client secret,
# AWS keys or any other credential.
FRONTEND_CONFIG_KEYS = {"apiBaseUrl", "auth0Domain", "auth0ClientId", "auth0Audience"}


def inspect_frontend_config(config_js: str) -> tuple[bool, set]:
    """Returns (only allowed public keys and no secrets, keys found) for a frontend/config.js text."""
    without_strings = re.sub(r"\"[^\"]*\"|'[^']*'", '""', config_js)
    code = re.sub(r"//[^\n]*", "", without_strings)  # comments removed after strings, so "https://" is safe
    keys = set(re.findall(r"(\w+)\s*:", code))
    has_secret = (re.search(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", config_js)
                  or re.search(r"(?i)(secret|password|token)\w*", code))
    return (not has_secret and keys <= FRONTEND_CONFIG_KEYS and "apiBaseUrl" in keys), keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default=DEFAULT_STACK)
    parser.add_argument("--region")
    args = parser.parse_args()
    alice = TOKENS["TOKEN_ALICE"]
    if not alice:
        sys.exit("CAFE_TOKEN_ALICE is required (an API access token of Alice); see scripts/smoke_test.py.")

    session = boto3.session.Session(region_name=args.region)
    region = session.region_name
    out = stack_outputs(args.stack, region)
    api = out["ApiUrl"].rstrip("/")
    lam, ddb, logs, iam = (session.client(n) for n in ("lambda", "dynamodb", "logs", "iam"))
    orders_fn, catalog_fn, orders_table = out["OrdersFunctionName"], out["CatalogFunctionName"], out["OrdersTableName"]
    orders_cfg = lam.get_function_configuration(FunctionName=orders_fn)
    original_env = dict(orders_cfg["Environment"]["Variables"])
    run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_started_ms = int(time.time() * 1000)
    r = Report()

    r.line(f"# Campus Café — quality evidence ({run_started})\n")
    r.line(f"- Account: `{session.client('sts').get_caller_identity()['Account']}`  Region: `{region}`")
    r.line(f"- Stack: `{args.stack}`  API: `{api}`")
    r.line(f"- Runtime: Orders `{orders_cfg['Runtime']}` / Catalog `{lam.get_function_configuration(FunctionName=catalog_fn)['Runtime']}`, "
           f"architecture `{orders_cfg['Architectures'][0]}`, memory {orders_cfg['MemorySize']} MB, timeout {orders_cfg['Timeout']} s")

    products = request("GET", f"{api}/products")[2]["items"]
    product = next(p for p in products if p["available"])
    body = {"items": [{"productId": product["productId"], "quantity": 2}]}

    try:
        # ---------------- Reliability: concurrency ----------------
        r.h("Reliability — 5 concurrent identical creation requests")
        key = f"evidence-{uuid.uuid4()}"
        before = count_orders_since(ddb, orders_table, run_started)
        barrier = threading.Barrier(5)

        def submit(_):
            barrier.wait()
            return request("POST", f"{api}/orders", body, {"Idempotency-Key": key}, token=alice)

        with ThreadPoolExecutor(max_workers=5) as pool:
            outcomes = list(pool.map(submit, range(5)))
        statuses = sorted(o[0] for o in outcomes)
        ids = {o[2].get("orderId") for o in outcomes}
        created = next(o for o in outcomes if o[0] == 201) if 201 in statuses else outcomes[0]
        order = created[2]
        gateway_request_id = created[1].get("apigw-requestid")
        stored = count_orders_since(ddb, orders_table, run_started) - before
        r.check("statuses are one 201 and four 200", statuses == [200, 200, 200, 200, 201], statuses)
        r.check("one order ID returned to all five", len(ids) == 1, ids)
        r.check("one stored order created by this run", stored == 1, f"orders stored: {stored}")
        item = ddb.get_item(TableName=orders_table, Key={"pk": {"S": f"ORDER#{order.get('orderId')}"}},
                            ConsistentRead=True).get("Item", {})
        owner_iss, owner_sub = item.get("ownerIss", {}).get("S"), item.get("ownerSub", {}).get("S", "")
        r.check("persisted owner is the token's issuer + subject (G3)",
                owner_iss == out.get("Auth0Issuer") and bool(owner_sub),
                f"ownerIss={owner_iss} ownerSub={owner_sub[:10]}…")  # subject shortened in shared evidence
        r.check("server-calculated total", order.get("totalCents") == product["priceCents"] * 2,
                f"{order.get('totalCents')} = 2 x {product['priceCents']} {order.get('currency')}")

        # ---------------- Reliability + persistence after a configuration update ----------------
        r.h("Reliability & persistence — after a Lambda configuration update")
        update_env(lam, orders_fn, {**original_env, "EVIDENCE_REVISION": run_started})
        r.line(f"Updated `{orders_fn}` environment (EVIDENCE_REVISION={run_started}); new execution environments start.")
        status, _, replay, cold_ms = timed("POST", f"{api}/orders", body, {"Idempotency-Key": key}, token=alice)
        r.check("replay after update returns the original creation result", status == 200 and replay == order, status)
        r.line(f"- First request after the update (includes cold start): {cold_ms:.0f} ms")
        status, _, fetched, _ = timed("GET", f"{api}/orders/{order['orderId']}", token=alice)
        r.check("same order retrievable after update (consistent read)", status == 200 and fetched["orderId"] == order["orderId"], status)

        # ---------------- Performance ----------------
        r.h("Performance — GET /orders/{orderId}, 3 warm-up + 20 sequential")
        for _ in range(3):
            timed("GET", f"{api}/orders/{order['orderId']}", token=alice)
        samples = [timed("GET", f"{api}/orders/{order['orderId']}", token=alice)[3] for _ in range(20)]
        r.line("Timings (ms): " + ", ".join(f"{s:.0f}" for s in samples))
        r.line(f"- min {min(samples):.0f} · median {statistics.median(samples):.0f} · p95 {p95(samples):.0f} · max {max(samples):.0f} ms "
               f"(client-side, from this machine to `{region}`)")
        r.check("p95 below 2000 ms", p95(samples) < 2000, f"{p95(samples):.0f} ms")
        inits = find_log(logs, out["OrdersLogGroupName"], "Init Duration",
                         run_started_ms, attempts=3)
        if inits:
            r.line("- Observed cold starts (Lambda REPORT lines, reported separately from p95):")
            for line in inits[:5]:
                r.line(f"  - `{line[:220]}`")
        else:
            r.line("- No REPORT line with Init Duration found yet (logs can lag); check CloudWatch.")

        # ---------------- Observability ----------------
        r.h("Observability — one order from the gateway access log to application logs")
        r.line(f"Gateway request ID of the 201 response (`apigw-requestid` header): `{gateway_request_id}`")
        if gateway_request_id:
            access = find_log(logs, out["ApiAccessLogGroupName"], gateway_request_id, run_started_ms)
            orders_logs = find_log(logs, out["OrdersLogGroupName"], gateway_request_id, run_started_ms)
            catalog_logs = find_log(logs, out["CatalogLogGroupName"], gateway_request_id, run_started_ms)
            r.check("access log entry found", bool(access), (access or [""])[0][:300])
            r.check("Orders log with same requestId and orderId",
                    any(order["orderId"] in line for line in orders_logs), (orders_logs or [""])[-1][:300])
            r.check("Catalog log with correlationId = that requestId", bool(catalog_logs), (catalog_logs or [""])[0][:300])

        # ---------------- Fault isolation ----------------
        r.h("Fault isolation — Catalog unreachable (temporary configuration)")
        for label, url in UNREACHABLE.items():
            update_env(lam, orders_fn, {**original_env, "CATALOG_BASE_URL": url})
            fault_key = f"evidence-fault-{uuid.uuid4()}"
            count_before = count_orders_since(ddb, orders_table, run_started)
            status, _, payload, elapsed = timed("POST", f"{api}/orders", body, {"Idempotency-Key": fault_key}, token=alice)
            record = ddb.get_item(TableName=orders_table, Key={"pk": {"S": f"IDEMPOTENCY#{fault_key}"}}, ConsistentRead=True)
            count_after = count_orders_since(ddb, orders_table, run_started)
            r.check(f"{label}: 503 CATALOG_UNAVAILABLE", status == 503 and payload.get("code") == "CATALOG_UNAVAILABLE",
                    f"{status} {payload} in {elapsed:.0f} ms")
            r.check(f"{label}: no idempotency record and no new order", "Item" not in record and count_after == count_before,
                    f"orders {count_before} -> {count_after}")
            r.check(f"{label}: answered within the Lambda timeout", elapsed < orders_cfg["Timeout"] * 1000, f"{elapsed:.0f} ms")
    finally:
        update_env(lam, orders_fn, original_env)
        r.line(f"\nRestored the original environment of `{orders_fn}`.")

    status, _, after_restore, _ = timed("POST", f"{api}/orders", body, {"Idempotency-Key": f"evidence-{uuid.uuid4()}"},
                                        token=alice)
    r.check("orders work again after restoring configuration", status == 201, status)

    # ---------------- Security ----------------
    r.h("Security — browser configuration and role policies")
    # config.js is generated per deployment and not committed; fall back to the committed example.
    config_path = next(p for p in (ROOT / "frontend" / "config.js", ROOT / "frontend" / "config.example.js")
                       if p.exists())
    r.line(f"- Inspected `frontend/{config_path.name}`")
    ok, config_keys = inspect_frontend_config(config_path.read_text())
    r.check("frontend config holds only public values: API URL and Auth0 SPA settings (no secrets)",
            ok, f"keys: {sorted(config_keys)}")
    r.line(f"- Execution role mode (stack output): {out.get('ExecutionRoleMode', 'unknown')}")
    for fn in (catalog_fn, orders_fn):
        role = lam.get_function_configuration(FunctionName=fn)["Role"].split("/")[-1]
        try:
            attached = iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]
            r.line(f"\n`{fn}` → role `{role}`; managed policies: {[p['PolicyName'] for p in attached] or 'none'}")
            for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                doc = iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
                for st in doc["Statement"]:
                    r.line(f"  - {name}: {st['Effect']} {st['Action']} on {st['Resource']}")
        except Exception as exc:  # e.g. Learner Lab accounts may deny IAM reads
            r.line(f"\n`{fn}` → role `{role}`; policies could not be read: {type(exc).__name__}")
    if not out.get("ExecutionRoleMode", "").startswith("least-privilege"):
        r.line("\n**Limitation:** functions run with a shared pre-existing role (Learner Lab), not the "
               "least-privilege roles defined in template.yaml.")
    r.line("\nInvalid-input checks: see `scripts/smoke_test.py` (price/total override, quantities, duplicates, line limit, malformed IDs).")

    target = ROOT / "docs" / "evidence" / f"evidence-{run_started.replace(':', '')}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(r.lines) + "\n")
    print(f"\nWrote {target} — {r.failures} failed check(s)")
    sys.exit(1 if r.failures else 0)


if __name__ == "__main__":
    main()
