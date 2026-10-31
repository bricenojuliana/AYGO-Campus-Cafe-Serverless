#!/usr/bin/env bash
# Writes frontend/config.js from the deployed stack's outputs. Uses your normal AWS CLI credentials.
#   AUTH0_CLIENT_ID=<SPA client ID> scripts/configure_frontend.sh [stack]
# The client ID belongs to the Auth0 SPA application, not to the stack, so it is passed in. All values are
# public; never put a client secret here.
set -euo pipefail
STACK="${1:-campus-cafe}"
: "${AUTH0_CLIENT_ID:?set AUTH0_CLIENT_ID to the Client ID of the Auth0 SPA application}"
cd "$(dirname "$0")/.."
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
URL=$(output ApiUrl)
ISSUER=$(output Auth0Issuer)
AUDIENCE=$(output Auth0Audience)
[[ "$URL" == https://* ]] || { echo "ApiUrl not found for stack $STACK" >&2; exit 1; }
[[ "$ISSUER" == https://*/ ]] || { echo "Auth0Issuer not found for stack $STACK (redeploy it)" >&2; exit 1; }
DOMAIN="${ISSUER#https://}"
DOMAIN="${DOMAIN%/}"
cat > frontend/config.js <<CONFIG
// Written by scripts/configure_frontend.sh from the stack outputs. Public values only: no secrets here.
window.CAMPUS_CAFE_CONFIG = {
  apiBaseUrl: "$URL",
  auth0Domain: "$DOMAIN",
  auth0ClientId: "$AUTH0_CLIENT_ID",
  auth0Audience: "$AUDIENCE"
};
CONFIG
echo "frontend/config.js -> API $URL, Auth0 $DOMAIN, audience $AUDIENCE"
