# Campus Café — Serverless Ordering Application

Ordering application for a campus café: a locally served web interface, **API Gateway HTTP API (payload 2.0)**, two **Python 3.13** Lambdas (Catalog and Orders), one DynamoDB table per service and logs in CloudWatch. All infrastructure is created with **AWS SAM** (`template.yaml`).

Users sign in with **Auth0** (Authorization Code Flow with PKCE). The menu is public; every order operation needs an Auth0 access token checked by the gateway's **JWT authorizer**, and each user can only see and cancel their own orders. See [Authentication and authorization](#authentication-and-authorization-auth0).

![Campus Café architecture](docs/architecture.png)

**Demo** — the local browser UI calling the deployed API:

https://github.com/user-attachments/assets/9d5d8ba3-3dea-425c-a212-0e6836cb9025

* API contract: [docs/openapi.yaml](docs/openapi.yaml) (OpenAPI 3.0).
* Design decisions (service and data boundaries, atomic idempotency, durable state, bounded Catalog call): [docs/DESIGN.md](docs/DESIGN.md).
* Actual check output and measurements against AWS: [docs/evidence/](docs/evidence/README.md).
* Demo video of the application working: [docs/evidence/demo-ui.webm](docs/evidence/demo-ui.webm).

## Structure

| Path | Contents |
|---|---|
| `template.yaml` | API, stage with access logs, 5 routes, 2 functions, 2 tables, log groups, least-privilege IAM, parameters |
| `samconfig.toml` | stack name and deployment options (no credentials) |
| `services/catalog/src/catalog_service/` | `handler.py` (2.0 event) → `domain/` (rules), `adapters/` (DynamoDB), `api/` (HTTP responses, logs) |
| `services/orders/src/orders_service/` | `handler.py` → `application/` (use cases) + `domain/` (rules) → `adapters/` (DynamoDB, Catalog over HTTPS); `api/` (HTTP, auth, logs) |
| `frontend/` | HTML/CSS/JS; `config.example.js` is the template for the generated, uncommitted `config.js` (API URL only) |
| `seed/products.json` | 10 typical Colombian café products (COP in cents: `priceCents` 200000 = $2.000), 1 unavailable (jugo de lulo) |
| `scripts/` | seed, frontend configuration, local server, smoke test of the deployed API |
| `tests/` | unit, adapters (moto + local HTTP server), infra (template rules and OpenAPI conformance), `e2e/local_gateway.py` (tests only) |
| `docs/` | `architecture.png` (diagram), `openapi.yaml` (contract), `DESIGN.md` (design) and `evidence/` (smoke and evidence output against the deployed stack) |

## Requirements

* **To develop and test on your machine:** Python 3.12 or later. No AWS credentials needed.

  ```bash
  python3 -m venv .venv
  ```

  ```bash
  .venv/bin/pip install -r requirements-dev.txt
  ```

* **To deploy:** access to the AWS console. Deployment runs in **AWS CloudShell**, a terminal inside the console that already includes `aws` and `sam` and uses the session you signed in with. Credentials are **never** written to the repository or the chat.

## Authentication and authorization (Auth0)

| Route | Access | Enforced by |
|---|---|---|
| `GET /products`, `GET /products/{productId}` | public (synthetic catalog) | — |
| `POST /orders` | access token with `orders:create` | gateway JWT authorizer + Orders |
| `GET /orders/{orderId}` | access token with `orders:read` **and** being the order's owner | gateway (token, scope) + Orders (owner) |
| `POST /orders/{orderId}/cancel` | access token with `orders:cancel`, owner, order still `CREATED` | gateway + Orders + DynamoDB condition |

The owner is the verified `(iss, sub)` of the token, stored on the order by the server. Someone else's order, a missing order and a legacy order without owner all get the same 404. Idempotency keys are scoped to that owner. Details: [docs/DESIGN.md](docs/DESIGN.md) §8.

### Auth0 setup (once, in the Auth0 dashboard)

1. **API** — *Applications → APIs → Create API*: name `Campus Café API`, **Identifier** e.g. `https://campus-cafe-api` (this is the *audience*), signing algorithm **RS256**. In *Permissions* add `orders:create`, `orders:read`, `orders:cancel`. In *Settings* turn on **Enable RBAC** (the gateway reads the `scope` claim, so "Add Permissions in the Access Token" is not needed).
2. **SPA** — *Applications → Applications → Create Application* → **Single Page Web Application**. In *Settings → Application URIs* set **Allowed Callback URLs**, **Allowed Logout URLs** and **Allowed Web Origins** to `http://localhost:8080`. Do not use a client secret anywhere.
3. **Roles and test users** — *User Management → Roles*: a role `Customer` with the three permissions, assigned to the synthetic users **Alice** and **Bob** (e.g. `alice@example.com`, `bob@example.com`). Optional for check G5: a user **Carol** with a role that has only `orders:read`.

Values you will need (all public, never secrets):

| Value | Where | Used by |
|---|---|---|
| Domain, e.g. `your-tenant.us.auth0.com` | SPA → Settings | frontend `auth0Domain` |
| Issuer `https://<domain>/` (with the trailing slash) | derived from the domain | `Auth0Issuer` stack parameter |
| API Identifier, e.g. `https://campus-cafe-api` | API → Settings | `Auth0Audience` parameter and frontend `auth0Audience` |
| Client ID | SPA → Settings | frontend `auth0ClientId` |

## 1. Local automated tests (no AWS)

```bash
.venv/bin/python -m pytest
```

Runs the domain/service/handler unit tests, the real adapters against moto (DynamoDB) and against a local HTTP server (Catalog timeouts and retries), the template rules, and the OpenAPI document (valid OpenAPI 3.0, same routes as the template, schemas matching real responses).

## 2. Deployment with AWS SAM (from CloudShell)

This is the only deployment path. The same commands work in a local terminal if you have an AWS profile configured; in that case use `.venv/bin/python` instead of `python3`.

### 2.1 Package and upload the project

On your machine, build the package (without `.venv`, `.aws-sam` or caches):

```bash
zip -qr dist/campus-cafe.zip . -x '.venv/*' '.aws-sam/*' 'dist/*' '*__pycache__*' '.pytest_cache/*' '*.pyc' '.git/*'
```

In the AWS console, choose the region you will deploy to and open **CloudShell** (the `>_` icon in the top bar). Use **Actions → Upload file** to upload `dist/campus-cafe.zip`. Then, in CloudShell:

```bash
mkdir -p ~/campus-cafe && cd ~/campus-cafe && unzip -o ~/campus-cafe.zip
```

```bash
python3 -c "import boto3" || pip3 install --user boto3
```

boto3 is only used by the seed and verification scripts. The scripts work with Python 3.9 or later. The CloudShell home directory persists per region, but idle sessions are closed.

To **update** an existing deployment with a new zip, keep the `samconfig.toml` saved by `sam deploy --guided`, because it holds your `ExecutionRoleArn`. Then deploy without `--guided`:

```bash
cd ~/campus-cafe && unzip -o ~/campus-cafe.zip -x samconfig.toml
```

```bash
sam build && sam deploy
```

### 2.2 Check the account, region and tools

```bash
aws sts get-caller-identity
```

```bash
echo "$AWS_REGION"
```

```bash
sam --version
```

Declared runtime: `python3.13` (Globals → `Runtime`). It is the Python version CloudShell ships with, so `sam build` works there. According to the Lambda runtimes page it is supported on Amazon Linux 2023, with deprecation on June 30, 2029. Confirm your region in the [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) table. If the runtime were unavailable, changeset creation would fail before any resource is created.

### 2.3 Validate, build and deploy

**AWS Academy Learner Lab accounts:** the `voclabs` role does not allow `iam:CreateRole`, so the deployment fails when creating `CatalogRole` and `OrdersRole`. In that case the functions use the role the lab already provides, `LabRole`. Copy its ARN:

```bash
aws iam get-role --role-name LabRole --query Role.Arn --output text
```

Paste it when `sam deploy --guided` asks for `Parameter ExecutionRoleArn`. In an account that does allow creating roles, leave that parameter empty.

`sam deploy --guided` also asks for `Parameter Auth0Issuer` (e.g. `https://your-tenant.us.auth0.com/`, **with** the trailing slash) and `Parameter Auth0Audience` (the API Identifier). Run the guided mode again whenever a new parameter appears: it keeps your previous answers, including `ExecutionRoleArn`. Do **not** pass only the Auth0 values with `--parameter-overrides`: that replaces the whole saved list, drops `ExecutionRoleArn` and the deployment fails again on `iam:CreateRole`.

```bash
sam validate --lint
```

```bash
sam build
```

```bash
sam deploy --guided
```

* `sam build` needs a `python3.13` interpreter in the terminal; CloudShell has one. In a terminal without Python 3.13 it fails with "Binary validation failed for python". In that case delete `.aws-sam` and run `sam deploy` without `sam build`: the Lambdas have no external dependencies, and `sam deploy` packages `services/*/src` directly from `template.yaml`.
* If your SAM version does not accept `--lint`, use `sam validate`.

In guided mode accept the `campus-cafe` stack and the region, review the parameters and **read the changeset before confirming it**: it lists every resource and both IAM roles. The parameters are:

| Parameter | Default | Purpose |
|---|---|---|
| `FrontendOrigin` | `http://localhost:8080` | the only origin allowed by CORS |
| `LogRetentionDays` | `14` | retention of the 3 log groups |
| `LogLevel` | `INFO` | log level of both Lambdas |
| `CatalogTimeoutSeconds` | `2` | per-request timeout for Orders → Catalog |
| `CatalogMaxAttempts` | `2` | maximum attempts per product |
| `CatalogBudgetSeconds` | `5` | total budget for the Catalog lookup |
| `Currency` | `COP` | the application's single ISO 4217 currency; prices are integer minor units in it |
| `IdempotencyTtlHours` | `24` | how long Orders remembers an `Idempotency-Key` |
| `Auth0Issuer` | none (required) | Auth0 issuer exactly as in the token, with the trailing slash, e.g. `https://your-tenant.us.auth0.com/` |
| `Auth0Audience` | none (required) | identifier of the Campus Café API in Auth0, e.g. `https://campus-cafe-api` (not the SPA client ID) |
| `ExecutionRoleArn` | none (answer it) | empty answer = create the least-privilege roles; `LabRole` ARN = use that existing role (Learner Lab) |

Resulting IAM permissions (least privilege). Each function uses an explicit role (`CatalogRole`, `OrdersRole`) with no AWS managed policies. `AWSLambdaBasicExecutionRole` is not used, because it would allow writing to any log group.

* **CatalogFunction**: `dynamodb:GetItem` and `dynamodb:Scan` on `CatalogTable` only; `logs:CreateLogStream` and `logs:PutLogEvents` on its own log group only.
* **OrdersFunction**: `dynamodb:GetItem`, `dynamodb:PutItem` and `dynamodb:ConditionCheckItem` on `OrdersTable` only (the transaction is authorized through those actions); logs on its own log group only. It has no access to `CatalogTable`: it looks up products over HTTPS through the gateway.
* API Gateway can invoke each function only for its own routes (`AWS::Lambda::Permission` with `SourceArn`).

**Learner Lab limitation:** with `ExecutionRoleArn` pointing to `LabRole`, `CatalogRole` and `OrdersRole` are not created, and both functions use `LabRole`. That role has much broader permissions, so in that environment **least privilege at runtime is not met**. The restricted roles remain defined in the template, and the tests check that they are the default behavior. The `ExecutionRoleMode` output shows which mode was deployed, and `collect_evidence.py` shows the actual policies of the role in use.

**If a deployment fails:** the stack ends in `ROLLBACK_COMPLETE` and cannot be updated. Delete it with `sam delete --stack-name campus-cafe` before retrying. Because `OrdersTable` is retained, an orphan `campus-cafe-OrdersTable-*` table may remain. Find it with `aws dynamodb list-tables` and delete it with `aws dynamodb delete-table --table-name <name>`.

Stack outputs (names are generated by CloudFormation; there are no fixed names):

```bash
aws cloudformation describe-stacks --stack-name campus-cafe --query "Stacks[0].Outputs" --output table
```

`ApiUrl`, `CatalogTableName`, `OrdersTableName`, `ApiAccessLogGroupName`, `CatalogLogGroupName`, `OrdersLogGroupName`, `CatalogFunctionName`, `OrdersFunctionName`.

## 3. Seed the catalog

In CloudShell:

```bash
python3 scripts/seed_catalog.py --stack campus-cafe
```

It reads `CatalogTableName` and `Currency` from the stack outputs and validates each product with the same rules as the service (a product in another currency is rejected). On a stack deployed before the `Currency` output existed, redeploy or pass `--currency COP`.

## 4. Verify the deployed API

The smoke test needs real **access tokens** of the test users. Tokens are credentials: never paste them into chats, files, commits, screenshots or third-party decoder websites.

**Getting a token.** Sign in to the local UI (section 5) as Alice in her own browser profile, open DevTools → *Network*, click any `/orders` request and copy the value of its `Authorization` request header **after** `Bearer `. In CloudShell, read it without echoing it or saving it in the shell history:

```bash
read -rs CAFE_TOKEN_ALICE && export CAFE_TOKEN_ALICE
```

Repeat for Bob (`CAFE_TOKEN_BOB`) in a second browser profile, and optionally for a user with only `orders:read` (`CAFE_TOKEN_READER`, check G5) and an ID token (`CAFE_ID_TOKEN`, check G6). Then:

```bash
python3 scripts/smoke_test.py --stack campus-cafe
```

To keep the output as evidence, run it as `python3 scripts/smoke_test.py --stack campus-cafe | tee docs/evidence/smoke-$(date -u +%Y-%m-%dT%H%MZ).txt` and download the file. Token values are masked in everything the script prints.

Each check is labeled with its workshop ID and reported as PASS, FAIL or BLOCKED (a token it needs was not provided); it ends with a Markdown table:

* **G1** public product list and detail without a token;
* **G2** every Orders route without a token, and with a tampered token (broken signature) → 401;
* **G3** Alice and Bob each create and read their own order;
* **G4** Bob reads and cancels Alice's order (and Alice Bob's) → 404, Alice's order unchanged;
* **G5** a valid token without the route's scope → 403;
* **G6** an ID token instead of the access token → rejected;
* **S1–S8** over HTTP: forged owner/price/total fields and other invalid input (400), unknown/unavailable product, same-key replay (200) and conflict (409), Bob's independent order with Alice's key, 5 concurrent identical creations → one order, repeated cancellation, uniform 404s;
* CORS preflight with the `Authorization` header, and rejection of other origins.

It creates and cancels real (synthetic) orders.

### Quality attribute evidence

In CloudShell:

```bash
python3 scripts/collect_evidence.py --stack campus-cafe
```

It needs `CAFE_TOKEN_ALICE` exported as above. The script writes `docs/evidence/evidence-<timestamp>.md`. Download it with **Actions → Download file** (path `campus-cafe/docs/evidence/...`). The report covers:

* **Reliability:** 5 concurrent identical creations that must produce a single stored order, whose persisted owner is the token's issuer and subject (subject shortened in the report). After updating the Lambda configuration, it checks that the replay returns the same result.
* **Persistence:** the same order is read after the update.
* **Performance:** 3 warm-up reads and 20 sequential ones. It reports individual timings and p95, and cold starts separately, taken from the `REPORT ... Init Duration` lines.
* **Observability:** follows the creation's `apigw-requestid` from the gateway access log to the Orders logs (with `orderId`) and the Catalog logs (`correlationId`).
* **Fault isolation:** temporarily points Orders' `CATALOG_BASE_URL` to an unreachable URL: first a DNS failure, then a timeout. It checks for the 503 response and that neither the order nor the idempotency record is stored.
* **Security:** inspects `config.js` (only the API URL and public Auth0 values, no secrets) and the actual policies of each role.

The script **temporarily** changes the Orders function environment, outside CloudFormation, and always restores it when it finishes. A later `sam deploy` also resets it. Your user needs `lambda:UpdateFunctionConfiguration`, `logs:FilterLogEvents`, IAM read access and `dynamodb:Scan`/`GetItem` on the orders table.

## 5. Local web interface

In CloudShell, copy the API URL:

```bash
aws cloudformation describe-stacks --stack-name campus-cafe --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text
```

On your machine, copy `frontend/config.example.js` to `frontend/config.js` and fill in `apiBaseUrl` (that URL), `auth0Domain`, `auth0ClientId` and `auth0Audience` (see [Auth0 setup](#auth0-setup-once-in-the-auth0-dashboard)). All four are public values. `config.js` belongs to one deployment, so it is in `.gitignore`. Then start the interface:

```bash
scripts/serve_frontend.sh
```

Open http://localhost:8080 (exactly this origin: it must match the Auth0 URLs and `FrontendOrigin`). The menu and the cart work without login; **Log in** opens Auth0, and the header then shows who is signed in, with the permissions granted to the token. Use a separate browser profile per test user so their sessions do not mix. Each order gets a short code such as `CC-7K3Q-9XMD`, which can be searched in lowercase or without dashes. That origin must match `FrontendOrigin`. If you use another port, redeploy with `--parameter-overrides FrontendOrigin=http://localhost:PORT`.

If you have a local AWS profile, `AUTH0_CLIENT_ID=<SPA client ID> scripts/configure_frontend.sh campus-cafe` writes `config.js` from the stack outputs (`ApiUrl`, `Auth0Issuer`, `Auth0Audience`).

## 6. Logs and metrics

In CloudShell:

```bash
sam logs --stack-name campus-cafe --name OrdersFunction --tail
```

```bash
aws logs tail "$(aws cloudformation describe-stacks --stack-name campus-cafe --query "Stacks[0].Outputs[?OutputKey=='ApiAccessLogGroupName'].OutputValue" --output text)" --follow
```

Orders publishes the `OrdersCreated`, `IdempotentReplays` and `DependencyFailures` metrics in the `CampusCafe` namespace, using Embedded Metric Format.

## 7. Cleanup

`OrdersTable` has `DeletionPolicy: Retain` so orders survive redeployments and stack deletion. In CloudShell, save its name **before** deleting the stack:

```bash
ORDERS_TABLE=$(aws cloudformation describe-stacks --stack-name campus-cafe --query "Stacks[0].Outputs[?OutputKey=='OrdersTableName'].OutputValue" --output text)
```

```bash
sam delete --stack-name campus-cafe
```

`sam delete` removes the API, the functions, `CatalogTable`, the roles and the log groups. The retained table is deleted by hand, irreversibly:

```bash
aws dynamodb delete-table --table-name "$ORDERS_TABLE"
```

* If you no longer have the table name, find it with `aws dynamodb list-tables` (it starts with `campus-cafe-OrdersTable`).
* SAM also leaves the managed bucket `aws-sam-cli-managed-default-*` with the deployment artifacts. You can empty and delete it if you do not use it for other projects.
* Files uploaded to CloudShell are removed with `rm -rf ~/campus-cafe ~/campus-cafe.zip`.

## Local test harness

`tests/e2e/local_gateway.py` runs the real handlers behind a minimal HTTP API 2.0 emulator, with moto DynamoDB, to try the interface without AWS (login still goes to your real Auth0 tenant, configured in `frontend/config.js`). It is **for testing only**: it is not deployed and does not replace API Gateway, Lambda or DynamoDB.

Its JWT authorizer is **emulated**: it reads the token's claims and applies the gateway's issuer, audience, expiry and scope checks, but it does **not** verify the signature. Results obtained through it show Orders' behavior, never that the deployed authorizer works; a tampered-token check (G2) fails there by design. moto also does not isolate concurrent transactions, so the concurrency check (S7) is only meaningful against AWS.

```bash
.venv/bin/python tests/e2e/local_gateway.py
```
