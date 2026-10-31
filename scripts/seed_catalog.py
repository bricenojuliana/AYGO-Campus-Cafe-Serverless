"""Loads seed/products.json into the deployed Catalog table.

Usage: python scripts/seed_catalog.py [--stack campus-cafe] [--region us-east-1] [--table NAME]
Credentials come from the standard AWS chain (profile, SSO, env); none are stored here.
"""
import argparse
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "catalog" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog_service.domain.product import product_from_record, product_to_record  # noqa: E402
from stack_outputs import DEFAULT_STACK, stack_outputs  # noqa: E402


def load_seed(path: Path) -> list[dict]:
    records = json.loads(path.read_text())
    products = [product_from_record(r) for r in records]  # same invariants the service enforces
    if len(products) < 5 or all(p.available for p in products):
        raise SystemExit("seed must contain at least five products and one unavailable product")
    if len({p.product_id for p in products}) != len(products):
        raise SystemExit("seed contains duplicate productIds")
    return [product_to_record(p) for p in products]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default=DEFAULT_STACK)
    parser.add_argument("--region")
    parser.add_argument("--table", help="Catalog table name (default: read from stack outputs)")
    parser.add_argument("--file", default=str(ROOT / "seed" / "products.json"))
    args = parser.parse_args()

    records = load_seed(Path(args.file))
    table_name = args.table or stack_outputs(args.stack, args.region)["CatalogTableName"]
    table = boto3.resource("dynamodb", region_name=args.region).Table(table_name)
    with table.batch_writer(overwrite_by_pkeys=["productId"]) as batch:
        for record in records:
            batch.put_item(Item=record)
    print(f"Seeded {len(records)} products into {table_name}")


if __name__ == "__main__":
    main()
