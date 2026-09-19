#!/usr/bin/env bash
# Print the HyperDX Personal API Access Key, which is what the ClickStack MCP server
# authenticates with. This is NOT the ingestion key that setup.sh captures for the collector.
#
#   export CLICKSTACK_PERSONAL_API_KEY=$(./personal-key.sh)
#
# Same value as HyperDX -> Team Settings -> API Keys.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"

KEY=$(docker compose exec -T clickstack sh -c "
  rm -f /tmp/ck
  curl -s -c /tmp/ck -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d '{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}' http://localhost:8000/login/password
  curl -s -b /tmp/ck http://localhost:8000/me | sed -n 's/.*\"accessKey\":\"\([^\"]*\)\".*/\1/p'
" </dev/null | tr -d '[:space:]')

[ -n "$KEY" ] || { echo "could not retrieve the key -- is the stack up?" >&2; exit 1; }
printf '%s\n' "$KEY"
