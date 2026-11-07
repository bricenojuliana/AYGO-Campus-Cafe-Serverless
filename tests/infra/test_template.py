"""Asserts architecture rules on template.yaml so they cannot silently regress."""
import re
from pathlib import Path

import pytest
import yaml

from orders_service.api.auth import ROUTE_SCOPES

ROOT = Path(__file__).resolve().parents[2]


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template():
    return yaml.load((ROOT / "template.yaml").read_text(), Loader=CfnLoader)


@pytest.fixture(scope="module")
def resources(template):
    return template["Resources"]


def of_type(resources, type_name):
    return {name: r for name, r in resources.items() if r["Type"] == type_name}


def test_every_integration_is_lambda_proxy_payload_2(resources):
    integrations = of_type(resources, "AWS::ApiGatewayV2::Integration")
    assert len(integrations) == 2
    for integration in integrations.values():
        assert integration["Properties"]["IntegrationType"] == "AWS_PROXY"
        assert integration["Properties"]["PayloadFormatVersion"] == "2.0"


def test_routes_match_the_contract(resources):
    routes = {r["Properties"]["RouteKey"]: r["Properties"]["Target"]["Sub"]
              for r in of_type(resources, "AWS::ApiGatewayV2::Route").values()}
    assert routes == {
        "GET /products": "integrations/${CatalogIntegration}",
        "GET /products/{productId}": "integrations/${CatalogIntegration}",
        "POST /orders": "integrations/${OrdersIntegration}",
        "GET /orders/{orderId}": "integrations/${OrdersIntegration}",
        "POST /orders/{orderId}/cancel": "integrations/${OrdersIntegration}",
    }


def test_products_are_public_and_each_orders_route_needs_the_jwt_and_its_scope(resources):
    """The gateway requires exactly the scopes the Orders handler checks again (api/auth.py ROUTE_SCOPES)."""
    routes = {r["Properties"]["RouteKey"]: r["Properties"] for r in of_type(resources, "AWS::ApiGatewayV2::Route").values()}
    for key in ("GET /products", "GET /products/{productId}"):
        assert routes[key]["AuthorizationType"] == "NONE"
        assert "AuthorizerId" not in routes[key] and "AuthorizationScopes" not in routes[key]
    orders = {key: route for key, route in routes.items() if key.split(" ", 1)[1].startswith("/orders")}
    assert {key: route["AuthorizationScopes"] for key, route in orders.items()} == \
        {key: [scope] for key, scope in ROUTE_SCOPES.items()}
    for route in orders.values():
        assert route["AuthorizationType"] == "JWT"
        assert route["AuthorizerId"] == {"Ref": "OrdersJwtAuthorizer"}


def test_no_catch_all_route_could_skip_the_authorizer(resources):
    keys = {r["Properties"]["RouteKey"] for r in of_type(resources, "AWS::ApiGatewayV2::Route").values()}
    assert "$default" not in keys
    assert not any(key.startswith(("ANY ", "OPTIONS ")) or "{proxy+}" in key for key in keys)


def test_authorizer_accepts_only_auth0_access_tokens_for_this_api(template, resources):
    authorizers = of_type(resources, "AWS::ApiGatewayV2::Authorizer")
    assert list(authorizers) == ["OrdersJwtAuthorizer"]
    props = authorizers["OrdersJwtAuthorizer"]["Properties"]
    assert props["AuthorizerType"] == "JWT"
    assert props["IdentitySource"] == ["$request.header.Authorization"]
    assert props["JwtConfiguration"] == {"Issuer": {"Ref": "Auth0Issuer"}, "Audience": [{"Ref": "Auth0Audience"}]}
    params = template["Parameters"]
    issuer = re.compile(params["Auth0Issuer"]["AllowedPattern"])
    assert issuer.match("https://campus-cafe.us.auth0.com/")
    for bad in ("https://campus-cafe.us.auth0.com", "http://campus-cafe.us.auth0.com/", "campus-cafe.us.auth0.com"):
        assert not issuer.match(bad)  # exact iss string: https and the trailing slash matter
    assert "Default" not in params["Auth0Issuer"] and "Default" not in params["Auth0Audience"]


def test_adding_authentication_created_no_iam_resources(resources):
    """Authentication lives in API Gateway and Auth0: no new roles, policies or broadened grants."""
    iam = {name for name, r in resources.items() if r["Type"].startswith("AWS::IAM::")}
    assert iam == {"CatalogRole", "OrdersRole"}


def test_no_sam_api_events_that_would_bypass_the_explicit_api(resources):
    for function in of_type(resources, "AWS::Serverless::Function").values():
        assert "Events" not in function["Properties"]


def role_grants(resources, role_name):
    """{action: {resource}} granted by a role's inline policies."""
    grants = {}
    for policy in resources[role_name]["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            assert statement["Effect"] == "Allow"
            for action in statement["Action"]:
                grants.setdefault(action, set()).add(statement["Resource"]["GetAtt"])
    return grants


def test_each_service_role_reaches_only_its_own_table_and_log_group(resources):
    assert resources["CatalogFunction"]["Properties"]["Role"] == {
        "If": ["CreateServiceRoles", {"GetAtt": "CatalogRole.Arn"}, {"Ref": "ExecutionRoleArn"}]}
    assert resources["OrdersFunction"]["Properties"]["Role"] == {
        "If": ["CreateServiceRoles", {"GetAtt": "OrdersRole.Arn"}, {"Ref": "ExecutionRoleArn"}]}
    assert role_grants(resources, "CatalogRole") == {
        "dynamodb:GetItem": {"CatalogTable.Arn"}, "dynamodb:Scan": {"CatalogTable.Arn"},
        "logs:CreateLogStream": {"CatalogLogGroup.Arn"}, "logs:PutLogEvents": {"CatalogLogGroup.Arn"},
    }
    assert role_grants(resources, "OrdersRole") == {
        "dynamodb:GetItem": {"OrdersTable.Arn"}, "dynamodb:PutItem": {"OrdersTable.Arn"},
        "dynamodb:ConditionCheckItem": {"OrdersTable.Arn"},
        "logs:CreateLogStream": {"OrdersLogGroup.Arn"}, "logs:PutLogEvents": {"OrdersLogGroup.Arn"},
    }
    for role in of_type(resources, "AWS::IAM::Role").values():
        assert "ManagedPolicyArns" not in role["Properties"]  # no broad AWS-managed policies
    for function in of_type(resources, "AWS::Serverless::Function").values():
        assert "Policies" not in function["Properties"]  # SAM would add AWSLambdaBasicExecutionRole
    assert len(of_type(resources, "AWS::DynamoDB::Table")) == 2


def test_least_privilege_roles_are_the_default_and_a_shared_role_is_opt_in(template, resources):
    parameter = template["Parameters"]["ExecutionRoleArn"]
    assert parameter["Default"] == ""
    assert template["Conditions"]["CreateServiceRoles"] == {"Equals": [{"Ref": "ExecutionRoleArn"}, ""]}
    for role in ("CatalogRole", "OrdersRole"):
        assert resources[role]["Condition"] == "CreateServiceRoles"
    pattern = re.compile(parameter["AllowedPattern"])
    assert pattern.match("") and pattern.match("arn:aws:iam::123456789012:role/LabRole")
    for bad in ("LabRole", "arn:aws:iam::123:role/LabRole", "arn:aws:iam::123456789012:user/someone"):
        assert not pattern.match(bad)


def test_orders_table_is_retained_and_has_ttl(resources):
    table = resources["OrdersTable"]
    assert table["DeletionPolicy"] == "Retain" and table["UpdateReplacePolicy"] == "Retain"
    assert table["Properties"]["TimeToLiveSpecification"] == {"AttributeName": "expiresAt", "Enabled": True}


def test_cors_is_scoped_to_the_frontend(resources):
    cors = resources["CafeHttpApi"]["Properties"]["CorsConfiguration"]
    assert cors["AllowOrigins"] == [{"Ref": "FrontendOrigin"}]
    assert sorted(cors["AllowHeaders"]) == ["authorization", "content-type", "idempotency-key"]
    assert "ExposeHeaders" not in cors  # the UI reads no response headers
    assert cors["AllowCredentials"] is False


def test_orders_calls_catalog_through_the_gateway(template, resources):
    env = resources["OrdersFunction"]["Properties"]["Environment"]["Variables"]
    assert env["CATALOG_BASE_URL"] == {"Sub": "https://${CafeHttpApi}.execute-api.${AWS::Region}.${AWS::URLSuffix}"}
    assert env["CATALOG_BUDGET_SECONDS"] == {"Ref": "CatalogBudgetSeconds"}
    timeout = resources["OrdersFunction"]["Properties"]["Timeout"]
    params = template["Parameters"]
    # Even the largest allowed budget leaves time for the DynamoDB write inside the Lambda timeout.
    assert params["CatalogBudgetSeconds"]["MaxValue"] + 1 < timeout
    assert params["CatalogTimeoutSeconds"]["MaxValue"] <= params["CatalogBudgetSeconds"]["MaxValue"]
    assert resources["OrdersIntegration"]["Properties"]["TimeoutInMillis"] > timeout * 1000


def test_settings_come_from_parameters_not_from_code(template, resources):
    """Each configurable value has one source: a template parameter passed as an environment variable."""
    shared = template["Globals"]["Function"]["Environment"]["Variables"]
    assert shared["CURRENCY"] == {"Ref": "Currency"} and shared["LOG_LEVEL"] == {"Ref": "LogLevel"}
    orders = resources["OrdersFunction"]["Properties"]["Environment"]["Variables"]
    assert orders["CATALOG_TIMEOUT_SECONDS"] == {"Ref": "CatalogTimeoutSeconds"}
    assert orders["CATALOG_MAX_ATTEMPTS"] == {"Ref": "CatalogMaxAttempts"}
    assert orders["IDEMPOTENCY_TTL_HOURS"] == {"Ref": "IdempotencyTtlHours"}
    assert "METRICS_NAMESPACE" not in orders  # a constant in api/observability.py, the same everywhere
    assert template["Parameters"]["Currency"]["AllowedPattern"] == "^[A-Z]{3}$"
    assert template["Outputs"]["Currency"]["Value"] == {"Ref": "Currency"}  # read by scripts/seed_catalog.py


def test_access_log_records_authorizer_errors_but_never_the_token(resources):
    log_format = resources["CafeApiStage"]["Properties"]["AccessLogSettings"]["Format"]
    assert "$context.authorizer.error" in log_format
    assert "$request.header" not in log_format and "authorization" not in log_format.lower().replace("authorizer", "")


def test_access_logs_and_function_logs_are_configured(resources):
    stage = resources["CafeApiStage"]["Properties"]
    assert stage["AccessLogSettings"]["DestinationArn"] == {"GetAtt": "ApiAccessLogGroup.Arn"}
    for name in ("CatalogFunction", "OrdersFunction"):
        assert "LogGroup" in resources[name]["Properties"]["LoggingConfig"]


def test_runtime_declared_and_no_hardcoded_resource_names(template, resources):
    assert template["Globals"]["Function"]["Runtime"] == "python3.13"
    for r in resources.values():
        props = r.get("Properties", {})
        assert "TableName" not in props and "FunctionName" not in props or r["Type"] == "AWS::Lambda::Permission"


def test_no_credentials_in_repository():
    # Patterns of real secrets, not the words themselves (scripts legitimately mention them).
    suspicious = [re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), re.compile(r"aws_secret_access_key\s*=\s*\S+"),
                  re.compile(r"BEGIN (RSA |EC )?PRIVATE KEY"),
                  # a JWT (header.payload.signature, both JSON parts base64url "eyJ..."): bearer tokens must never be committed
                  re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")]
    for path in ROOT.rglob("*"):
        if path.is_file() and not {".venv", ".aws-sam", ".pytest_cache", "tests"} & set(path.parts) \
                and path.suffix in {".py", ".yaml", ".toml", ".js", ".html", ".json", ".md", ".sh", ".txt", ".csv"}:
            text = path.read_text(errors="ignore")
            assert not any(pattern.search(text) for pattern in suspicious), path
