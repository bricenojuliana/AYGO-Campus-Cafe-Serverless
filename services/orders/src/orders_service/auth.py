"""Caller identity from the HTTP API authorizer context (payload format 2.0).

The workshop baseline has no authorizer, so every caller is anonymous. When a JWT authorizer is
attached to the routes (see template.yaml), API Gateway validates the token and places its claims at
requestContext.authorizer.jwt.claims; this is the only place the handler reads them. CORS is not
authentication and is not consulted here.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Caller:
    subject: Optional[str]  # JWT "sub" claim; None for anonymous callers


ANONYMOUS = Caller(subject=None)


def caller_from_event(event: dict) -> Caller:
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    subject = claims.get("sub")
    return Caller(subject=str(subject)) if subject else ANONYMOUS
