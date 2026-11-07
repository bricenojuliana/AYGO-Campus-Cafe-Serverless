# ADR-001 — Identity and authorization for Campus Café orders

Status: accepted, implemented on branch `feature/auth0-security`, verified on 2026-11-07 ([verification](verification.md)).

**Context.** Orders were shared: anyone who knew an order code could read or cancel it, and idempotency keys were global. The change had to add an identity and authorization boundary without rebuilding the application or weakening server-side pricing, atomic creation, durable cancellation, Catalog-failure behavior and service-owned tables.

## 1. Why these enforcement points?

Three layers, each deciding something the others cannot:

| Layer | Decides | Implementation |
|---|---|---|
| Auth0 | who the user is, and which permissions the token may carry | Universal Login with PKCE; RBAC role `Customer` (three scopes); the access token's `scope` is the intersection of requested and assigned permissions |
| API Gateway | whether the token is genuine and allowed on this route | native JWT authorizer `OrdersJwtAuthorizer`: RS256 signature against the issuer's keys, `iss`, `aud`, `exp`/`nbf`, and exactly one scope per route |
| Orders | whether this caller may touch this order | principal from the verified claims, owner stored on the order, owner match on read, owner and status in the DynamoDB condition on cancel |

A valid token with `orders:read` only says "this user may read orders". It says nothing about *which* orders, so the gateway cannot authorize a particular order. That needs the persisted owner, which only Orders has. Bob's valid token is rejected with 404 for Alice's order (G4), and Carol's `orders:read` token gets 404 for it too (G5). Orders also re-checks the route scope (403) and fails closed (401) if the verified context is missing, so a misconfigured route cannot grant more than intended.

## 2. Why this principal?

The principal is the pair **`(iss, sub)`** from `requestContext.authorizer.jwt.claims`. `sub` is stable and unique only within its issuer, so the pair is the identity. Email can change and is not verified for the test users. A display name is not unique. A `customerId` or `owner` sent by the browser can be set to anyone. The create body therefore accepts only `items`, and fields such as `owner`, `ownerSub`, `ownerIssuer` or `customerId` are rejected with 400. Orders stores the pair as `ownerIss` and `ownerSub` and never returns it.

**Legacy orders** created before this change have no owner. They are accessible to nobody (the same 404 as a missing order) and are never assigned to the next caller. They stay in the table untouched.

## 3. Why this token flow?

The SPA runs in the browser, so it cannot keep a client secret. Authorization Code Flow with **PKCE**, through the official `@auth0/auth0-spa-js` SDK, binds the authorization code to a one-time verifier the browser created, so a stolen code cannot be redeemed. The SDK keeps tokens in memory, never in `localStorage`.

The API receives the **access token** for the audience `https://campus-cafe-api`. The ID token describes the login to the SPA; its audience is the SPA client ID, so the authorizer rejects it (G6, 401). The SPA uses the ID token's profile only to show who is signed in.

| Public (may be in the browser or the repo) | Never exposed |
|---|---|
| Auth0 domain, SPA client ID, API audience, issuer URL, API URL, scope names | access and ID tokens, user passwords, any client secret, Auth0 management credentials, AWS credentials |

`frontend/config.js` holds only the public values and is generated per deployment, not committed. Scripts read tokens from environment variables and mask them.

## 4. Why this data operation?

**Cancellation** is one conditional `PutItem`:

```
attribute_exists(pk) AND version = :expected
AND ownerIss = :owner_iss AND ownerSub = :owner_sub
AND #status = :expected_status
```

The service check comes first, but DynamoDB enforces the rule again at write time. A concurrent status change or a write by a non-owner loses (S4). A failed condition is retried by re-reading through the same owner check, so an inaccessible order is never revealed.

**Creation** stays one `TransactWriteItems`: the order (`attribute_not_exists`) and the idempotency record (`attribute_not_exists OR expired`) commit together or not at all. The record's key is `IDEMPOTENCY#` + `SHA-256(JSON [iss, sub, Idempotency-Key])`. JSON quotes each part, so separators inside a value (Auth0 subjects contain `|`) cannot make two callers collide, and the hash keeps the key at 64 characters. The record also stores its owner and the **request fingerprint**: `SHA-256` of the validated lines `[[productId, quantity], …]`, sorted by `productId`, serialized without spaces. Server-calculated totals and fetched prices are not part of it.

- Same principal, same key, same accepted request → the stored creation response, without calling Catalog (original price snapshot, S10).
- Same principal and key, different request → 409; the original order is unchanged.
- Another principal with the same key → its own namespace and its own order (S6). A record that does not belong to the caller is never replayed (409).
- Concurrent duplicates → one transaction wins; the others re-read that record and replay it (S7, verified live).

## 5. Why these public and private boundaries?

The catalog is deliberately **public and synthetic** for this exercise (menu browsing without login). It is an application decision, not a rule for every catalog. Orders reads prices through `GET /products/{productId}` on the same gateway, without forwarding the user's token: that route is public, and forwarding an Orders token to another service would give it a credential it does not need and an audience it was not issued for. Each service owns its table and its code cannot import the other's adapters (`test_layers.py`).

Verified about invocation and roles: the template adds no IAM resources and reuses `LabRole`; each Lambda permission is limited by `SourceArn` to this API's `/products*` or `/orders*` routes; there is no function URL or catch-all route. Not verified: that a lab principal cannot invoke the Orders function directly (bypassing the authorizer) — `LabRole` is broad, and this was not tested.

## 6. What remains?

- **Least privilege at runtime is not met in the lab.** Both functions run with the shared `LabRole`. The scoped roles exist in the template but cannot be created in the Learner Lab.
- **Token revocation.** Logout clears the SPA session, but an issued access token stays valid until it expires (Auth0 default 24 h). The API does not check revocation.
- **Session restore on reload** fails in browsers that block third-party cookies (Brave): the user logs in again with one click. Tokens were kept in memory rather than moved to `localStorage`. A custom Auth0 domain on the same site would fix it in production.
- **Not run:** A1, A2, A4 (no signed fixtures), A3 only partially, S10 live, direct-invocation test.
- **Local harness** (`tests/e2e/local_gateway.py`) emulates the authorizer without signature verification; nothing it shows counts as authorizer evidence.
- **Production work not attempted:** custom domain, refresh-token rotation, rate limiting, WAF, per-service IAM outside the lab, staff and tenant roles ([design only](staff-tenant-design.md)).
