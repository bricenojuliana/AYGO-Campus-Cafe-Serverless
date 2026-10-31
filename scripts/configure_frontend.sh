#!/usr/bin/env bash
# Writes frontend/config.js with the deployed ApiUrl. Uses your normal AWS CLI credentials.
set -euo pipefail
STACK="${1:-campus-cafe}"
cd "$(dirname "$0")/.."
URL=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)
[[ "$URL" == https://* ]] || { echo "ApiUrl not found for stack $STACK" >&2; exit 1; }
printf '// Written by scripts/configure_frontend.sh from the stack'"'"'s ApiUrl output. No secrets belong here.\nwindow.CAMPUS_CAFE_CONFIG = {\n  apiBaseUrl: "%s"\n};\n' "$URL" > frontend/config.js
echo "frontend/config.js -> $URL"
