"""Caller identity from the HTTP API JWT authorizer context (payload format 2.0).

API Gateway validates the access token (signature, issuer, audience, expiry, scopes) before invoking
this function and places its claims at requestContext.authorizer.jwt.claims. That verified context is
the only source of identity: headers, the body and CORS are never consulted. If the context is missing
or incomplete the request is rejected (fail closed).
"""
from ..domain.errors import Unauthenticated
from ..domain.principal import Principal


def principal_from_event(event: dict) -> Principal:
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims")
    if not isinstance(claims, dict):
        raise Unauthenticated()
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not (isinstance(issuer, str) and issuer and isinstance(subject, str) and subject):
        raise Unauthenticated()
    return Principal(issuer=issuer, subject=subject)
