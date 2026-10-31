"""Caller identity and permissions from the HTTP API JWT authorizer context (payload format 2.0).

API Gateway validates the access token (signature, issuer, audience, expiry, scopes) before invoking
this function and places its claims at requestContext.authorizer.jwt.claims. That verified context is
the only source of identity: headers, the body and CORS are never consulted. If the context is missing
or incomplete the request is rejected (fail closed).

Each Orders route also needs its own scope. The gateway checks it first; the handler checks it again so
a misconfigured route cannot grant more than intended (defense in depth).
"""
from typing import Mapping

from ..domain.errors import InsufficientScope, Unauthenticated
from ..domain.principal import Principal

# One scope per route: a token that may create orders cannot read or cancel them, and vice versa.
ROUTE_SCOPES: Mapping[str, str] = {
    "POST /orders": "orders:create",
    "GET /orders/{orderId}": "orders:read",
    "POST /orders/{orderId}/cancel": "orders:cancel",
}


def principal_from_event(event: dict) -> Principal:
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims")
    if not isinstance(claims, dict):
        raise Unauthenticated()
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not (isinstance(issuer, str) and issuer and isinstance(subject, str) and subject):
        raise Unauthenticated()
    scope = claims.get("scope")  # OAuth 2.0: one space-separated string, e.g. "orders:create orders:read"
    scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
    return Principal(issuer=issuer, subject=subject, scopes=scopes)


def require_scope(principal: Principal, route_key: str) -> None:
    """Raises 403 unless the token grants the exact scope of this route. Unknown routes need none here."""
    required = ROUTE_SCOPES.get(route_key)
    if required is not None and required not in principal.scopes:
        raise InsufficientScope()
