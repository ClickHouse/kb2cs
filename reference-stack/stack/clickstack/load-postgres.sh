#!/usr/bin/env bash
# Ship the POSTGRES query log into ClickStack as a third service.
#
#   export SHIFT_NS=$(SERVICE=postgres ../../ingest/clickstack/shift-ns.sh --align-hour)
#   ./load-postgres.sh
#
# Structurally identical to load.sh, with one deliberate difference: the guard counts
# POSTGRES rows, not all rows. load.sh refuses any non-empty otel_logs, which is right when
# the table is supposed to hold exactly one corpus -- but postgres is additive, so the
# question here is "has postgres already been loaded", not "is the table empty".
set -uo pipefail

EXPECTED=120421          # 120,000 statements + 421 error, warning and checkpoint lines
CH="docker compose exec -T clickstack clickhouse-client --query"

count_postgres() {
  $CH "SELECT count() FROM default.otel_logs WHERE ServiceName = 'postgresql'" 2>/dev/null \
    | tr -d '[:space:]'
}
count_all() { $CH "SELECT count() FROM default.otel_logs" 2>/dev/null | tr -d '[:space:]'; }

before=$(count_postgres); before=${before:-0}
total=$(count_all); total=${total:-0}
echo "[load] otel_logs holds ${total} rows, of which ${before} are postgres"
if [ "$before" -gt 0 ]; then
  echo "[load] postgres is already loaded -- refusing to append and double every postgres number."
  echo "[load] to reload just postgres, leaving nginx untouched:"
  echo "[load]   docker compose exec clickstack clickhouse-client --query \\"
  echo "[load]     \"ALTER TABLE default.otel_logs DELETE WHERE ServiceName = 'postgresql'\""
  echo "[load]   docker compose rm -sf load-postgres"
  echo "[load]   docker volume rm nginx-training-clickstack_checkpoints_postgres"
  exit 1
fi

if [ "${SHIFT_NS:-0}" = "0" ]; then
  echo "[load] NOTE: SHIFT_NS is unset, so each record is shifted to Now() as it is read."
  echo "[load]       That drifts from the Elastic stack. For a comparable load:"
  echo "[load]         export SHIFT_NS=\$(SERVICE=postgres ../../ingest/clickstack/shift-ns.sh --align-hour)"
fi

echo "[load] waiting for the OTLP receiver to accept connections"
ready=0
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -X POST http://localhost:4318/v1/logs \
         -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
  case "$code" in 200|401) ready=1; break ;; esac
  sleep 2
done
if [ "$ready" != "1" ]; then
  echo "[load] OTLP never came up. Check setup:  docker compose logs setup"
  exit 1
fi
echo "[load] OTLP is ready"

# Same checkpoint trap as load.sh: with postgres rows deleted but the offsets still on disk,
# the receivers resume at EOF and ship nothing. We only reach here with zero postgres rows.
docker compose rm -sf load-postgres >/dev/null 2>&1
if docker volume rm nginx-training-clickstack_checkpoints_postgres >/dev/null 2>&1; then
  echo "[load] cleared stale filelog checkpoints"
fi

echo "[load] starting the collector"
docker compose up -d load-postgres >/dev/null 2>&1 \
  || { echo "[load] could not start the load-postgres service"; exit 1; }

echo "[load] waiting for ingest to settle (expecting ${EXPECTED} postgres rows)"
last=-1; stable=0
for i in $(seq 1 120); do
  sleep 3
  now=$(count_postgres); now=${now:-0}
  printf "\r[load]   %s postgres rows" "$now"
  if [ "$now" = "$last" ] && [ "$now" -gt 0 ]; then
    stable=$((stable+1))
    [ "$stable" -ge 4 ] && break
  else
    stable=0
  fi
  last=$now
done
echo

echo "[load] stopping the collector"
docker compose stop load-postgres >/dev/null 2>&1

final=$(count_postgres); final=${final:-0}
if [ "$final" = "$EXPECTED" ]; then
  echo "[load] done: ${final} postgres rows, exactly as expected"
  echo "[load] now run ./verify-postgres.sh"
else
  echo "[load] WARNING: ${final} postgres rows, expected ${EXPECTED}"
  echo "[load] A mismatch here is usually a duplicated batch, not lost data: OTLP delivery"
  echo "[load] is at-least-once and otel_logs has no dedup, so an ambiguous timeout rewrites"
  echo "[load] a whole batch. Delete the postgres rows and reload (see the guard message above)."
  docker compose logs load-postgres 2>&1 | grep -iE '\berror\b' | tail -5 | sed 's/^/    /'
  exit 1
fi
