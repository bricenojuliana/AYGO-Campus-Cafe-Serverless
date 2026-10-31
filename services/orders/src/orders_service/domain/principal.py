"""Who is calling Orders. A subject (`sub`) is unique only within its issuer (`iss`), so the pair is the identity."""
from dataclasses import dataclass
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class Principal:
    issuer: Optional[str]
    subject: Optional[str]
    scopes: FrozenSet[str] = frozenset()  # permissions the access token grants, e.g. "orders:read"


# Service-level default kept until ownership becomes mandatory; handlers never produce it.
ANONYMOUS = Principal(issuer=None, subject=None)
