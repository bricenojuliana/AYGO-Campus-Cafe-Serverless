"""Static checks on the browser UI: official Auth0 SDK, access token (never the ID token) only on the
Orders calls, no secrets in the committed configuration. The real login is verified manually."""
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orders_service.api.auth import ROUTE_SCOPES

ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "frontend/index.html").read_text()
APP = (ROOT / "frontend/app.js").read_text()
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests/e2e")]

from collect_evidence import FRONTEND_CONFIG_KEYS, inspect_frontend_config  # noqa: E402
from local_gateway import emulated_authorizer  # noqa: E402


def test_official_auth0_sdk_is_pinned_and_integrity_checked():
    tag = re.search(r'<script src="https://cdn\.jsdelivr\.net/npm/@auth0/auth0-spa-js@(\d+\.\d+\.\d+)/[^"]+"'
                    r'\s+integrity="(sha384-[A-Za-z0-9+/=]+)"\s+crossorigin="anonymous"></script>', INDEX)
    assert tag, "the SDK must be loaded from an exact version with an integrity hash"
    assert INDEX.index("auth0-spa-js") < INDEX.index('src="config.js"') < INDEX.index('src="app.js"')


def test_no_client_secret_and_no_id_token_sent_to_the_api():
    for forbidden in ("clientSecret", "client_secret", "getIdTokenClaims", "__raw", "id_token", "localstorage"):
        assert forbidden not in APP, forbidden
    assert "getTokenSilently" in APP and "loginWithRedirect" in APP


def test_access_token_is_requested_for_the_api_audience_with_the_route_scopes():
    assert re.search(r"audience:\s*CONFIG\.auth0Audience", APP)
    requested = re.search(r'const ORDERS_SCOPES = "([^"]+)";', APP).group(1).split()
    assert sorted(requested) == sorted(ROUTE_SCOPES.values())


def api_calls(source):
    """Full argument text of every `api(...)` call, with nested parentheses balanced."""
    for match in re.finditer(r"\bapi\(", source):
        depth, i = 1, match.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(source[i], 0)
            i += 1
        yield source[match.end():i - 1]


def test_only_orders_calls_carry_the_token():
    by_path = {}
    for args in api_calls(APP.split("async function api(")[1].split("\n}\n", 1)[1]):  # calls, not the definition
        path = re.match(r'"(?:GET|POST)",\s*[`"](/[a-z]+)', args).group(1)
        by_path.setdefault(path, set()).add(bool(re.search(r"\bauth:\s*true\b", args)))
    assert by_path == {"/products": {False}, "/orders": {True}}
    assert APP.count("authorization = `Bearer ${await accessToken()}`") == 1  # one place adds the header


def test_committed_example_config_has_only_public_keys_and_real_config_is_not_committed():
    ok, keys = inspect_frontend_config((ROOT / "frontend/config.example.js").read_text())
    assert ok and keys == FRONTEND_CONFIG_KEYS
    tracked = subprocess.run(["git", "ls-files", "frontend"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "frontend/config.example.js" in tracked and "frontend/config.js" not in tracked.split()


@pytest.mark.parametrize("config_js, ok", [
    ('window.C = {apiBaseUrl: "https://x", auth0ClientId: "abc"};', True),
    ('window.C = {apiBaseUrl: "https://x", clientSecret: "abc"};', False),
    ('window.C = {apiBaseUrl: "AKIAABCDEFGHIJKLMNOP"};', False),
    ('window.C = {apiBaseUrl: "https://x", extra: "1"};', False),
])
def test_evidence_check_accepts_public_values_and_rejects_secrets(config_js, ok):
    assert inspect_frontend_config(config_js)[0] is ok


# ---------- the TEST-ONLY authorizer emulation in tests/e2e/local_gateway.py ----------

CONFIG = {"auth0Domain": "tenant.example.com", "auth0Audience": "https://campus-cafe-api"}


def bearer(**claims):
    base = {"iss": "https://tenant.example.com/", "sub": "auth0|alice", "aud": ["https://campus-cafe-api"],
            "exp": time.time() + 60, "scope": "orders:create orders:read orders:cancel"}
    payload = base64.urlsafe_b64encode(json.dumps({**base, **claims}).encode()).decode().rstrip("=")
    return {"authorization": f"Bearer header.{payload}.signature"}


@pytest.mark.parametrize("headers, status", [
    ({}, 401),
    (bearer(), 200),
    (bearer(aud="https://dev.example.com/api/v2/"), 401),   # another API's token
    (bearer(iss="https://other.example.com/"), 401),
    (bearer(exp=time.time() - 1), 401),
    (bearer(scope="orders:read"), 403),
])
def test_emulated_authorizer_mirrors_the_gateway_checks(headers, status):
    assert emulated_authorizer(headers, "POST /orders", CONFIG, ROUTE_SCOPES)[0] == status
