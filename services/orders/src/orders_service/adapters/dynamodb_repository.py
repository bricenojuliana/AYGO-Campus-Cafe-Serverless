"""DynamoDB adapter for the Orders table (orders and idempotency records share one table)."""
import hashlib
import json
from typing import Optional

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from ..application.ports import IdempotencyKeyTaken, OrderIdTaken, VersionConflict
from ..domain.errors import StoreUnavailable
from ..domain.order import IdempotencyRecord, Order, OrderItem
from ..domain.principal import Principal

ORDER_PREFIX = "ORDER#"
IDEMPOTENCY_PREFIX = "IDEMPOTENCY#"

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _to_ddb(item: dict) -> dict:
    return {k: _serializer.serialize(v) for k, v in item.items()}


def _from_ddb(item: dict) -> dict:
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


def order_to_item(order: Order) -> dict:
    return {
        "pk": ORDER_PREFIX + order.order_id,
        "entityType": "ORDER",
        "orderId": order.order_id,
        "status": order.status,
        "items": [{"productId": i.product_id, "name": i.name, "quantity": i.quantity,
                   "unitPriceCents": i.unit_price_cents} for i in order.items],
        "totalCents": order.total_cents,
        "currency": order.currency,
        "createdAt": order.created_at,
        "updatedAt": order.updated_at,
        "version": order.version,
        # Owner of the order (verified token iss + sub). Read back only for ownership checks.
        **({"ownerIss": order.owner_issuer, "ownerSub": order.owner_subject}
           if order.owner_issuer and order.owner_subject else {}),
    }


def order_from_item(item: dict) -> Order:
    return Order(
        order_id=item["orderId"],
        status=item["status"],
        items=tuple(OrderItem(i["productId"], i["name"], int(i["quantity"]), int(i["unitPriceCents"]))
                    for i in item["items"]),
        total_cents=int(item["totalCents"]),
        currency=item["currency"],
        created_at=item["createdAt"],
        updated_at=item["updatedAt"],
        version=int(item["version"]),
        owner_issuer=item.get("ownerIss"),  # absent on legacy orders: nobody can access them
        owner_subject=item.get("ownerSub"),
    )


class DynamoOrderRepository:
    def __init__(self, client, table_name: str):
        self._client = client
        self._table = table_name

    @classmethod
    def from_table_name(cls, table_name: str) -> "DynamoOrderRepository":
        import boto3

        return cls(boto3.client("dynamodb"), table_name)

    def get_idempotency_record(self, key: str, now_epoch: int) -> Optional[IdempotencyRecord]:
        item = self._get(IDEMPOTENCY_PREFIX + key)
        # TTL deletion is asynchronous, so an expired record may still be present.
        if item is None or int(item["expiresAt"]) <= now_epoch:
            return None
        return IdempotencyRecord(key, item["fingerprint"], item["orderId"],
                                 json.loads(item["responseBody"]), int(item["expiresAt"]))

    def create_order(self, order: Order, record: IdempotencyRecord, now_epoch: int) -> None:
        idempotency_item = {
            "pk": IDEMPOTENCY_PREFIX + record.key,
            "entityType": "IDEMPOTENCY",
            "fingerprint": record.fingerprint,
            "orderId": record.order_id,
            # Stored as the exact JSON text so a replay returns the original creation result.
            "responseBody": json.dumps(record.response_body, separators=(",", ":")),
            "expiresAt": record.expires_at,
        }
        # Same token on SDK-level retries of this one attempt makes them idempotent for 10 minutes.
        token = hashlib.sha256(f"{record.key}|{order.order_id}".encode()).hexdigest()[:36]
        try:
            self._client.transact_write_items(ClientRequestToken=token, TransactItems=[
                {"Put": {"TableName": self._table, "Item": _to_ddb(order_to_item(order)),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self._table, "Item": _to_ddb(idempotency_item),
                         "ConditionExpression": "attribute_not_exists(pk) OR expiresAt <= :now",
                         "ExpressionAttributeValues": {":now": {"N": str(now_epoch)}}}},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                if self.get_idempotency_record(record.key, now_epoch) is not None:
                    raise IdempotencyKeyTaken(record.key) from exc
                if self.get_order(order.order_id) is not None:
                    raise OrderIdTaken(order.order_id) from exc
            raise StoreUnavailable() from exc
        except BotoCoreError as exc:
            raise StoreUnavailable() from exc

    def get_order(self, order_id: str) -> Optional[Order]:
        item = self._get(ORDER_PREFIX + order_id)
        return order_from_item(item) if item else None

    def replace_order(self, order: Order, expected_version: int, owner: Principal) -> None:
        try:
            self._client.put_item(
                TableName=self._table,
                Item=_to_ddb(order_to_item(order)),
                # attribute_exists keeps a cancellation from ever creating an order; the owner condition
                # makes DynamoDB itself refuse a write by anyone but the stored owner.
                ConditionExpression=("attribute_exists(pk) AND version = :expected"
                                     " AND ownerIss = :owner_iss AND ownerSub = :owner_sub"),
                ExpressionAttributeValues={":expected": {"N": str(expected_version)},
                                           ":owner_iss": {"S": owner.issuer},
                                           ":owner_sub": {"S": owner.subject}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionConflict(order.order_id) from exc
            raise StoreUnavailable() from exc
        except BotoCoreError as exc:
            raise StoreUnavailable() from exc

    def _get(self, pk: str) -> Optional[dict]:
        try:
            response = self._client.get_item(TableName=self._table, Key={"pk": {"S": pk}}, ConsistentRead=True)
        except (BotoCoreError, ClientError) as exc:
            raise StoreUnavailable() from exc
        item = response.get("Item")
        return _from_ddb(item) if item else None
