# Campus Café — Design

Backend: **Python**, Lambda runtime `python3.13`. Infrastructure: **AWS SAM**. API: **API Gateway HTTP API, Lambda proxy payload format 2.0** on every integration. The browser UI is served locally.

## 1. Services and data ownership

| Service | Routes | Owns | IAM |
|---|---|---|---|
| **Catalog** (`services/catalog`) | `/products` | `CatalogTable` (key `productId`) | `GetItem`, `Scan` on CatalogTable |
| **Orders** (`services/orders`) | `/orders` | `OrdersTable` (key `pk`: `ORDER#<id>` and `IDEMPOTENCY#<scoped key>`) | `GetItem`, `PutItem`, `ConditionCheckItem` on OrdersTable |

Orders never reads CatalogTable. It gets current prices and availability with `GET /products/{productId}` **through the gateway** over HTTPS.

Layering in both services, one folder per layer. Dependencies point inward, and `tests/infra/test_layers.py` enforces it:

```
orders_service/
├── handler.py      Lambda entry point: parses the 2.0 event and wires the layers together
├── domain/         business rules, no boto3, no HTTP: errors.py, order.py, requests.py, principal.py, ids.py
├── application/    use cases (service.py) and the ports they need (ports.py) → uses domain
├── adapters/       dynamodb_repository.py, catalog_http_client.py → use domain and application ports
└── api/            HTTP side: responses.py (HTTPStatus mapping of errors), auth.py, observability.py → uses domain

catalog_service/
├── handler.py
├── domain/product.py
├── adapters/dynamodb_repository.py
└── api/            responses.py, observability.py
```

Business errors carry only a `code` and a `message`; `api/responses.py` is the single place that maps each one to a standard `HTTPStatus` (and headers such as `WWW-Authenticate`).

Configuration has one source. Every value that can differ between deployments is a `template.yaml` parameter passed as an environment variable: table names, Catalog URL, timeouts and attempts, `Currency`, `IdempotencyTtlHours`, log level. The handlers read them with `required_env` and stop at init if one is missing, instead of falling back to a default that could drift from the template. `LOG_LEVEL` is the one exception: it defaults to `INFO`, so a logging setting can never keep the function from starting. Values that are design rules rather than settings stay as named constants in code (order limits, retry counts, the metrics namespace).

### How the two services can evolve independently

Both services are deployed by one template for this exercise, but nothing ties them together except the public HTTP contract:

* **Data.** Each service is the only reader and writer of its own table, and IAM enforces it. Catalog can change its key schema, add attributes or move to another store without touching Orders.
* **Coupling point.** Orders depends only on `GET /products/{productId}` and the fields `productId`, `name`, `priceCents`, `currency`, `available` ([openapi.yaml](openapi.yaml)). Catalog can add fields or routes freely; removing or renaming one of those is a breaking change and needs a new version path.
* **Historical correctness.** Orders stores a price snapshot per line, so later price or name changes in Catalog never alter existing orders.
* **Configuration, not code.** Orders finds Catalog through `CATALOG_BASE_URL`. Catalog could move to its own stack or API, and Orders would only need that value changed.
* **Separate stacks.** Splitting `template.yaml` in two would require only that the Catalog stack export its URL and the Orders stack import it. Each service could then be deployed, rolled back and scaled on its own schedule, with its own roles and log groups, which are already per-service.
* **Failure.** A Catalog outage blocks only new orders (503, nothing stored). Reading and cancelling existing orders do not call Catalog.

## 2. HTTP contract

The full contract, with schemas, examples and every error code, is [openapi.yaml](openapi.yaml). `tests/infra/test_openapi.py` checks that it is valid OpenAPI 3.0, that its routes equal the routes in `template.yaml`, and that seed products and real domain objects match its schemas.

| Route | Success | Errors |
|---|---|---|
| `GET /products` | 200 `{"items":[Product]}` | 503 `STORE_UNAVAILABLE` |
| `GET /products/{productId}` | 200 `Product` | 404 `PRODUCT_NOT_FOUND`, 503 |
| `POST /orders` + `Idempotency-Key` | 201 `Order` + `Location: /orders/{id}`; 200 stored creation result on replay | 400 `INVALID_REQUEST` / `MISSING_IDEMPOTENCY_KEY` / `UNKNOWN_PRODUCT`; 409 `IDEMPOTENCY_KEY_CONFLICT` / `PRODUCT_UNAVAILABLE`; 503 `CATALOG_UNAVAILABLE` / `STORE_UNAVAILABLE` |
| `GET /orders/{orderId}` | 200 current `Order` | 404 `ORDER_NOT_FOUND` |
| `POST /orders/{orderId}/cancel` | 200 cancelled `Order` (also on repeat) | 404 `ORDER_NOT_FOUND` |

* `Product = {productId, name, priceCents, currency, available}`. `productId` is a canonical lowercase UUID; the seed uses fixed UUIDs so reseeding overwrites instead of duplicating.
* `Order = {orderId, status: CREATED|CANCELLED, items:[{productId, name, quantity, unitPriceCents}], totalCents, currency, createdAt, updatedAt}`.
* `orderId` is a short, human-friendly code such as `CC-7K3Q-9XMD`, generated by `orders_service/domain/ids.py`.
  * It uses 8 Crockford base32 characters (no I, L, O or U), which is about 40 random bits.
  * Uniqueness is enforced by the conditional write. On the rare collision the service draws a new code (up to 3 attempts).
  * Lookups normalize what people type: case, missing dashes, and O→0, I/L→1. A malformed code is reported as 404.
* Error body: `{"code", "message", "requestId"}`, where `requestId` is the API Gateway request id.
* Request body: `{"items":[{"productId","quantity"}]}`.
  * 1–10 distinct products, integer quantity 1–10, duplicate product IDs are rejected.
  * Unknown fields such as `totalCents` or `priceCents` are rejected, so the client can never influence prices.
* One currency for the whole application: COP (ISO 4217, 2 minor units). Money is always integer minor units, so `priceCents: 200000` is $2.000 COP. The UI shows whole pesos.

CORS is configured on the API, not in the code:

* origin: only `FrontendOrigin`;
* methods: `GET`, `POST`, `OPTIONS`;
* allowed headers: `content-type`, `idempotency-key`;
* exposed headers: none (the UI reads only response bodies; `Location` is still returned for API clients);
* no credentials.

CORS controls which browser origin may read responses; it does not authenticate callers.

## 3. Atomic idempotency

1. Validate the key and the body. Compute a fingerprint: SHA-256 of the normalized lines, sorted by `productId`.
   * Scope the key to the caller: `scoped key = SHA-256(JSON [iss, sub, Idempotency-Key])` (`domain/requests.py` `idempotency_scope_key`). The JSON list quotes each value, so separators inside a value (Auth0 subjects look like `auth0|abc`) cannot make two different callers collide. Two users sending the same header value get two independent records.
2. **Before calling Catalog**, run a consistent `GetItem IDEMPOTENCY#<scoped key>`. Expired records are ignored because DynamoDB TTL deletes them asynchronously.
   * Same caller and same fingerprint → 200 with the stored creation result. No repricing and no new order.
   * Different fingerprint, or a record owned by another principal → 409, without returning the stored response.
3. Call Catalog, then price the order from Catalog data only.
4. Run one `TransactWriteItems` with two puts:
   * `Put ORDER#<id>` with `attribute_not_exists(pk)`;
   * `Put IDEMPOTENCY#<scoped key>` (fingerprint, orderId, exact response JSON, owner `ownerIss`/`ownerSub`, `expiresAt` = now + `IdempotencyTtlHours`) with `attribute_not_exists(pk) OR expiresAt <= :now`.
5. If the transaction is cancelled and the record now exists, a concurrent identical submission won. The service re-reads the record and replays it, or returns 409 if the fingerprints differ. Exactly one order is persisted.

Order and record commit together or not at all. Failures before step 4 (validation, product errors, Catalog down) write nothing, so the client can retry with the same key.

## 4. Durable state

* Every piece of state is in DynamoDB: on-demand billing, point-in-time recovery, SSE. The Lambdas keep no process-local state.
* `OrdersTable` has `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`, so it survives redeployments.
* Cancel reads the order, applies the domain rule (`CREATED → CANCELLED`, where `CANCELLED` is a no-op), then runs a `PutItem` with `attribute_exists(pk) AND version = :expected`.
  * A cancel can therefore never create an order.
  * Concurrent changes are retried (up to 3 attempts).
  * A repeated cancel returns the same cancelled order without writing.

## 5. Bounded Catalog call

`HttpCatalogClient` sends `GET {CATALOG_BASE_URL}/products/{id}`, where `CATALOG_BASE_URL` comes from `!Sub https://${CafeHttpApi}.execute-api…`.

* Per-request timeout: `CatalogTimeoutSeconds` (default 2 s).
* Attempts: at most `CatalogMaxAttempts` (default 2). Only timeouts, connection errors, 5xx and 429 are retried.
* Overall deadline: `min(CatalogBudgetSeconds, Lambda remaining time − 1 s)`. The caller stops waiting at the deadline (`future.result(timeout=…)`, executor shut down without waiting), so a slow socket cannot extend the request.
* Distinct products are fetched concurrently (≤ 5 workers).
* 404 → product unknown (400). `available:false` → 409. Exhausted budget, a malformed payload or a foreign currency → 503 `CATALOG_UNAVAILABLE`.

Timeouts are ordered so the budget always fits: Catalog Lambda 5 s; Orders Lambda 12 s, which is more than the maximum budget of 8 s plus margin; Orders integration 13 s.

## 6. Why explicit `AWS::ApiGatewayV2::*` resources

Orders needs the API's URL in its environment. With SAM `HttpApi` events, the generated API definition embeds the function ARNs, which creates the cycle `Function → Api → Function`. With explicit resources the graph is acyclic: `Api ← Function ← Integration ← Route`. Each integration sets `PayloadFormatVersion: "2.0"`, and `tests/infra` asserts it.

## 7. Observability

* API access logs (JSON) go to a dedicated log group and include `$context.requestId`.
* Each Lambda writes to its own log group via `LoggingConfig`. Retention is set by `LogRetentionDays`.
* Every request produces one JSON line with these fields:
  * `requestId`: the API Gateway request id, equal to the access log's id and the `apigw-requestid` response header;
  * `lambdaRequestId`;
  * `operation` (`CreateOrder`, `GetOrder`, `CancelOrder`, `ListProducts`, `GetProduct`);
  * `status`;
  * `result` (`OK` or the error code);
  * `durationMs`;
  * `orderId` (on order operations) or `productId`.
* Orders forwards its `requestId` to Catalog as `x-correlation-id`, and Catalog logs it as `correlationId`. One order can therefore be followed through the gateway access log, Orders and Catalog.
* Orders emits Embedded Metric Format metrics: `OrdersCreated`, `IdempotentReplays`, `DependencyFailures`.

## 8. Security and the authentication seam

* The browser holds no AWS credentials. `frontend/config.js` contains only the API URL; it is generated per deployment from `config.example.js` and not committed.
* By default each function has an explicit IAM role with inline policies scoped to its own table and its own log group. There are no AWS-managed policies.
* Accounts that forbid `iam:CreateRole` (AWS Academy Learner Lab) set `ExecutionRoleArn` to the pre-existing `LabRole`. The scoped roles are then not created, and both functions run with that broader role.
  * Least privilege at runtime is therefore **not** met in the lab. The template still encodes the intended policy, and it is the default.
  * The `ExecutionRoleMode` output and the evidence report state which mode is deployed.
* All inputs are validated in the domain layer: UUID product IDs, quantity bounds, line limits, duplicates and unknown fields such as client prices or totals.
* Authentication uses Auth0 access tokens and the native HTTP API JWT authorizer (`OrdersJwtAuthorizer`) on the three Orders routes; the two Products routes stay public (`AuthorizationType: NONE`). The authorizer checks the RS256 signature against Auth0's published keys, `iss` = `Auth0Issuer`, `aud` = `Auth0Audience` (the API identifier, so ID tokens are rejected), expiry and the route's scope (`orders:create`, `orders:read`, `orders:cancel`). Invalid or missing token → 401, missing scope → 403, and Orders is not invoked. No IAM resources were added.
  * `orders_service/api/auth.py` reads `iss` and `sub` from `requestContext.authorizer.jwt.claims` into a `Principal`. Without them Orders answers 401 (fail closed).
  * Each Orders route needs exactly one scope from the token's `scope` claim (`orders:create`, `orders:read`, `orders:cancel`; `api/auth.py` `ROUTE_SCOPES`). Without it Orders answers 403 `INSUFFICIENT_SCOPE` before doing any work. The gateway authorizer checks the same scopes; this second check keeps a misconfigured route from granting more.
  * Every order stores its owner as `ownerIss` + `ownerSub`, taken from the verified token at creation; the request can never set it (unknown fields are rejected) and the API never returns it.
  * Only the owner can read or cancel an order. A missing order, someone else's order and a legacy order without an owner all get the same 404 `ORDER_NOT_FOUND`, so order codes cannot be probed.
  * The cancel write is conditional on the stored owner as well (`ownerIss = :iss AND ownerSub = :sub`), so DynamoDB itself refuses a write by anyone else, even if the service check were bypassed.
  * Idempotency keys are scoped per caller (iss + sub), and a stored creation response is replayed only to the principal that created it (section 3).
  * CORS is not used as access control.

## 9. Retries

* Catalog GETs are safe to repeat and are retried within the deadline.
* `POST /orders` is never retried by the server. The browser retries it with the same `Idempotency-Key`.
* `TransactWriteItems` carries a `ClientRequestToken` derived from the key and the new order id, so SDK-level retries of the same attempt are idempotent.
* A timeout never becomes an accepted order: on any Catalog failure nothing is written.

## 10. Test limits

moto implements transactions by copying and restoring whole tables without a cross-thread lock, so it cannot demonstrate DynamoDB's isolation under real concurrency. The adapter test therefore serializes calls and forces the race through the service path. The authoritative concurrency checks run against the deployed API: `scripts/smoke_test.py` and `scripts/collect_evidence.py`.
