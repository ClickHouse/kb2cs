#!/bin/sh
# Install the Fleet nginx integration: index templates, ingest pipelines, dashboards.
# Idempotent -- safe to re-run.
set -eu

KIBANA="${KIBANA:-http://kibana:5601}"
AUTH="elastic:${ELASTIC_PASSWORD:-changeme}"
H_XSRF="kbn-xsrf: true"
H_JSON="Content-Type: application/json"

say() { echo "[setup] $*"; }

say "waiting for Kibana to report available"
i=0
until curl -sf -u "$AUTH" "$KIBANA/api/status" 2>/dev/null | grep -q '"level":"available"'; do
  i=$((i + 1))
  [ "$i" -gt 120 ] && { say "Kibana never became available"; exit 1; }
  sleep 5
done
say "Kibana is up"

# Fleet has to be initialised before packages can be installed. This creates the default
# policies and the .fleet-* indices. It is slow the first time (30-60s is normal).
say "initialising Fleet (this takes a minute on first run)"
curl -sf -u "$AUTH" -X POST "$KIBANA/api/fleet/setup" -H "$H_XSRF" -H "$H_JSON" >/dev/null
say "Fleet initialised"

# Install the nginx package. Without a version Kibana resolves the latest compatible one.
say "installing the nginx integration from the package registry"
RESP="$(curl -s -u "$AUTH" -X POST "$KIBANA/api/fleet/epm/packages/nginx" \
  -H "$H_XSRF" -H "$H_JSON" -d '{"force":true}')"

echo "$RESP" | grep -q '"items"' || {
  say "install failed:"
  echo "$RESP" | head -c 600
  say ""
  say "If this is a network error, you have no route to epr.elastic.co."
  say "See the README for running a local package registry instead."
  exit 1
}

VER="$(curl -s -u "$AUTH" "$KIBANA/api/fleet/epm/packages/nginx" -H "$H_XSRF" \
  | sed -n 's/.*"latestVersion":"\([^"]*\)".*/\1/p')"
say "nginx integration installed (latest available: ${VER:-unknown})"

# What we actually care about: the pipelines that parse the logs, and the dashboards.
PIPES="$(echo "$RESP" | tr ',' '\n' | grep -c 'ingest_pipeline' || true)"
DASH="$(echo "$RESP" | tr ',' '\n' | grep -c 'dashboard' || true)"
say "assets installed: ~$PIPES ingest pipelines, ~$DASH dashboards"

say "done. Kibana: http://localhost:5601  ->  Analytics > Dashboard > search 'nginx'"
say "now load the data:  docker compose run --rm load"
