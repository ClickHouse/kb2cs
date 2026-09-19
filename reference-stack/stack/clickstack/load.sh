#!/usr/bin/env bash
# Ship the dataset into ClickStack and wait until it has actually landed.
#
#   ./load.sh
#
# The OTel collector tails files and never exits on its own, so this starts it, watches
# otel_logs until the row count stops moving, then stops it. Roughly 1-2 minutes.
set -uo pipefail

EXPECTED=1011404
CH="docker compose exec -T clickstack clickhouse-client --query"

count() { $CH "SELECT count() FROM default.otel_logs" 2>/dev/null | tr -d '[:space:]'; }

before=$(count); before=${before:-0}
echo "[load] otel_logs currently holds ${before} rows"
if [ "$before" -gt 0 ]; then
  echo "[load] refusing to append to a non-empty table -- that is how you get duplicates."
  echo "[load] to reload:  docker compose exec clickstack clickhouse-client --query 'TRUNCATE TABLE default.otel_logs'"
  echo "[load]             docker compose down load && docker volume rm nginx-training-clickstack_checkpoints"
  exit 1
fi

# `docker compose up -d` returns as soon as containers START, not when `setup` has finished
# registering the team -- and until it has, the bundled collector runs `receivers: [nop]` and
# 4317/4318 are not bound at all. Starting the shipper before that gives a stream of
# "connection refused" retries and a load that never lands. Gate on the port being live.
echo "[load] waiting for the OTLP receiver to accept connections"
ready=0
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -X POST http://localhost:4318/v1/logs \
         -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
  # 401 is fine here: the port is bound and rejecting an unauthenticated probe
  case "$code" in 200|401) ready=1; break ;; esac
  sleep 2
done
if [ "$ready" != "1" ]; then
  echo "[load] OTLP never came up. Check setup:  docker compose logs setup"
  exit 1
fi
echo "[load] OTLP is ready"

# The filelog receiver remembers how far it read. If otel_logs is empty but checkpoints
# survive from a previous run -- which is exactly what a plain `docker compose down -v`
# leaves behind, since it skips profiled services -- the collector resumes at EOF and ships
# nothing at all. Clear them; we only get here when the table is empty anyway.
docker compose rm -sf load >/dev/null 2>&1
if docker volume rm nginx-training-clickstack_checkpoints >/dev/null 2>&1; then
  echo "[load] cleared stale filelog checkpoints"
fi

echo "[load] starting the collector"
docker compose up -d load >/dev/null 2>&1 || { echo "[load] could not start the load service"; exit 1; }

echo "[load] waiting for ingest to settle (expecting ${EXPECTED} rows)"
last=-1; stable=0
for i in $(seq 1 120); do
  sleep 3
  now=$(count); now=${now:-0}
  printf "\r[load]   %s rows" "$now"
  if [ "$now" = "$last" ] && [ "$now" -gt 0 ]; then
    stable=$((stable+1))
    [ "$stable" -ge 4 ] && break      # 12s with no change
  else
    stable=0
  fi
  last=$now
done
echo

echo "[load] stopping the collector"
docker compose stop load >/dev/null 2>&1

final=$(count); final=${final:-0}
if [ "$final" = "$EXPECTED" ]; then
  echo "[load] done: ${final} rows, exactly as expected"
  echo "[load] now run ./verify.sh"
else
  echo "[load] WARNING: ${final} rows, expected ${EXPECTED}"
  echo "[load] last collector errors:"
  docker compose logs load 2>&1 | grep -iE '\berror\b' | tail -5 | sed 's/^/    /'
  echo "[load] full logs:  docker compose logs load"
  exit 1
fi
