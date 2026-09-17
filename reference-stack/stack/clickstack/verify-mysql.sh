#!/usr/bin/env bash
# Verify the MySQL migration -- all three dashboards, logs and metrics.
#
#   ./verify-mysql.sh
#
# The eighth verifier here, and the first covering a MULTI-LINE log format: the slow log's
# records are five lines each, so the row count is an assertion about the record reader and
# the collector's `multiline` block, not just about delivery. A line-oriented
# misconfiguration produces 12,565 rows instead of 2,513 -- five times too many, with no
# error anywhere.
#
# Every expectation was measured against the Elastic stack on 2026-09-16 over the absolute
# window 2026-09-15T15:00Z .. 2026-09-16T14:00Z. Counts that depend on the window say so.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
D_LOGS="[Logs MySQL] Overview"
D_DB="[Metrics MySQL] Database Overview"
D_REPLICA="[Metrics MySQL] Replica Status"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; fail=$((fail+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ch() { docker compose exec -T clickstack clickhouse-client --query "$1" </dev/null 2>/dev/null | tr -d '\r'; }
expect() {
  local got
  got=$(ch "$3" | tr '\n\t' '  ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/  */ /g')
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "got '$got', expected '$2'"; fi
}

MY="ServiceName = 'mysql'"
SLOW="$MY AND LogAttributes['log.stream'] = 'mysql_slowlog'"
ERRL="$MY AND LogAttributes['log.stream'] = 'mysql_error'"
GAUGE="default.otel_metrics_gauge"
SUM="default.otel_metrics_sum"

# ---------------------------------------------------------------- ingest: the multi-line bit
head_ "Slow log: multi-line records"

expect "slow log is 2,513 RECORDS, not 12,565 lines" "2513" \
  "SELECT count() FROM default.otel_logs WHERE $SLOW"
expect "error log is 267 lines" "267" \
  "SELECT count() FROM default.otel_logs WHERE $ERRL"
expect "every record parsed a query" "2513" \
  "SELECT countIf(LogAttributes['query'] != '') FROM default.otel_logs WHERE $SLOW"
expect "every record parsed user, duration, rows_examined, source ip" "2513 2513 2513 2513" \
  "SELECT countIf(LogAttributes['user'] != ''),
          countIf(LogAttributes['duration_ns'] != ''),
          countIf(LogAttributes['rows_examined'] != ''),
          countIf(LogAttributes['source_ip'] != '')
   FROM default.otel_logs WHERE $SLOW"
# The failure mode of a line-oriented reader: header lines arriving as their own documents,
# or leaking into the query text of a document that did parse.
expect "no header line leaked into a query" "0" \
  "SELECT countIf(position(LogAttributes['query'], '# Time:') > 0
                OR position(LogAttributes['query'], 'User@Host') > 0
                OR position(LogAttributes['query'], 'SET timestamp') > 0)
   FROM default.otel_logs WHERE $SLOW"
expect "every slow query really is slower than long_query_time (1s)" "2513" \
  "SELECT countIf(toFloat64(LogAttributes['query_time']) > 1.0)
   FROM default.otel_logs WHERE $SLOW"
expect "4 distinct queries, 2 users" "4 2" \
  "SELECT uniqExact(LogAttributes['query']), uniqExact(LogAttributes['user'])
   FROM default.otel_logs WHERE $SLOW"

head_ "Error log parsing"

# mysqld's own level vocabulary must survive in LogAttributes['level']; SeverityText holds
# the mapped OTel names, which merge Note and System into 'info'.
expect "raw levels preserved (Note/Warning/ERROR/System)" "131 109 19 8" \
  "SELECT countIf(LogAttributes['level'] = 'Note'),
          countIf(LogAttributes['level'] = 'Warning'),
          countIf(LogAttributes['level'] = 'ERROR'),
          countIf(LogAttributes['level'] = 'System')
   FROM default.otel_logs WHERE $ERRL"
expect "severity mapped, System counted as info" "139 109 19" \
  "SELECT countIf(SeverityText = 'info'), countIf(SeverityText = 'warn'),
          countIf(SeverityText = 'error')
   FROM default.otel_logs WHERE $ERRL"
expect "6 distinct MY- error codes" "6" \
  "SELECT uniqExact(LogAttributes['code']) FROM default.otel_logs WHERE $ERRL"

# ---------------------------------------------------------------- metrics: shape
head_ "Metrics: series identity and Sum/Gauge split"

expect "115,200 metric points (63,360 sum + 51,840 gauge)" "63360 51840" \
  "SELECT (SELECT count() FROM $SUM WHERE MetricName LIKE 'mysql.%'),
          (SELECT count() FROM $GAUGE WHERE MetricName LIKE 'mysql.%')"

# ONE ATTRIBUTE THAT MOVES WITH THE VALUE DESTROYS SERIES IDENTITY. `source.file_info` is
# "<binlog file> <byte position>", so putting it on each data point gave 2,879 series for one
# replica; nothing errors, the tiles just read an arbitrary point per bucket.
expect "each replica metric is ONE series, not 2,879" "1 1 1 1" \
  "SELECT uniqExact(Attributes) FROM $GAUGE WHERE MetricName LIKE 'mysql.replica.%'
   GROUP BY MetricName ORDER BY MetricName"

# The five fields Elastic types 'counter' that must be GAUGES, because their panels read the
# absolute value (max() / last_value()) rather than a difference.
expect "counter-typed-but-gauge fields are in the gauge table" "2880 2880 2880 2880 2880" \
  "SELECT count() FROM $GAUGE
   WHERE MetricName IN ('mysql.max_used_connections','mysql.buffer_pool.reads',
                        'mysql.buffer_pool.read_requests',
                        'mysql.replica.log_position.read','mysql.replica.log_position.exec')
   GROUP BY MetricName ORDER BY MetricName"
expect "cache.ssl.size ships as a SUM (panel differences it)" "2880" \
  "SELECT count() FROM $SUM
   WHERE MetricName = 'mysql.ssl_cache' AND Attributes['status'] = 'size'"
# One row with named columns. UNION ALL was returning the six counts in an arbitrary order,
# which failed even though every count was right.
expect "the collapsed families carry their discriminating attribute" "4 6 2 2 3 3" \
  "SELECT uniqExactIf(Attributes['command'],   MetricName = 'mysql.commands'),
          uniqExactIf(Attributes['error'],     MetricName = 'mysql.connection.errors'),
          uniqExactIf(Attributes['kind'],      MetricName = 'mysql.aborted'),
          uniqExactIf(Attributes['direction'], MetricName = 'mysql.traffic'),
          uniqExactIf(Attributes['status'],    MetricName = 'mysql.table_open_cache'),
          uniqExactIf(Attributes['status'],    MetricName = 'mysql.ssl_cache')
   FROM $SUM WHERE MetricName LIKE 'mysql.%'"

# ---------------------------------------------------------------- metrics: values
head_ "Metrics: the invariants both stacks must reproduce"

expect "questions == sum(command.*) == 120,000" "120000 120000" \
  "SELECT (SELECT toUInt64(max(Value)) FROM $SUM WHERE MetricName = 'mysql.questions'),
          (SELECT toUInt64(sum(m)) FROM (
             SELECT max(Value) AS m FROM $SUM WHERE MetricName = 'mysql.commands'
             GROUP BY Attributes['command']))"
expect "max_used_connections high-water mark is 79" "79" \
  "SELECT toUInt64(max(Value)) FROM $GAUGE WHERE MetricName='mysql.max_used_connections'"
expect "buffer pool miss rate is 0.05% (99.95% hit rate)" "0.05" \
  "SELECT round(100.0 *
     (SELECT max(Value) FROM $GAUGE WHERE MetricName='mysql.buffer_pool.reads') /
     (SELECT max(Value) FROM $GAUGE WHERE MetricName='mysql.buffer_pool.read_requests'), 4)"
expect "the replica trails the source on 1,758 of 2,880 scrapes" "1758" \
  "SELECT countIf(rd > ex) FROM (
     SELECT TimeUnix, Value AS rd FROM $GAUGE WHERE MetricName='mysql.replica.log_position.read'
   ) AS r INNER JOIN (
     SELECT TimeUnix, Value AS ex FROM $GAUGE WHERE MetricName='mysql.replica.log_position.exec'
   ) AS e USING (TimeUnix)"
# A DELAYED replica: SOURCE_DELAY=30 between 02:00 and 05:00 (360 scrapes at 30 s). Without
# it `thread.sql.delay.sec` was a constant 0, which made the "SQL thread delay" panel
# unplottable -- both stacks agreed perfectly and both charts were empty. A step function is
# also a far better cross-stack comparison than a flat line: a mis-scoped tile shows instantly.
expect "SQL_Delay is 30s for the 360-scrape maintenance window" "30 360" \
  "SELECT toUInt64(max(Value)), countIf(Value = 30)
   FROM $GAUGE WHERE MetricName = 'mysql.replica.sql_delay'"
# A delayed replica reports Seconds_Behind_Source as the configured delay PLUS its real lag,
# so the ceiling is 30 + the 7 s worst case outside the window.
expect "replication lag tops out at 37 seconds (30 delay + 7 real)" "37" \
  "SELECT toUInt64(max(Value)) FROM $GAUGE WHERE MetricName='mysql.replica.time_behind_source'"

# ---------------------------------------------------------------- the migrated objects
head_ "Migrated dashboards"

# Retried: a single probe against a busy container gives a false "not answering", which
# looks like the stack is down when it is merely mid-merge. Seen twice while writing this.
up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf -o /dev/null --max-time 5 http://localhost:8080/api/health </dev/null; then
    up=1; break
  fi
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
printf '%s' "$API" > /tmp/hdx-mysql-objects.txt
q() { python3 verify-migration.py "$1" < /tmp/hdx-mysql-objects.txt; }

for spec in "$D_LOGS:7" "$D_DB:17" "$D_REPLICA:5"; do
  name="${spec%:*}"; want="${spec##*:}"
  got=$(q "tilecount:$name")
  if [ "$got" = "$want" ]; then ok "$name — $want tiles"
  else bad "$name: expected $want tiles, found '${got:-none}'"; fi
done

types=$(q "displaytypes:$D_LOGS")
if [ "$types" = "markdown,pie,pie,search,stacked_bar,stacked_bar,table" ]; then
  ok "$D_LOGS display types: $types"
else bad "$D_LOGS display types" "got '$types'"; fi

# Every XY panel on the source dashboard is `seriesType: bar_stacked` -- a TIME-SERIES
# stacked bar (date_histogram on x), which maps to ClickStack `stacked_bar`. Only `bar` is
# categorical. Four of these were built as `line` and corrected 2026-09-16; this locks the
# faithful mapping in. 15 XY panels -> 15 stacked_bar, plus the lnsMetric -> number and the
# provenance markdown.
types=$(q "displaytypes:$D_DB")
want_db="markdown,number$(printf ',stacked_bar%.0s' $(seq 1 15))"
if [ "$types" = "$want_db" ]; then
  ok "$D_DB display types: 15 stacked_bar + number + markdown"
else bad "$D_DB display types" "got '$types'"; fi

types=$(q "displaytypes:$D_REPLICA")
if [ "$types" = "line,line,markdown,stacked_bar,table" ]; then
  ok "$D_REPLICA display types: $types"
else bad "$D_REPLICA display types" "got '$types'"; fi

# The gauge panels became SQL tiles because HyperDX collapses a gauge to one sample per
# bucket before aggFn runs -- so avg()/max() over a gauge's samples is not expressible in a
# builder tile. If someone "simplifies" these back to builder tiles, these checks fail.
for t in "Open Tables, Files, Streams" "Thread Activity" "Buffer Pool Pages" \
         "Connected Threads" "Buffer Pool Utilization" "Buffer Pool Efficiency"; do
  if [ -n "$(q "sqltemplate:$D_DB:$t")" ]; then ok "$t is a SQL tile"
  else bad "$t should be a SQL tile" "a builder gauge tile cannot aggregate within a bucket"; fi
done

# and the per-second rate tiles must divide by the bucket length
for t in "Statements Executed" "Rate of SELECT statements" "Network Traffic"; do
  if q "sqltemplate:$D_DB:$t" | grep -q 'interval_s'; then ok "$t divides by \$__interval_s"
  else bad "$t must normalise to a per-second rate" "Kibana uses normalize_by_unit(.., 's')"; fi
done

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m%d checks passed.\033[0m The mysql migration reproduced cleanly.\n' "$pass"
  echo "Dashboards: http://localhost:8080/dashboards"
  exit 0
else
  printf '\033[31m%d passed, %d FAILED.\033[0m\n' "$pass" "$fail"
  exit 1
fi
