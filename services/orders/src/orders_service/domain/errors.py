"""Business errors of Orders. `code` and `message` are part of the public API; HTTP status lives in api/."""
from typing import Optional


class OrderError(Exception):
    """Base for business errors; `code` and `message` are part of the public API."""

    code = "ORDER_ERROR"
    message = "The order could not be processed."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.message)
        self.message = message or self.message


class Unauthenticated(OrderError):
    code = "UNAUTHENTICATED"
    message = "Sign in to use orders."


class InsufficientScope(OrderError):
    code = "INSUFFICIENT_SCOPE"
    message = "Your sign-in does not allow this action."


class InvalidRequest(OrderError):
    code = "INVALID_REQUEST"


class MissingIdempotencyKey(OrderError):
    code = "MISSING_IDEMPOTENCY_KEY"
    message = "The Idempotency-Key header is required (8-128 chars of letters, digits, '_', '.', ':' or '-')."


class IdempotencyConflict(OrderError):
    code = "IDEMPOTENCY_KEY_CONFLICT"
    message = "This Idempotency-Key was already used with a different request."


class ProductUnavailable(OrderError):
    code = "PRODUCT_UNAVAILABLE"
    message = "This product cannot be ordered."

    def __init__(self, product_id: str):
        super().__init__(f"This product cannot be ordered: {product_id}.")
        self.product_id = product_id


class UnknownProduct(OrderError):
    code = "UNKNOWN_PRODUCT"
    message = "This product does not exist."

    def __init__(self, product_id: str):
        super().__init__(f"This product does not exist: {product_id}.")
        self.product_id = product_id


class OrderNotFound(OrderError):
    code = "ORDER_NOT_FOUND"
    message = "This order does not exist."


class CatalogUnavailable(OrderError):
    code = "CATALOG_UNAVAILABLE"
    message = "Product information is temporarily unavailable. Retry with the same Idempotency-Key."


class StoreUnavailable(OrderError):
    code = "STORE_UNAVAILABLE"
    message = "Orders are temporarily unavailable. Retry with the same Idempotency-Key."
