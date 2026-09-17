#!/bin/sh
# Register a HyperDX team and capture its ingestion API key.
# Idempotent: if the key file already exists and still works, does nothing.
set -eu

API="http://localhost:8000"
KEYFILE=/secrets/api-key
EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"

say() { echo "[setup] $*"; }

say "waiting for the HyperDX API"
i=0
until curl -sf "$API/health" >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 90 ] && { say "API never came up"; exit 1; }
  sleep 2
done

# Fast path: a key we wrote earlier, still accepted by the ingest endpoint.
if [ -s "$KEYFILE" ]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:4318/v1/logs \
    -H 'Content-Type: application/json' -H "authorization: $(cat $KEYFILE)" -d '{}' 2>/dev/null || echo 000)
  if [ "$CODE" = "200" ]; then
    say "existing API key still valid, nothing to do"
    exit 0
  fi
  say "stored API key no longer works (HTTP $CODE), re-provisioning"
fi

if curl -s "$API/installation" | grep -q '"isTeamExisting":true'; then
  # Happens whenever the mongo volume outlives the secrets volume, e.g. after removing just
  # one of them. Log in with the same credentials and read the key back.
  say "a team already exists; logging in to retrieve its key"
  curl -s -c /tmp/cookie -X POST "$API/login/password" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" >/dev/null
else
  say "registering team for $EMAIL"
  curl -s -c /tmp/cookie -X POST "$API/register/password" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"confirmPassword\":\"$PASS\"}" >/dev/null
fi

KEY=$(curl -s -b /tmp/cookie "$API/team" | sed -n 's/.*"apiKey":"\([^"]*\)".*/\1/p')
if [ -z "$KEY" ]; then
  say "could not read the API key from GET /team"
  say "if the team was created with different credentials, either set HDX_EMAIL/HDX_PASSWORD"
  say "to match, or run 'docker compose down -v' to start from scratch"
  exit 1
fi

# no trailing newline: this value becomes an HTTP header verbatim
printf '%s' "$KEY" > "$KEYFILE"
say "API key captured (${KEY%%-*}-...) and written to the secrets volume"

# Registering a team flips the bundled collector off `receivers: [nop]`; give it a moment.
say "waiting for OTLP ingest to come online"
i=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:4318/v1/logs \
        -H 'Content-Type: application/json' -H "authorization: $KEY" -d '{}' 2>/dev/null)" = "200" ]; do
  i=$((i+1)); [ "$i" -gt 60 ] && { say "OTLP never accepted data"; exit 1; }
  sleep 2
done
say "OTLP ingest is live on 4317/4318"
say "next:  docker compose run --rm load"
