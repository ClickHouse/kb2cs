#!/usr/bin/env bash
# Ship the MYSQL slow log and error log into ClickStack as a fourth service.
#
#   export SHIFT_NS=$(SERVICE=mysql ../../ingest/clickstack/shift-ns.sh --align-hour)
#   ./load-mysql.sh
#
# Like load-postgres.sh, the guard counts MYSQL rows rather than refusing any non-empty
# otel_logs: this is the fourth service in a shared table, so the question is "has mysql
# already been loaded", not "is the table empty".
#
# The settle loop matters more here than for the other three. The slow log is recombined
# from multi-line records by `multiline.line_start_pattern`, so the row count is NOT the
# file's line count -- 2,513 records out of 12,565 lines. A line-oriented misconfiguration
# therefore shows up as a row count five times too high, not as an error, which is exactly
# why EXPECTED is asserted instead of just reported.
set -uo pipefail

EXPECTED=2780            # 2,513 slow-query RECORDS (from 12,565 lines) + 267 error lines
CH="docker compose exec -T clickstack clickhouse-client --query"

count_mysql() {
  $CH "SELECT count() FROM default.otel_logs WHERE ServiceName = 'mysql'" 2>/dev/null \
    | tr -d '[:space:]'
}
count_all() { $CH "SELECT count() FROM default.otel_logs" 2>/dev/null | tr -d '[:space:]'; }

before=$(count_mysql); before=${before:-0}
total=$(count_all); total=${total:-0}
echo "[load] otel_logs holds ${total} rows, of which ${before} are mysql"
if [ "$before" -gt 0 ]; then
  echo "[load] mysql is already loaded -- refusing to append and double every mysql number."
  echo "[load] to reload just mysql, leaving nginx untouched:"
  echo "[load]   docker compose exec clickstack clickhouse-client --query \\"
  echo "[load]     \"ALTER TABLE default.otel_logs DELETE WHERE ServiceName = 'mysql'\""
  echo "[load]   docker compose rm -sf load-mysql"
  echo "[load]   docker volume rm nginx-training-clickstack_checkpoints_mysql"
  exit 1
fi

if [ "${SHIFT_NS:-0}" = "0" ]; then
  echo "[load] NOTE: SHIFT_NS is unset, so each record is shifted to Now() as it is read."
  echo "[load]       That drifts from the Elastic stack. For a comparable load:"
  echo "[load]         export SHIFT_NS=\$(SERVICE=mysql ../../ingest/clickstack/shift-ns.sh --align-hour)"
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

# Same checkpoint trap as load.sh: with mysql rows deleted but the offsets still on disk,
# the receivers resume at EOF and ship nothing. We only reach here with zero mysql rows.
docker compose rm -sf load-mysql >/dev/null 2>&1
if docker volume rm nginx-training-clickstack_checkpoints_mysql >/dev/null 2>&1; then
  echo "[load] cleared stale filelog checkpoints"
fi

echo "[load] starting the collector"
docker compose up -d load-mysql >/dev/null 2>&1 \
  || { echo "[load] could not start the load-mysql service"; exit 1; }

echo "[load] waiting for ingest to settle (expecting ${EXPECTED} mysql rows)"
last=-1; stable=0
for i in $(seq 1 120); do
  sleep 3
  now=$(count_mysql); now=${now:-0}
  printf "\r[load]   %s mysql rows" "$now"
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
docker compose stop load-mysql >/dev/null 2>&1

final=$(count_mysql); final=${final:-0}
if [ "$final" = "$EXPECTED" ]; then
  echo "[load] done: ${final} mysql rows, exactly as expected"
  echo "[load] now run ./verify-mysql.sh"
else
  echo "[load] WARNING: ${final} mysql rows, expected ${EXPECTED}"
  echo "[load] A mismatch here is usually a duplicated batch, not lost data: OTLP delivery"
  echo "[load] is at-least-once and otel_logs has no dedup, so an ambiguous timeout rewrites"
  echo "[load] a whole batch. Delete the mysql rows and reload (see the guard message above)."
  docker compose logs load-mysql 2>&1 | grep -iE '\berror\b' | tail -5 | sed 's/^/    /'
  exit 1
fi
