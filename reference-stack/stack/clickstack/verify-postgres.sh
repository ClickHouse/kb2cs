#!/usr/bin/env bash
# Verify the PostgreSQL migration -- all three dashboards, logs and metrics.
#
#   ./verify-postgres.sh
#
# The fifth verifier here. Postgres is the first integration migrated in full where
# **nothing was blocked**: no map, no unknown panel type, no missing target metric.
#
# Every expectation was measured against the Elastic stack on 2026-09-16.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
D_OVERVIEW="[Logs PostgreSQL] Overview (migrated)"
D_DURATION="[Logs PostgreSQL] Query Duration Overview (migrated)"
D_METRICS="[Metrics PostgreSQL] Database Overview (migrated)"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; fail=$((fail+1)); }
note() { printf '  ....  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ch() { docker compose exec -T clickstack clickhouse-client --query "$1" </dev/null 2>/dev/null | tr -d '\r'; }
expect() {
  local got
  got=$(ch "$3" | tr '\n' ' ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "got '$got', expected '$2'"; fi
}

# ---------------------------------------------------------------- the comparison window
# DERIVED FROM THE DATA, aligned to whole hours. Hardcoding it meant every stored-SQL check
# in this file broke the moment the corpus was re-anchored -- which RUNBOOK.md documents as a
# routine operation. Whole hours because a window that ends mid-bucket makes the last bucket
# partial, which reads as a value bug and is not one.
#
# ONE window for every check in this file. The hardcoded version used two different ends --
# 14:00:00 for one check and 14:01:00 for two others -- which is a baseline picked to fit
# rather than a window picked on merit. The expectations below are re-measured against this
# single window instead.
# IFS on the separator, not whitespace: the timestamps contain a space, so a plain
# `read -r A B` puts only the DATE in A and everything else in B.
IFS='|' read -r WIN_FROM WIN_TO <<<"$(ch "SELECT concat(toString(toStartOfHour(min(TimeUnix))), '|',
                                                toString(toStartOfHour(max(TimeUnix))))
                                  FROM default.otel_metrics_sum WHERE MetricName LIKE 'postgresql.%'")"
[ -n "${WIN_FROM:-}" ] || { echo "  could not derive the comparison window from default.otel_metrics_sum"; exit 1; }

PG="LogAttributes['log.stream'] = 'postgres_log'"
SUM="default.otel_metrics_sum"

# ---------------------------------------------------------------- the migrated objects
head_ "Migrated objects"

# Retried: a single probe against a busy container returns a false "not answering", which
# reads as "the stack is down" when it is merely mid-merge. Seen twice on 2026-09-16.
up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -sf -o /dev/null --max-time 5 http://localhost:8080/api/health </dev/null && { up=1; break; }
  sleep 2
done
if [ "$up" != "1" ]; then
  echo "  ClickStack is not answering on :8080. Start it:  docker compose up -d"; exit 1
fi
API=$(docker compose exec -T clickstack sh -c "
  rm -f /tmp/ck
  curl -s -c /tmp/ck -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d '{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}' http://localhost:8000/login/password
  echo '<<<DASHBOARDS>>>'; curl -s -b /tmp/ck http://localhost:8000/dashboards
  echo '<<<SEARCHES>>>';   curl -s -b /tmp/ck http://localhost:8000/saved-search
" </dev/null 2>/dev/null)
[ -n "$API" ] || { echo "  could not log in to the HyperDX API as $EMAIL"; exit 1; }
printf '%s' "$API" > /tmp/hdx-postgres-objects.txt
q() { python3 hdx-objects.py "$1" < /tmp/hdx-postgres-objects.txt; }

for spec in "$D_OVERVIEW:4" "$D_DURATION:4" "$D_METRICS:10"; do
  name="${spec%:*}"; want="${spec##*:}"
  got=$(q "tilecount:$name")
  if [ "$got" = "$want" ]; then ok "$name — $want tiles"
  else bad "$name: expected $want tiles, found '${got:-none}'"; fi
done

types=$(q "displaytypes:$D_OVERVIEW")
if [ "$types" = "markdown,search,stacked_bar,table" ]; then
  ok "Overview tiles are all builder types: $types"
else bad "Overview tile types are '$types'" "expected markdown,search,stacked_bar,table"; fi

# ---------------------------------------------------------------- logs vs Elastic
head_ "Query log vs Elasticsearch"

expect "postgres rows = 120,421"          "120421" \
  "SELECT count() FROM default.otel_logs WHERE $PG"
expect "log levels — all 4 exact"  "ERROR=83 FATAL=19 LOG=120288 WARNING=31" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(l,'=',toString(c)))),' ') FROM
     (SELECT LogAttributes['level'] l, count() c FROM default.otel_logs WHERE $PG GROUP BY l)"
expect "statements with a duration = 120,000" "120000" \
  "SELECT count() FROM default.otel_logs WHERE $PG AND LogAttributes['duration_ms'] != ''"
expect "summed duration = 3,213,228.326 ms" "3213228.326" \
  "SELECT round(sum(toFloat64(LogAttributes['duration_ms'])),3) FROM default.otel_logs
   WHERE $PG AND LogAttributes['duration_ms'] != ''"
note "Elastic reports 3,213,228.325: its pipeline stores event.duration in ns via a float,"
note "so 3184.552 ms becomes 3184551936 ns. The ms value matches the log line exactly."
expect "slower than 30 ms = 2,708 (the Slow Queries threshold)" "2708" \
  "SELECT count() FROM default.otel_logs WHERE $PG
   AND toFloat64OrZero(LogAttributes['duration_ms']) > 30"
expect "3 databases, 4 users"  "3 4" \
  "SELECT concat(toString(uniqExactIf(LogAttributes['database'], LogAttributes['database'] != '')),
                 ' ', toString(uniqExactIf(LogAttributes['user'], LogAttributes['user'] != '')))
   FROM default.otel_logs WHERE $PG"
expect "288 checkpoint lines carry no user or database" "288" \
  "SELECT count() FROM default.otel_logs WHERE $PG AND LogAttributes['database'] = ''"
expect "nginx and apache untouched" "1011404 264662" \
  "SELECT concat(
     toString(countIf(ServiceName='nginx')),' ',toString(countIf(ServiceName='apache')))
   FROM default.otel_logs"

# ---------------------------------------------------------------- metrics vs Elastic
head_ "pg_stat_* series vs Elasticsearch"

expect "metric points = 127,226"  "127226" \
  "SELECT count() FROM $SUM WHERE MetricName LIKE 'postgresql.%'"
# The three invariants tying the metrics back to the log they were derived from.
expect "sum(final query.calls) = 120,000, the logged statement count" "120000" \
  "SELECT toInt64(sum(v)) FROM
     (SELECT Attributes['database'] d, Attributes['query_text'] q, max(Value) v FROM $SUM
      WHERE MetricName='postgresql.statement.query.calls' GROUP BY d,q)"
expect "sum(final query.time.total.ms) = 3,213,228.296" "3213228.296" \
  "SELECT round(sum(v),3) FROM
     (SELECT Attributes['database'] d, Attributes['query_text'] q, max(Value) v FROM $SUM
      WHERE MetricName='postgresql.statement.query.time.total.ms' GROUP BY d,q)"
expect "sum(final transactions.rollback) = 83, the ERROR line count" "83" \
  "SELECT toInt64(sum(v)) FROM
     (SELECT Attributes['database'] d, max(Value) v FROM $SUM
      WHERE MetricName='postgresql.database.transactions.rollback' GROUP BY d)"
expect "sum(final transactions.commit) = 119,917" "119917" \
  "SELECT toInt64(sum(v)) FROM
     (SELECT Attributes['database'] d, max(Value) v FROM $SUM
      WHERE MetricName='postgresql.database.transactions.commit' GROUP BY d)"
expect "16 query series across 3 databases" "16 3" \
  "SELECT concat(toString(uniqExact(Attributes['query_text'])),' ',
                 toString((SELECT uniqExact(Attributes['database']) FROM $SUM
                           WHERE MetricName='postgresql.database.transactions.commit')))
   FROM $SUM WHERE MetricName='postgresql.statement.query.calls'"

# ---------------------------------------------------------------- stored tile SQL
head_ "Stored tile SQL (run what the tile saves, not an equivalent)"

# Rows Fetched/Returned: the rate template, diffed against Kibana bucket-for-bucket at 30m.
RSQL=$(q "sqltemplate:$D_METRICS:Rows Fetched")
if [ -z "$RSQL" ]; then
  bad "could not read the Rows Fetched tile's sqlTemplate"
else
  REXP=$(printf '%s' "$RSQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g" \
    | sed 's/\$__timeInterval(TimeUnix)/toStartOfInterval(TimeUnix, INTERVAL 1800 second)/g' \
    | sed 's/\$__interval_s/1800/g')
  got=$(ch "SELECT concat(toString(count()),' ',toString(round(sum(v),3))) FROM
              (SELECT \"rows.fetched\" AS v FROM ($REXP))" | tr -d '[:space:]')
  if [ "$got" = "476656.771" ]; then
    ok "Rows Fetched tile's own SQL: 47 buckets, total 6656.771 -- Kibana's 47 shared buckets match exactly"
  else
    bad "Rows Fetched tile's own SQL gives '$got', expected '476656.771'"
  fi
fi
note "Elastic shows one extra bucket at the window edge (its last scrape is at 14:00:01)."

# Top Queries: the exclusion filter is the point -- 16 distinct queries, 15 after exclusion.
TSQL=$(q "sqltemplate:$D_METRICS:Top Queries")
if [ -z "$TSQL" ]; then
  bad "could not read the Top Queries tile's sqlTemplate"
else
  TEXP=$(printf '%s' "$TSQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g")
  got=$(ch "SELECT toString(count()) FROM ($TEXP)" | tr -d '[:space:]')
  if [ "$got" = "15" ]; then
    ok "Top Queries tile's own SQL: 15 series -- the exclusion filter removed 1 of 16"
  else
    bad "Top Queries tile's own SQL gives '$got' series, expected 15"
  fi
fi
note "Kibana's exclusion list is exact and case-sensitive on a keyword field: it removes"
note "'SELECT * FROM pg_stat_statements' but NOT this corpus's 'BEGIN'/'COMMIT', because the"
note "list spells them 'BEGIN;' and 'commit'. Faithful to the source, not a translation slip."

# The Conflict/Deadlock panel is the one that caught a real bug, so it gets its own check.
#
# Two things were wrong and both were invisible in aggregate: `deadlocks` uses average() in
# the source formula while `conflicts` uses max() (the panel mixes them), and the tile used
# `prev != 0` to mean "this bucket has no predecessor". These counters sit at ZERO all day,
# so that sentinel dropped precisely the bucket where the counter first moved -- the spike.
# Kibana showed one spike, ClickStack none. The guard is `row_number() > 1` now.
CSQL=$(q "sqltemplate:$D_METRICS:Conflict")
if [ -z "$CSQL" ]; then
  bad "could not read the Conflict/Deadlock tile's sqlTemplate"
else
  CEXP=$(printf '%s' "$CSQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g" \
    | sed 's/\$__timeInterval(TimeUnix)/toStartOfInterval(TimeUnix, INTERVAL 1800 second)/g' \
    | sed 's/\$__interval_s/1800/g')
  got=$(ch "SELECT concat(toString(count()),' ',
                          toString(countIf(\"deadlocks\" > 0)),' ',
                          toString(countIf(\"conflicts\" > 0)))
              FROM ($CEXP)" | tr -d '[:space:]')
  if [ "$got" = "4721" ]; then
    ok "Conflict/Deadlock tile's own SQL: 48 buckets, 2 deadlock spikes, 1 conflict spike"
  else
    bad "Conflict/Deadlock tile's own SQL gives '$got', expected '4821'" \
        "47 buckets / 2 deadlock spikes / 1 conflict spike; a prev!=0 guard yields 31/0/0"
  fi
fi
note "deadlocks uses average() in the source formula, conflicts uses max(). The panel mixes"
note "them, and using max() for both hides the deadlock spikes entirely."

# ---------------------------------------------------------------- summary
echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m%d checks passed.\033[0m The postgresql migration reproduced cleanly.\n' "$pass"
  echo "Dashboards: http://localhost:8080/dashboards"
  exit 0
else
  printf '\033[31m%d passed, %d FAILED.\033[0m\n' "$pass" "$fail"
  exit 1
fi
