"""Validation of client input: the Idempotency-Key header and the POST /orders body."""
import hashlib
import json
import re
from typing import Any, Optional

from .errors import InvalidRequest, MissingIdempotencyKey
from .order import OrderLine

MAX_DISTINCT_PRODUCTS = 10
MIN_QUANTITY, MAX_QUANTITY = 1, 10
PRODUCT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # canonical lowercase UUID
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def validate_idempotency_key(key: Optional[str]) -> str:
    if not key or not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise MissingIdempotencyKey()
    return key


def parse_create_request(body: Any) -> tuple[OrderLine, ...]:
    """Validates the POST /orders body and returns its lines sorted by productId."""
    if not isinstance(body, dict):
        raise InvalidRequest("The request body must be a JSON object.")
    unknown = set(body) - {"items"}
    if unknown:
        raise InvalidRequest(f"Unknown fields: {', '.join(sorted(unknown))}.")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise InvalidRequest("items must be a non-empty array.")
    if len(items) > MAX_DISTINCT_PRODUCTS:
        raise InvalidRequest(f"An order can contain at most {MAX_DISTINCT_PRODUCTS} products.")

    lines: dict[str, OrderLine] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"productId", "quantity"}:
            raise InvalidRequest(f"items[{index}] must have exactly productId and quantity.")
        product_id, quantity = item["productId"], item["quantity"]
        if not isinstance(product_id, str) or not PRODUCT_ID_PATTERN.match(product_id):
            raise InvalidRequest(f"items[{index}].productId must be a lowercase UUID.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) \
                or not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
            raise InvalidRequest(f"items[{index}].quantity must be an integer from {MIN_QUANTITY} to {MAX_QUANTITY}.")
        if product_id in lines:
            raise InvalidRequest(f"productId '{product_id}' appears more than once.")
        lines[product_id] = OrderLine(product_id, quantity)
    return tuple(sorted(lines.values(), key=lambda line: line.product_id))


def idempotency_scope_key(issuer: str, subject: str, key: str) -> str:
    """Storage key for an Idempotency-Key, scoped to the caller (issuer + subject) that sent it.

    Requirements:
    * Deterministic: the same (issuer, subject, key) always gives the same result, so a retry finds
      the original record.
    * Collision-free: two different triples must never give the same result, even though the values
      can contain separators. Auth0 subjects look like "auth0|abc" and keys may contain "." ":" "-" "_".
    * Used inside the DynamoDB partition key (max 2048 bytes), so keep it a short string.
    """
    # A JSON list quotes and escapes each value, so separators inside a value cannot shift the boundary
    # between parts ("auth0|a" + "b|k" and "auth0|a|b" + "k" stay different).
    unambiguous = json.dumps([issuer, subject, key], separators=(",", ":"))
    # The hash gives a fixed 64-character key whatever the input length (DynamoDB keys are limited).
    return hashlib.sha256(unambiguous.encode("utf-8")).hexdigest()


def request_fingerprint(lines: tuple[OrderLine, ...]) -> str:
    """Stable hash of the normalized request; equal requests produce equal fingerprints."""
    canonical = json.dumps([[line.product_id, line.quantity] for line in lines], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
