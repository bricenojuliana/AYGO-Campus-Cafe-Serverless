"""Clients are built at import time when the Lambda environment is configured, and not otherwise.
Every setting comes from template.yaml; a missing one fails at init instead of falling back to a guess."""
import importlib
import sys

import pytest
from moto import mock_aws

# What template.yaml sets on the functions (values are examples).
LAMBDA_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "ORDERS_TABLE_NAME": "orders",
    "CATALOG_TABLE_NAME": "catalog",
    "CATALOG_BASE_URL": "https://example.execute-api.us-east-1.amazonaws.com",
    "CATALOG_TIMEOUT_SECONDS": "2",
    "CATALOG_MAX_ATTEMPTS": "2",
    "CATALOG_BUDGET_SECONDS": "5",
    "CURRENCY": "COP",
    "IDEMPOTENCY_TTL_HOURS": "24",
}


def fresh_import(module_name):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


@pytest.fixture
def lambda_environment(monkeypatch):
    for name, value in LAMBDA_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    with mock_aws():  # no AWS calls are made at import; this only guarantees it
        yield monkeypatch
    sys.modules.pop("orders_service.handler", None)
    sys.modules.pop("catalog_service.handler", None)


def test_no_environment_means_no_eager_clients(monkeypatch):
    for name in LAMBDA_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    assert fresh_import("orders_service.handler")._service is None
    assert fresh_import("catalog_service.handler")._repository is None


def test_configured_environment_builds_clients_at_import(lambda_environment):
    orders = fresh_import("orders_service.handler")
    assert orders._service is not None
    assert orders._service._currency == "COP" and orders._service._idempotency_ttl == 24 * 3600
    assert fresh_import("catalog_service.handler")._repository is not None


@pytest.mark.parametrize("missing", ["CATALOG_TIMEOUT_SECONDS", "CATALOG_MAX_ATTEMPTS", "CURRENCY",
                                     "IDEMPOTENCY_TTL_HOURS"])
def test_orders_fails_at_init_when_a_setting_is_missing(lambda_environment, missing):
    lambda_environment.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        fresh_import("orders_service.handler")


def test_catalog_fails_at_init_without_currency(lambda_environment):
    lambda_environment.delenv("CURRENCY")
    with pytest.raises(RuntimeError, match="CURRENCY"):
        fresh_import("catalog_service.handler")
