"""Clients are built at import time when the Lambda environment is configured, and not otherwise."""
import importlib
import sys

from moto import mock_aws


def fresh_import(module_name):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_no_environment_means_no_eager_clients(monkeypatch):
    for name in ("ORDERS_TABLE_NAME", "CATALOG_BASE_URL", "CATALOG_TABLE_NAME"):
        monkeypatch.delenv(name, raising=False)
    assert fresh_import("orders_service.handler")._service is None
    assert fresh_import("catalog_service.handler")._repository is None


def test_configured_environment_builds_clients_at_import(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("ORDERS_TABLE_NAME", "orders")
    monkeypatch.setenv("CATALOG_BASE_URL", "https://example.execute-api.us-east-1.amazonaws.com")
    monkeypatch.setenv("CATALOG_TABLE_NAME", "catalog")
    with mock_aws():  # no AWS calls are made at import; this only guarantees it
        try:
            assert fresh_import("orders_service.handler")._service is not None
            assert fresh_import("catalog_service.handler")._repository is not None
        finally:
            sys.modules.pop("orders_service.handler", None)
            sys.modules.pop("catalog_service.handler", None)
