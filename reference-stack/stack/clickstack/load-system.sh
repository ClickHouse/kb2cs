#!/usr/bin/env bash
# Ship the SYSTEM syslog and auth log into ClickStack as a fifth service.
#
#   export SHIFT_NS=$(SERVICE=system ../../ingest/clickstack/shift-ns.sh --align-hour)
#   ./load-system.sh
#
# Like the other additive loaders, the guard counts SYSTEM rows rather than refusing any
# non-empty otel_logs.
#
# Both files are ordinary one-line-per-record syslog, so unlike mysql there is no multiline
# config -- but syslog carries NO YEAR, and the collector's timestamp parser defaults to the
# current one. See the header of ingest/clickstack/otel-collector-system.yaml.

set -uo pipefail

EXPECTED=56224          # 40,000 syslog + 16,224 auth lines
CH="docker compose exec -T clickstack clickhouse-client --query"

count_system() {
  $CH "SELECT count() FROM default.otel_logs WHERE ServiceName = 'system'" 2>/dev/null \
    | tr -d '[:space:]'
}
count_all() { $CH "SELECT count() FROM default.otel_logs" 2>/dev/null | tr -d '[:space:]'; }

before=$(count_system); before=${before:-0}
total=$(count_all); total=${total:-0}
echo "[load] otel_logs holds ${total} rows, of which ${before} are system"
if [ "$before" -gt 0 ]; then
  echo "[load] system is already loaded -- refusing to append and double every system number."
  echo "[load] to reload just system, leaving nginx untouched:"
  echo "[load]   docker compose exec clickstack clickhouse-client --query \\"
  echo "[load]     \"ALTER TABLE default.otel_logs DELETE WHERE ServiceName = 'system'\""
  echo "[load]   docker compose rm -sf load-system"
  echo "[load]   docker volume rm nginx-training-clickstack_checkpoints_system"
  exit 1
fi

if [ "${SHIFT_NS:-0}" = "0" ]; then
  echo "[load] NOTE: SHIFT_NS is unset, so each record is shifted to Now() as it is read."
  echo "[load]       That drifts from the Elastic stack. For a comparable load:"
  echo "[load]         export SHIFT_NS=\$(SERVICE=system ../../ingest/clickstack/shift-ns.sh --align-hour)"
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

# Same checkpoint trap as load.sh: with system rows deleted but the offsets still on disk,
# the receivers resume at EOF and ship nothing. We only reach here with zero system rows.
docker compose rm -sf load-system >/dev/null 2>&1
if docker volume rm nginx-training-clickstack_checkpoints_system >/dev/null 2>&1; then
  echo "[load] cleared stale filelog checkpoints"
fi

echo "[load] starting the collector"
docker compose up -d load-system >/dev/null 2>&1 \
  || { echo "[load] could not start the load-system service"; exit 1; }

echo "[load] waiting for ingest to settle (expecting ${EXPECTED} system rows)"
last=-1; stable=0
for i in $(seq 1 120); do
  sleep 3
  now=$(count_system); now=${now:-0}
  printf "\r[load]   %s system rows" "$now"
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
docker compose stop load-system >/dev/null 2>&1

final=$(count_system); final=${final:-0}
if [ "$final" = "$EXPECTED" ]; then
  echo "[load] done: ${final} system rows, exactly as expected"
  echo "[load] now run ./verify-system.sh"
else
  echo "[load] WARNING: ${final} system rows, expected ${EXPECTED}"
  echo "[load] A mismatch here is usually a duplicated batch, not lost data: OTLP delivery"
  echo "[load] is at-least-once and otel_logs has no dedup, so an ambiguous timeout rewrites"
  echo "[load] a whole batch. Delete the system rows and reload (see the guard message above)."
  docker compose logs load-system 2>&1 | grep -iE '\berror\b' | tail -5 | sed 's/^/    /'
  exit 1
fi
