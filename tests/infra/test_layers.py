"""Enforces the layer rule inside each service: dependencies point inward, the domain depends on nothing.

    handler.py (entry point, wires everything)
        ├── api/          HTTP and API Gateway event → may use domain
        ├── adapters/     DynamoDB, Catalog over HTTPS → may use domain and application ports
        ├── application/  use cases and ports → may use domain
        └── domain/       business rules → no other layer, no AWS SDK, no HTTP
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    "orders_service": ROOT / "services/orders/src/orders_service",
    "catalog_service": ROOT / "services/catalog/src/catalog_service",
}
ALLOWED = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "adapters": {"domain", "application", "adapters"},
    "api": {"domain", "api"},
}
FORBIDDEN_IN_DOMAIN = {"boto3", "botocore", "http", "urllib"}


def module_files():
    for package, base in PACKAGES.items():
        for path in sorted(base.rglob("*.py")):
            layer = path.relative_to(base).parts[0]
            if layer in ALLOWED:
                yield package, layer, path


def imported_modules(path: Path, package: str):
    """Absolute dotted names of everything a file imports, including imports inside functions."""
    current = [package, *path.relative_to(PACKAGES[package]).parent.parts]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                yield node.module
            else:
                base = current[: len(current) - (node.level - 1)]
                yield ".".join(base + ([node.module] if node.module else []))


@pytest.mark.parametrize("package, layer, path", list(module_files()),
                         ids=lambda value: value.name if isinstance(value, Path) else str(value))
def test_layer_imports_point_inward(package, layer, path):
    for module in imported_modules(path, package):
        parts = module.split(".")
        if parts[0] == package:
            target = parts[1] if len(parts) > 1 else "<package root>"
            assert target in ALLOWED[layer], f"{path.name} ({layer}) must not import {module}"
        elif layer == "domain":
            assert parts[0] not in FORBIDDEN_IN_DOMAIN, f"{path.name} (domain) must not import {module}"


def test_every_service_module_lives_in_a_layer():
    for base in PACKAGES.values():
        loose = {p.name for p in base.glob("*.py")} - {"__init__.py", "handler.py"}
        assert not loose, f"modules outside a layer folder in {base.name}: {sorted(loose)}"
