#!/usr/bin/env bash
# Verify the nginx METRICS migration: the series landed, and the migrated tiles' expressions
# return what Elasticsearch returns.
#
#   ./verify-metrics.sh
#
# The third verifier in this repo, after verify-migration.sh (nginx logs) and
# verify-apache.sh. It is the first one covering OTel *metrics*, and the shape of the checks
# is different because the target's metric semantics are different — see the two
# "deliberate deviation" checks at the end, which assert that ClickStack does NOT match
# Kibana, for reasons that are properties of the platforms rather than faults.
#
# Every expectation was measured against metrics-nginx.stubstatus-default on 2026-09-16.
set -uo pipefail
# Run from this script's own directory. `docker compose exec` resolves the compose file
# from the working directory, so without this the script only works when invoked from
# here -- and reports "ClickHouse not reachable", which reads as a broken stack.
cd "$(dirname "$0")"
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
DASH="[Metrics Nginx] Overview (migrated)"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; fail=$((fail+1)); }
note() { printf '  ....  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ch() { docker compose exec -T clickstack clickhouse-client --query "$1" </dev/null 2>/dev/null | tr -d '\r'; }
expect() { # expect <label> <expected> <query>
  local got
  got=$(ch "$3" | tr '\n' ' ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "got '$got', expected '$2'"; fi
}

# ---------------------------------------------------------------- the comparison window
# DERIVED FROM THE DATA, aligned to whole hours. Hardcoding it meant every stored-SQL check
# in this file broke the moment the corpus was re-anchored -- which RUNBOOK.md documents as a
# routine operation. Whole hours because a window that ends mid-bucket makes the last bucket
# partial, which reads as a value bug and is not one.
# IFS on the separator, not whitespace: the timestamps contain a space, so a plain
# `read -r A B` puts only the DATE in A and everything else in B.
IFS='|' read -r WIN_FROM WIN_TO <<<"$(ch "SELECT concat(toString(toStartOfHour(min(TimeUnix))), '|',
                                                toString(toStartOfHour(max(TimeUnix))))
                                  FROM default.otel_metrics_sum WHERE MetricName LIKE 'nginx.%'")"
[ -n "${WIN_FROM:-}" ] || { echo "  could not derive the comparison window from default.otel_metrics_sum"; exit 1; }

SUM="default.otel_metrics_sum"
GAU="default.otel_metrics_gauge"

# ---------------------------------------------------------------- the migrated object
head_ "Migrated object"

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
printf '%s' "$API" > /tmp/hdx-metrics-objects.txt
q() { python3 verify-migration.py "$1" < /tmp/hdx-metrics-objects.txt; }

tiles=$(q "tilecount:$DASH")
if [ "$tiles" = "9" ]; then ok "$DASH — 9 tiles (8 panels + provenance note)"
else bad "expected 9 tiles, found '${tiles:-none}'" "re-run the migration"; fi

tags=$(q "tags:$DASH")
if [ "$tags" = "metrics,migrated-from-kibana,nginx" ]; then ok "tagged nginx + metrics + migrated-from-kibana"
else bad "tags are '$tags'" "expected 'metrics,migrated-from-kibana,nginx'"; fi

# ---------------------------------------------------------------- data landed
head_ "Series landed (8,640 scrapes x 3 sum metrics + 4 gauge states)"

expect "sum points = 25,920"    "25920" "SELECT count() FROM $SUM WHERE MetricName LIKE 'nginx.%'"
expect "gauge points = 34,560"  "34560" "SELECT count() FROM $GAU WHERE MetricName LIKE 'nginx.%'"
expect "3 hosts, as resource attributes" "3" \
  "SELECT uniqExact(ResourceAttributes['host.name']) FROM $SUM WHERE MetricName='nginx.requests'"
# A counter must arrive as CUMULATIVE + monotonic or ClickStack cannot de-cumulate it.
expect "counters are cumulative (temporality=2) and monotonic" "2 true" \
  "SELECT concat(toString(any(AggregationTemporality)),' ',toString(any(IsMonotonic)))
   FROM $SUM WHERE MetricName='nginx.requests'"
expect "the four connection states are one metric" "active reading waiting writing" \
  "SELECT arrayStringConcat(arraySort(groupArray(s)),' ') FROM
     (SELECT DISTINCT Attributes['state'] AS s FROM $GAU WHERE MetricName='nginx.connections_current')"

# ---------------------------------------------------------------- vs Elastic
head_ "Tile expressions vs Elasticsearch"

# The two SQL tiles chart the raw cumulative counter, which is the one thing a Sum builder
# tile cannot do. These are the numbers Kibana's max() panels show.
expect "Total requests — raw counter max = 12,165,071"  "12165071" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='nginx.requests'"
expect "Processed requests — raw counter max = 12,022,022" "12022022" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='nginx.connections_handled'"
expect "accepts max = 12,022,023"  "12022023" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='nginx.connections_accepted'"

# Gauges: these match Elastic to four decimal places, which is the strongest evidence the
# four-fields-to-one-metric reshape did not lose anything.
expect "avg active  = 6.3696"  "6.3696" \
  "SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='nginx.connections_current' AND Attributes['state']='active'"
expect "avg waiting = 6.3177"  "6.3177" \
  "SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='nginx.connections_current' AND Attributes['state']='waiting'"
expect "avg writing = 0.0428"  "0.0428" \
  "SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='nginx.connections_current' AND Attributes['state']='writing'"
expect "avg reading = 0.009"   "0.009" \
  "SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='nginx.connections_current' AND Attributes['state']='reading'"
note "reading is ~0 across this corpus on BOTH platforms — a property of the generated data."

# Per-host increases: what each node really served, and the link back to the access log.
expect "per-host request increase = 166,103 / 168,762 / 165,050" "166103 168762 165050" \
  "SELECT arrayStringConcat(groupArray(toString(d)),' ') FROM
     (SELECT ResourceAttributes['host.name'] AS h, toInt64(max(Value)-min(Value)) AS d
      FROM $SUM WHERE MetricName='nginx.requests' GROUP BY h ORDER BY h)"
expect "fleet increase = 499,915"  "499915" \
  "SELECT toInt64(sum(d)) FROM
     (SELECT ResourceAttributes['host.name'] AS h, max(Value)-min(Value) AS d
      FROM $SUM WHERE MetricName='nginx.requests' GROUP BY h)"
note "499,915 not 499,964: the first scrape already includes its own interval, so max-min"
note "drops the first bucket. differences()/increase() behave identically, on both platforms."

# Elastic's max(nginx.stubstatus.dropped) is the largest PER-HOST dropped counter, so the
# comparison has to group by host too -- table-wide maxima of accepts and handled come from
# different hosts and their difference means nothing.
# Run the Drops Rate tile's OWN stored SQL, macros expanded, rather than a hand-written
# equivalent. This check exists because the hand-written version passed while the stored
# query was wrong: it subtracted two independent cross-host maxima, so it only ever saw the
# highest-numbered host's drops -- 1 spike where Kibana showed 4. A check that does not
# execute what the tile stores cannot catch that class of bug.
SQL=$(q "sqltemplate:$DASH:Drops")
if [ -z "$SQL" ]; then
  bad "could not read the Drops Rate tile's sqlTemplate" "is the tile a sql tile?"
else
  # expand the macros the same way the tile server does, at 1-hour granularity
  EXPANDED=$(printf '%s' "$SQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g" \
    | sed 's/\$__timeInterval(TimeUnix)/toStartOfInterval(TimeUnix, INTERVAL 3600 second)/g')
  got=$(ch "SELECT concat(toString(count()),' ',toString(toInt64(sum(v)))) FROM
              (SELECT \"Drops Rate\" AS v FROM ($EXPANDED)) WHERE v > 0" \
        | tr -d '[:space:]')
  if [ "$got" = "44" ]; then
    ok "the Drops Rate tile's own SQL yields 4 spikes totalling 4, as Kibana does"
  else
    bad "the Drops Rate tile's own SQL yields '$got', expected '44' (4 spikes, total 4)" \
        "it probably subtracts cross-host maxima instead of differencing per host"
  fi
fi

# Same treatment for Request Rate: run the tile's own SQL and diff against the numbers
# Kibana draws. 10-minute buckets over the whole window; ES agrees on all 144 of them.
RSQL=$(q "sqltemplate:$DASH:Request Rate")
if [ -z "$RSQL" ]; then
  bad "could not read the Request Rate tile's sqlTemplate"
else
  REXP=$(printf '%s' "$RSQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g" \
    | sed 's/\$__timeInterval(TimeUnix)/toStartOfInterval(TimeUnix, INTERVAL 600 second)/g')
  got=$(ch "SELECT concat(toString(count()),' ',toString(toInt64(sum(v))),' ',toString(toInt64(max(v))))
              FROM (SELECT \"Request Rate\" AS v FROM ($REXP)) WHERE v IS NOT NULL" \
        | tr -d '[:space:]')
  if [ "$got" = "1441644982368" ]; then
    ok "Request Rate tile's own SQL: 144 buckets, total 164,498, peak 2,368 — Kibana exactly"
  else
    bad "Request Rate tile's own SQL gives '$got'" \
        "expected 144 buckets / total 164498 / peak 2368 (concatenated)"
  fi
fi

expect "max per-host dropped counter = 4" "4" \
  "SELECT toInt64(max(d)) FROM
     (SELECT ResourceAttributes['host.name'] AS h,
             maxIf(Value, MetricName='nginx.connections_accepted')
           - maxIf(Value, MetricName='nginx.connections_handled') AS d
      FROM $SUM WHERE MetricName IN ('nginx.connections_accepted','nginx.connections_handled')
      GROUP BY h)"

# ---------------------------------------------------------------- deliberate deviations
head_ "Platform semantics (measured, and the reason three tiles are SQL)"

# `increase` is NOT differences(of max(counter)) on multi-series data, and this is the
# measurement that proves it. Kibana collapses hosts with max() then differences: one node's
# rate, 165,050. `increase` differences each series then sums: 499,915 fleet-wide.
#
# The migrated rate tiles use SQL to reproduce Kibana's figure, because a migration should
# reproduce its source -- the first attempt shipped `increase` as a "better metric" and the
# difference was visible on the chart: a single-node burst that Kibana shows as a 1.8x spike
# is diluted to noise once three nodes are summed. If the fleet total is what you want, an
# `increase` builder tile gives it in one line; that is an improvement to offer, not a
# migration.
expect "Kibana's figure — highest-counter host alone = 165,050" "165050" \
  "SELECT toInt64(max(Value)-min(Value)) FROM $SUM
   WHERE MetricName='nginx.requests' AND ResourceAttributes['host.name']='web-edge-03'"
expect "what increase would have shown instead - fleet total" "499915" \
  "SELECT toInt64(sum(d)) FROM
     (SELECT ResourceAttributes['host.name'] AS h, max(Value)-min(Value) AS d
      FROM $SUM WHERE MetricName='nginx.requests' GROUP BY h)"

# And the finding that forced two SQL tiles.
expect "a Sum tile cannot show the raw counter: max of increase != max of Value" "2163 12002184" \
  "SELECT concat(
     toString(toInt64((SELECT max(d) FROM
       (SELECT ResourceAttributes['host.name'] AS h, max(Value)-min(Value) AS d
        FROM $SUM WHERE MetricName='nginx.requests'
          AND TimeUnix >= '$WIN_FROM' AND TimeUnix < '$WIN_FROM'::DateTime + INTERVAL 1 HOUR
        GROUP BY h)))), ' ',
     toString(toInt64((SELECT max(Value) FROM $SUM WHERE MetricName='nginx.requests'
        AND TimeUnix >= '$WIN_FROM' AND TimeUnix < '$WIN_FROM'::DateTime + INTERVAL 1 HOUR))))"
note "aggFn=max on this bucket returns 2,163 (biggest per-host increase), while the counter"
note "itself reads 12,002,184 — which is why Total/Processed requests are sql tiles."

# ---------------------------------------------------------------- summary
echo
# ---------------------------------------------------------------- gauge tiles must be SQL
head_ "Gauge panels are SQL tiles (regression guard added 2026-09-16)"

# Found during the mysql migration: on a metric source HyperDX collapses a gauge to ONE
# sample per bucket (the last) BEFORE aggFn runs, so `avg`/`max`/`min`/`sum`/`last_value` all
# return the same number and none of them is Kibana's average()/max() over the bucket's
# samples. These tiles were builder tiles until then and disagreed with Elastic in
# 141/144 and 23-141/144 buckets respectively -- while the whole-window averages checked above still matched, which is
# exactly why nothing caught it. If someone "simplifies" them back, these checks fail.
for t in "Active connections" "Reading / Writing / Waiting Rates"; do
  if [ -n "$(q "sqltemplate:$DASH:$t")" ]; then ok "$t is a SQL tile"
  else bad "$t must be a SQL tile" "a builder gauge tile cannot aggregate within a bucket"; fi
done

if [ "$fail" -eq 0 ]; then
  printf '\033[32m%d checks passed.\033[0m The metrics migration reproduced cleanly.\n' "$pass"
  echo "Dashboard: http://localhost:8080/dashboards"
  exit 0
else
  printf '\033[31m%d passed, %d FAILED.\033[0m\n' "$pass" "$fail"
  exit 1
fi
