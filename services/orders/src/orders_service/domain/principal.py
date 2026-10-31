"""Who is calling Orders. A subject (`sub`) is unique only within its issuer (`iss`), so the pair is the identity."""
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    scopes: FrozenSet[str] = frozenset()  # permissions the access token grants, e.g. "orders:read"
