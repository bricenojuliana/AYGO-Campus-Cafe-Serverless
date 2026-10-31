"""DynamoDB adapter for the Catalog table."""
import logging
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError

from ..domain.product import InvalidProduct, Product, product_from_record

logger = logging.getLogger(__name__)


class StoreUnavailable(Exception):
    """The catalog table could not be read."""


class DynamoProductRepository:
    def __init__(self, table):
        self._table = table

    @classmethod
    def from_table_name(cls, table_name: str) -> "DynamoProductRepository":
        import boto3

        return cls(boto3.resource("dynamodb").Table(table_name))

    def get(self, product_id: str) -> Optional[Product]:
        try:
            item = self._table.get_item(Key={"productId": product_id}).get("Item")
        except (BotoCoreError, ClientError) as exc:
            raise StoreUnavailable(str(exc)) from exc
        return product_from_record(item) if item else None

    def list_all(self) -> list[Product]:
        # The menu is small (tens of products); a paginated Scan is adequate here.
        products: list[Product] = []
        kwargs: dict = {}
        try:
            while True:
                page = self._table.scan(**kwargs)
                for item in page.get("Items", []):
                    try:
                        products.append(product_from_record(item))
                    except InvalidProduct:
                        logger.warning("skipping invalid product record %s", item.get("productId"))
                if "LastEvaluatedKey" not in page:
                    return products
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        except (BotoCoreError, ClientError) as exc:
            raise StoreUnavailable(str(exc)) from exc
