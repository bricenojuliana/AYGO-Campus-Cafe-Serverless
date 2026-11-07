# Design: café staff and catalog maintainers (not implemented)

This is a policy design for two actors outside the customer-only baseline. **Nothing here is implemented.** The baseline has no staff access, no tenant field and no catalog write routes, and adding them would need new API contracts, trusted assignment data, cross-tenant tests and instructor approval.

## Principles

1. **Permission × assignment × resource.** A scope says *what kind of action* a caller may attempt. A trusted assignment says *where* (which café). The persisted resource says *which café it belongs to*. All three must match; a staff scope alone never means "every café".
2. **Assignments come from a trusted source**, never from the browser: a request header, query parameter or body field named `cafeId` or `tenant` is ignored for authorization.
3. **The tenant is persisted on the data** at creation and checked in the data operation (key design or condition expression), not only hidden in the UI.
4. **Application authorization is separate from cloud administration.** Maintaining products is an API permission; it never implies AWS IAM rights.

## Policy table

| Actor | Action | Resource | Trusted assignment source | Enforcement point |
|---|---|---|---|---|
| Customer | create, read, cancel | own orders | token `(iss, sub)`; scopes `orders:create/read/cancel` (RBAC role `Customer`) | gateway (token + scope) → Orders (owner match, conditional write) — **implemented** |
| Café staff | read orders | orders of their assigned café only | Auth0 role `CafeStaff` grants scope `cafe-orders:read`; the café assignment is a server-side table `StaffAssignments` keyed by `(iss, sub)` → `cafeIds`, maintained by an administrator (or an Auth0 Action adds a namespaced `https://campus-cafe/cafe_ids` claim from Auth0 `app_metadata`, which only administrators can write) | gateway (scope) → Orders: `order.cafeId ∈ assignment.cafeIds`, else the same uniform 404 |
| Café staff | mark ready / cancel on behalf of a customer | orders of their assigned café, allowed states only | same | Orders: conditional write `cafeId = :assigned AND #status IN (:allowed)`; the customer owner fields stay unchanged; audit log entry with staff principal |
| Café staff | anything in another café | other cafés' orders | — | denied: 404, no data; tested with a staff token assigned to café A against an order of café B |
| Catalog maintainer | create/update/archive products | products of the catalogs they maintain | Auth0 role `CatalogMaintainer` grants `catalog:write`; maintained catalogs come from a trusted assignment (table or admin-only `app_metadata` claim) | gateway (new protected routes, e.g. `PUT /products/{id}`, scope `catalog:write`) → Catalog: `product.catalogId ∈ assignment` in a condition expression |
| Catalog maintainer | AWS deployment, IAM, table administration | AWS account | — | not granted: maintainers are Auth0 users, not IAM principals; deployments use a separate pipeline role |
| Platform administrator | assign staff and maintainers | assignment table / `app_metadata` | Auth0 admin with MFA, outside the public API | Auth0 dashboard or an admin-only service; never the customer SPA |

## Where the tenant lives and how it constrains data

- Each order gets a `cafeId`, chosen by the server from the product or pickup point, never from a client field named for the tenant. It is persisted next to `ownerIss`/`ownerSub` and is immutable after creation.
- Staff reads use a GSI keyed by `cafeId` (for example `GSI1PK = CAFE#<cafeId>`). The Orders service builds the query key **from the assignment**, never from a client parameter, so a query can only touch assigned cafés. Single-order reads load the item and compare `order.cafeId` with the assignment before returning anything.
- Staff writes use condition expressions that include `cafeId = :assigned`, the same way customer cancellation includes the owner pair, so a race or a bug in the application check cannot write across cafés.
- Missing, foreign-café and legacy orders without `cafeId` all return the same 404.

## Catalog authorization versus cloud administration

`catalog:write` lets a person change synthetic product data through the Catalog API, limited by their catalog assignment and validated by the same domain rules (integer minor units, the configured currency). It does not give access to AWS. Creating tables, deploying functions or changing IAM remain with the deployment identity (in the lab, the existing `LabRole` session), audited separately in CloudTrail.

## Tests an implementation would need

- Staff of café A: reads A's orders; gets 404 for B's order and for a missing order (identical bodies); cannot cancel B's order (condition fails, order unchanged).
- A forged `cafeId` header, query parameter or body field changes nothing.
- A staff token without the café assignment (scope only) gets nothing.
- A customer token with `cafe-orders:read` but no assignment gets nothing beyond their own orders.
- A maintainer cannot edit a product outside their catalog, and cannot reach any AWS administrative operation through the API.
