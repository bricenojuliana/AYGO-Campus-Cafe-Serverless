#!/usr/bin/env bash
# Serves the browser UI on the origin allowed by CORS (FrontendOrigin, default http://localhost:8080).
set -euo pipefail
cd "$(dirname "$0")/../frontend"
exec python3 -m http.server "${PORT:-8080}" --bind 127.0.0.1
