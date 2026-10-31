"""Catalog business rules. No AWS and no API Gateway types in this module."""
import re
from dataclasses import dataclass

CURRENCY = "COP"
# Product IDs are canonical lowercase UUIDs, e.g. "0b3f6a1e-2c4d-4e8f-9a1b-3c5d7e9f1a2b".
PRODUCT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class InvalidProductId(ValueError):
    pass


class InvalidProduct(ValueError):
    pass


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    price_cents: int
    available: bool
    currency: str = CURRENCY

    def to_api(self) -> dict:
        return {
            "productId": self.product_id,
            "name": self.name,
            "priceCents": self.price_cents,
            "currency": self.currency,
            "available": self.available,
        }


def validate_product_id(product_id: str) -> str:
    if not isinstance(product_id, str) or not PRODUCT_ID_PATTERN.match(product_id):
        raise InvalidProductId("productId must be a lowercase canonical UUID")
    return product_id


def product_from_record(record: dict) -> Product:
    """Builds a Product from a stored record, rejecting records that break catalog invariants."""
    try:
        price = record["priceCents"]
        if int(price) != price:
            raise ValueError("priceCents must be an integer number of minor units")
        product = Product(
            product_id=validate_product_id(str(record["productId"])),
            name=str(record["name"]),
            price_cents=int(price),
            available=bool(record["available"]),
            currency=str(record.get("currency", CURRENCY)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidProduct(f"invalid product record: {exc}") from exc
    if product.price_cents < 0:
        raise InvalidProduct("priceCents must be >= 0")
    if not product.name.strip():
        raise InvalidProduct("name must not be empty")
    if product.currency != CURRENCY:
        raise InvalidProduct(f"currency must be {CURRENCY}")
    return product


def product_to_record(product: Product) -> dict:
    return {
        "productId": product.product_id,
        "name": product.name,
        "priceCents": product.price_cents,
        "currency": product.currency,
        "available": product.available,
    }


def sort_for_menu(products: list[Product]) -> list[Product]:
    return sorted(products, key=lambda p: (p.name.lower(), p.product_id))
