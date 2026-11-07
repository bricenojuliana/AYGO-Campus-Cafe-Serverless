# Verification matrix — Campus Café security workshop

Execution track: **prepared live application** (own Auth0 tenant, AWS Academy Learner Lab) plus local service tests.

| Environment label | What it is | What it can prove |
|---|---|---|
| **HTTP API / lab** | Deployed stack `campus-cafe`, us-east-1, 2026-11-07, real API Gateway JWT authorizer, real Auth0 access tokens (Alice, Bob, Carol read-only) and an ID token | Gateway authentication and scope checks, and Orders behavior end to end |
| **Lab / AWS SDK** | `scripts/collect_evidence.py` reading DynamoDB and CloudWatch with the lab credentials | Persisted owner, logs, fault isolation |
| **Local / unit** | pytest with **trusted-context fixtures**: fabricated payload-v2 events whose `requestContext.authorizer.jwt.claims` the tests write themselves (`tests/events.py`, `TEST_CLAIMS`) | Orders logic only. Never Auth0 login, signature validation, API Gateway or IAM |
| **Local / moto** | pytest against moto's DynamoDB | Exact DynamoDB conditions and transactions, single-threaded (moto does not isolate concurrent transactions) |
| **Local / static** | pytest reading `template.yaml`, `docs/openapi.yaml`, `frontend/*` | Configuration as written, not as deployed |

Artifacts: [smoke-2026-11-07T0315Z.txt](../evidence/smoke-2026-11-07T0315Z.txt) (46 PASS, 0 FAIL, 0 BLOCKED), [evidence-2026-11-07T031633Z.md](../evidence/evidence-2026-11-07T031633Z.md) (19 PASS, 0 FAIL), [local-checks-2026-11-07.txt](../evidence/local-checks-2026-11-07.txt) (248 passed, cfn-lint and `sam validate --lint` clean). Tokens were read from environment variables and are masked by the scripts; no artifact contains a bearer value (`tests/infra/test_template.py::test_no_credentials_in_repository` fails on anything shaped like a JWT).

## Core deployed boundary checks

| ID | Boundary / environment | Command or test | Fixture | Expected | Observed | Result |
|---|---|---|---|---|---|---|
| G1 | HTTP API / lab | `smoke_test.py` G1 | no token | public product list and detail 200 | 200; at least 5 products, one unavailable, one currency | PASS |
| G2 | HTTP API / lab | `smoke_test.py` G2 | no token; Alice's token with a reversed signature | 401, Orders not invoked | 401 on all three Orders routes; 401 for the tampered token; body is the gateway's `{"message":"Unauthorized"}` | PASS |
| G3 | HTTP API / lab + Lab / AWS SDK | `smoke_test.py` G3; `collect_evidence.py` owner check | Alice, Bob | each creates and reads own order; owner derived on the server | 201 + 200 for both; DynamoDB `ownerIss` = Auth0 issuer, `ownerSub` = token subject (shortened in the report) | PASS |
| G4 | HTTP API / lab | `smoke_test.py` G4 | Bob → Alice's order; Alice → Bob's | uniform 404, no payload, no state change | 404 `ORDER_NOT_FOUND` for read and cancel; Alice's order identical afterwards; Alice → Bob 404 | PASS |
| G5 | HTTP API / lab | `smoke_test.py` G5 | Carol: genuinely issued token with only `orders:read` (Auth0 RBAC role) | 403 on routes needing another scope | POST /orders 403, cancel 403; GET of Alice's order 404 (scope is not ownership) | PASS |
| G6 | HTTP API / lab | `smoke_test.py` G6 | Alice's ID token as bearer | rejected, not 2xx | 401 (audience is the SPA client ID, not the API) | PASS |

## Core service and persistence checks

| ID | Boundary / environment | Command or test | Expected | Observed | Result |
|---|---|---|---|---|---|
| S1 | Local / unit | `test_auth_seam.py::test_every_orders_route_fails_closed_without_a_verified_principal`, `::test_identity_claimed_in_headers_or_body_is_ignored`, `::test_ownership_fields_in_create_input_are_rejected_and_nothing_is_written` (8 fields), `::test_ownership_inside_an_item_line_is_rejected_too`, `::test_missing_context_leaves_a_safe_diagnostic_without_the_token` | missing/malformed context → 401, no Catalog call, no write, safe diagnostic; forged owner fields → 400, no write | as expected | PASS |
| S1 | HTTP API / lab | `smoke_test.py` S1 | forged `ownerSub` / `customerId` → 400 | 400 `INVALID_REQUEST` | PASS |
| S2 | Local / unit | `test_ownership.py::test_missing_foreign_and_legacy_orders_get_the_same_404`, `::test_each_user_sees_only_their_own_orders` | own order returned; foreign, missing and ownerless → identical 404 (status, headers, body) | as expected for GET and cancel | PASS |
| S2 | HTTP API / lab | `smoke_test.py` S2 | foreign and missing → same 404 body | same body except `requestId` | PASS |
| S3 | Local / unit + moto | `test_ownership.py::test_someone_else_cannot_cancel_and_the_order_is_unchanged`, `test_orders_service.py::test_cancel_is_persisted_and_repeatable`, `test_dynamodb_adapters.py::test_owner_is_stored_and_dynamodb_refuses_a_cancel_by_anyone_else` | no foreign write; own cancel persisted; repeat returns the same CANCELLED order | as expected | PASS |
| S3 | HTTP API / lab | `smoke_test.py` S3 | own cancel 200, repeat identical, persisted; cancel of a missing order creates nothing | as expected | PASS |
| S4 | Local / moto | `test_dynamodb_adapters.py::test_cancel_write_carries_owner_and_status_in_its_condition`, `::test_a_racing_status_change_cannot_be_overwritten`, `::test_legacy_order_without_owner_cannot_be_replaced` | the write itself carries `attribute_exists(pk) AND version = :v AND ownerIss = :iss AND ownerSub = :sub AND #status = :expected_status`; a concurrent status change wins | condition recorded from the real `put_item` call; stale transition rejected, stored order unchanged | PASS |
| S5 | Local / unit | `test_idempotency_scope.py::test_same_user_same_request_replays_the_original_result`, `::test_same_user_changed_request_is_a_conflict` | replay; 409 and original unchanged | as expected | PASS |
| S5 | HTTP API / lab | `smoke_test.py` S5 | 200 original result; 409 `IDEMPOTENCY_KEY_CONFLICT`; replay still returns the creation result after cancel | as expected | PASS |
| S6 | Local / unit | `test_idempotency_scope.py::test_two_users_with_the_same_key_get_separate_orders`, `::test_a_record_of_another_user_is_never_replayed`, `::test_different_inputs_never_collide` | independent Bob order; a planted foreign record → 409, never its response; no key collisions with separators in `sub` | as expected | PASS |
| S6 | HTTP API / lab | `smoke_test.py` S6 | Bob with Alice's client key → his own new order | 201, different order ID | PASS |
| S7 | HTTP API / lab | `smoke_test.py` S7; `collect_evidence.py` concurrency | 5 concurrent identical creates → one order | `[200, 200, 200, 200, 201]`, one order ID, one stored order | PASS |
| S7 | Local / moto | `test_dynamodb_adapters.py::test_racing_identical_submissions_persist_one_order`, `::test_second_transaction_with_same_key_writes_nothing` | conditional transaction never overwrites a winner | as expected (serialized client: moto cannot run real concurrency) | PASS |
| S8 | HTTP API / lab | `smoke_test.py` S8 (price/total override, quantities, duplicates, unknown/unavailable product, server total) | 400 / 409; server-calculated total | as expected | PASS |
| S8 | Lab / AWS SDK | `collect_evidence.py` fault isolation (temporary unreachable Catalog URL) | 503 `CATALOG_UNAVAILABLE`, no order, no idempotency record | DNS failure 806 ms, blackhole 4953 ms, `orders 1 -> 1` both | PASS |
| S9 | Local / unit + static | `test_observability.py::test_request_log_never_contains_the_bearer_token`, `test_template.py::test_access_log_records_authorizer_errors_but_never_the_token`, `test_layers.py::test_layer_imports_point_inward`, `test_template.py::test_each_service_role_reaches_only_its_own_table_and_log_group` | correlation kept; no token in logs; no cross-service table access in code or in the template roles | as expected | PASS |
| S9 | Lab / AWS SDK | `collect_evidence.py` observability | one `requestId` from the access log to Orders and Catalog logs | found in all three; access log `authorizerError` field present | PASS |
| S10 | Local / unit | `test_idempotency_scope.py::test_replay_keeps_the_original_price_snapshot_after_catalog_prices_change` | identical retry returns the original snapshot without calling Catalog; a new order uses the new price | as expected | PASS |
| S10 | HTTP API / lab | — | same, against the deployed stack | changing a deployed price was not done | NOT RUN |

## Additional token assurance

| ID | Case | Result | Reason |
|---|---|---|---|
| A1 | correctly signed, expired `exp` | NOT RUN | no legitimately expired token or instructor-signed fixture; editing `exp` would only test the signature again |
| A2 | correctly signed, future `nbf` | NOT RUN | Auth0 does not issue such tokens; no signed fixture available |
| A3 | correctly signed for another audience | PARTIAL | G6 shows a correctly signed token with another `aud` (the ID token) rejected with 401; no access token for a different API was tested |
| A4 | token from an untrusted issuer | NOT RUN | no second tenant or controlled fixture |
| A5 | same subject string under another issuer | PASS (Local / unit) | `test_ownership.py::test_service_level_rule_matches_on_issuer_and_subject`, `test_idempotency_scope.py::test_different_inputs_never_collide` |

G2's tampered token is a signature-tampering test only. No JWT verification code was added to Orders.

## Configuration evidence (Local / static, then confirmed live)

| Claim | Test | Live confirmation |
|---|---|---|
| Products public, each Orders route has the JWT authorizer and exactly one scope; gateway scopes equal the handler's `ROUTE_SCOPES` | `test_template.py::test_products_are_public_and_each_orders_route_needs_the_jwt_and_its_scope` | G1, G2, G5 |
| No `$default`, `ANY`, `OPTIONS` or `{proxy+}` route that could bypass the authorizer; no SAM API events | `::test_no_catch_all_route_could_skip_the_authorizer`, `::test_no_sam_api_events_that_would_bypass_the_explicit_api` | preflight answered by gateway CORS (smoke CORS rows) |
| Authorizer: `$request.header.Authorization`, issuer and audience from parameters, issuer pattern requires `https` and the trailing slash | `::test_authorizer_accepts_only_auth0_access_tokens_for_this_api` | G6 |
| No IAM resources added; `LabRole` reused through `ExecutionRoleArn` | `::test_adding_authentication_created_no_iam_resources` | `ExecutionRoleMode` output, evidence security section |
| SPA: official SDK pinned with integrity, PKCE, access token for the API audience, only Orders calls carry it, no client secret, no ID token as bearer | `test_frontend.py` (7 tests) | browser login as Alice, Bob, Carol (manual) |
| Lambda invocation limited to this API's routes (`SourceArn`), no function URL | template review (`OrdersInvokePermission`, `CatalogInvokePermission`) | NOT verified live: direct invocation with lab credentials was not attempted |
