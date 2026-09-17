#!/usr/bin/env bash
# Verify the migrated apache dashboards -- logs AND metrics -- against Elasticsearch.
#
#   ./verify-apache.sh
#
# One file per integration, matching verify-{nginx,postgres,mysql,system}.sh. This was two
# scripts until 2026-09-17: verify-apache.sh (logs) and verify-apache-metrics.sh (metrics).
#
# Checks that the migrated objects exist and are shaped correctly, and that the expressions
# stored inside the tiles still return the numbers Elasticsearch returns -- including the four
# places where Elastic's own apache parsing is at fault and the two platforms deliberately
# disagree.
#
# For the per-bucket value diff of every tile series, see ../../../verify/.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
DASH="[Logs Apache] Access and error logs (migrated)"
DASH_METRICS="[Metrics Apache] Overview (migrated)"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; fail=$((fail+1)); }
note() { printf '  ....  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ch() { docker compose exec -T clickstack clickhouse-client --query "$1" </dev/null 2>/dev/null | tr -d '\r'; }

# Compare one ClickHouse scalar against an expected value.
#
# Trims only the ENDS. Several checks below compare a whole distribution rendered as one
# space-separated string, so stripping interior whitespace (tr -d '[:space:]') would make
# every one of them fail no matter what the data says.
expect() { # expect <label> <expected> <query>
  local got
  got=$(ch "$3" | tr '\n' ' ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "got '$got', expected '$2'"; fi
}

ACC="LogAttributes['log.stream'] = 'apache_access'"
ERR="LogAttributes['log.stream'] = 'apache_error'"

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
  echo "  ClickStack is not answering on :8080. Start it:  docker compose up -d"
  exit 1
fi

# ClickStack's own API is on 8000 and is container-internal only, so this reaches it from
# inside rather than from the host.
API=$(docker compose exec -T clickstack sh -c "
  rm -f /tmp/ck
  curl -s -c /tmp/ck -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d '{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}' http://localhost:8000/login/password
  echo '<<<DASHBOARDS>>>'; curl -s -b /tmp/ck http://localhost:8000/dashboards
  echo '<<<SEARCHES>>>';   curl -s -b /tmp/ck http://localhost:8000/saved-search
" </dev/null 2>/dev/null)

if [ -z "$API" ]; then
  echo "  Could not log in to the HyperDX API as $EMAIL."
  echo "  If you recreated the stack with different credentials, set HDX_EMAIL/HDX_PASSWORD."
  exit 1
fi
printf '%s' "$API" > /tmp/hdx-apache-objects.txt
q() { python3 hdx-objects.py "$1" < /tmp/hdx-apache-objects.txt; }

# 7 source data panels -> 7 data tiles, plus the provenance markdown = 8.
tiles=$(q "tilecount:$DASH")
if [ "$tiles" = "8" ]; then ok "$DASH — 8 tiles (7 panels + provenance note)"
else bad "$DASH — expected 8 tiles, found '${tiles:-none}'" \
         "re-run the migration; see MIGRATION.md"; fi

tags=$(q "tags:$DASH")
if [ "$tags" = "apache,migrated-from-kibana" ]; then ok "tagged apache + migrated-from-kibana"
else bad "tags are '$tags'" "expected 'apache,migrated-from-kibana'"; fi

# 7 source panels -> 1 markdown + 1 bar (the degraded map) + 2 stacked_bar + 2 pie
# + 1 table + 1 search. The bar is the tell that the map degraded rather than vanished.
types=$(q "displaytypes:$DASH")
want="bar,markdown,pie,pie,search,stacked_bar,stacked_bar,table"
if [ "$types" = "$want" ]; then ok "tile types: $want"
else bad "tile types are '$types'" "expected '$want'"; fi

# ---------------------------------------------------------------- the scoping trap
head_ "Stream scoping (the trap that would fold nginx into every apache number)"

# otel_logs holds both services. A builder tile has no tile-level WHERE, so the predicate
# lives on each select item -- and if it is missing, the tile silently counts nginx too.
unscoped=$(q "unscoped:$DASH")
if [ -z "$unscoped" ]; then ok "every queryable tile carries a log.stream predicate"
else bad "tiles with no log.stream filter: $unscoped" \
         "see MIGRATION.md, 'the filter trap'"; fi

# ---------------------------------------------------------------- data invariants
head_ "Data invariants (independent of the dashboard)"

expect "apache access requests = 249,997"         "249997" \
  "SELECT count() FROM default.otel_logs WHERE $ACC"
expect "apache error entries = 14665"            "14665" \
  "SELECT count() FROM default.otel_logs WHERE $ERR"
expect "bytes sent = 49,005,925,124"             "49005925124" \
  "SELECT sum(toUInt64OrZero(LogAttributes['body_bytes_sent'])) FROM default.otel_logs WHERE $ACC"
expect "nginx still holds 1,011,404 rows"        "1011404" \
  "SELECT count() FROM default.otel_logs WHERE ServiceName = 'nginx'"

# One check, whole distribution: matching a top-3 you wrote down is not matching the panel.
expect "status codes — all 7 exact"  "200=193704 206=412 304=41392 403=3555 404=10789 500=68 503=77" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(toString(s),'=',toString(c)))),' ')
   FROM (SELECT toUInt16(LogAttributes['status']) s, count() c FROM default.otel_logs
         WHERE $ACC GROUP BY s)"

expect "error log levels — all 4 exact"  "error=3768 info=10885 notice=8 warn=4" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(l,'=',toString(c)))),' ')
   FROM (SELECT LogAttributes['level'] l, count() c FROM default.otel_logs
         WHERE $ERR GROUP BY l)"

expect "error modules — all 8 exact" \
  "authz_core=2872 autoindex=683 cgid=136 core=10791 mpm_event=6 proxy=77 reqtimeout=96 ssl=4" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(m,'=',toString(c)))),' ')
   FROM (SELECT LogAttributes['module'] m, count() c FROM default.otel_logs
         WHERE $ERR GROUP BY m)"

# ---------------------------------------------------------------- tile expressions
head_ "Migrated tile expressions vs Elastic's ua-parser output"

if [ "$(ch "SELECT count() FROM system.columns WHERE database='default' AND table='otel_logs'
              AND name IN ('ua_browser_version','ua_os_version')" | tr -d '[:space:]')" = "2" ]; then
  ok "ua_browser_version / ua_os_version columns present (ua.sh has been run)"
else
  bad "the ua version columns are missing" "run ./ua.sh -- the two donut tiles need them"
fi

# The apache donuts group on name AND version, which is what forced the version components
# into the dictionary in the first place.
#
# All 7 of Elastic's name x version buckets, plus the one extra ClickHouse necessarily has:
# Elastic writes no os.version for Linux agents, so those 6,707 rows sit in no Kibana slice,
# while a ClickHouse column has to hold something (''). Same partition, one visible extra.
expect "OS breakdown — all 7 Elastic buckets match, + the documented Linux one" \
  "Android 14=15326 Android 15=18092 Linux =6707 Mac OS X 10.15.7=33943 Windows 10=79556 Windows 7=3842 iOS 17.6.1=8397 iOS 18.6=31452" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(os,' ',v,'=',toString(c)))),' ')
   FROM (SELECT ua_os os, ua_os_version v, count() c FROM default.otel_logs
         WHERE $ACC AND ua_os != 'Other' GROUP BY os, v)"

expect "browser versions parsed on 238,507 of 249,997 rows" "238507" \
  "SELECT countIf(ua_browser_version != '') FROM default.otel_logs WHERE $ACC"

expect "Firefox bucket = 15,801 (Elastic agrees on the count)" "15801" \
  "SELECT count() FROM default.otel_logs WHERE $ACC AND ua_browser = 'Firefox'"

expect "status x URL pairs = 290 (the table tile's row count)" "290" \
  "SELECT uniqExact((LogAttributes['status'], LogAttributes['request_uri']))
   FROM default.otel_logs WHERE $ACC"

# ---------------------------------------------------------------- known source-platform gaps
head_ "Source-platform gaps (ClickStack is the correct side — do not 'fix' these)"

# 1. The integration's grok cannot parse a bare IPv6 client address out of a combined log:
#    it keeps only the final hextet and files it under source.domain. 60 distinct IPv6
#    addresses collapse onto 59 hextets, which is why Elastic's unique-IP counts run low.
expect "IPv6 clients kept whole: 214 distinct, 7,881 rows" "214 7881" \
  "SELECT concat(toString(uniqExactIf(LogAttributes['remote_addr'], position(LogAttributes['remote_addr'],':')>0)),
                 ' ', toString(countIf(position(LogAttributes['remote_addr'],':')>0)))
   FROM default.otel_logs WHERE $ACC"
note "Elastic reports 5,716 unique IPs here, ClickStack 5,718 — its grok truncates IPv6."

# 2. Elastic drops url.original entirely when the request line contains backslashes, so its
#    URL panel silently omits these 503 requests. Status, IP and UA survive on both sides.
expect "backslash request line kept: 503 rows, URL intact" "503" \
  "SELECT count() FROM default.otel_logs WHERE $ACC
   AND LogAttributes['request_uri'] LIKE '%invokefunction%'"
note "Elastic has 190 distinct URLs to ClickStack's 191, for that reason."

# 3. Elastic's user_agent processor emits a trailing dot when a version capture group
#    matches the empty string, so its Firefox bucket is literally "141.0." -- see ua.sql.
expect "Firefox version rendered as 141.0, not Elastic's '141.0.'" "141.0" \
  "SELECT DISTINCT ua_browser_version FROM default.otel_logs
   WHERE $ACC AND ua_browser = 'Firefox'"

# ---------------------------------------------------------------- alignment
head_ "Cross-stack time alignment"

# apache's %t is whole seconds, the same precision the Elasticsearch loader reads, so the
# two stacks compute a byte-identical shift for apache -- unlike nginx, whose $msec is
# millisecond-precision. This should be exact, not approximate.
# Asserted against the ELASTIC stack's own maximum rather than a literal date: the property
# that matters is that the two stacks agree to the second, and that survives a re-anchor.
ES_MAX=$(curl -s -u "${AUTH:-elastic:changeme}" "${ES:-http://localhost:9200}/logs-apache.access-default/_search?size=0" \
  -H 'Content-Type: application/json' -d '{"aggs":{"m":{"max":{"field":"@timestamp"}}}}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['aggregations']['m']['value_as_string'][:19].replace('T',' '))" 2>/dev/null)
if [ -n "$ES_MAX" ]; then
  expect "apache ends at the same second on both stacks ($ES_MAX)" "$ES_MAX" \
    "SELECT formatDateTime(max(Timestamp), '%Y-%m-%d %H:%i:%S') FROM default.otel_logs WHERE $ACC"
else
  bad "could not read apache's maximum @timestamp from Elasticsearch" "is it running?"
fi


# ---------------------------------------------------------------- the comparison window
# DERIVED FROM THE DATA, aligned to whole hours. Hardcoding it meant every stored-SQL check
# in this file broke the moment the corpus was re-anchored -- which RUNBOOK.md documents as a
# routine operation. Whole hours because a window that ends mid-bucket makes the last bucket
# partial, which reads as a value bug and is not one.
# IFS on the separator, not whitespace: the timestamps contain a space, so a plain
# `read -r A B` puts only the DATE in A and everything else in B.
IFS='|' read -r WIN_FROM WIN_TO <<<"$(ch "SELECT concat(toString(toStartOfHour(min(TimeUnix))), '|',
                                                toString(toStartOfHour(max(TimeUnix))))
                                  FROM default.otel_metrics_sum WHERE MetricName LIKE 'apache.%'")"
[ -n "${WIN_FROM:-}" ] || { echo "  could not derive the comparison window from default.otel_metrics_sum"; exit 1; }

SUM="default.otel_metrics_sum"
GAU="default.otel_metrics_gauge"

# 11 source panels -> 10 data tiles + a gap note for the unmigratable one + provenance.
tiles=$(q "tilecount:$DASH_METRICS")
if [ "$tiles" = "12" ]; then ok "$DASH_METRICS — 12 tiles (10 data + gap note + provenance)"
else bad "expected 12 tiles, found '${tiles:-none}'" "re-run the migration"; fi

tags=$(q "tags:$DASH_METRICS")
if [ "$tags" = "apache,metrics,migrated-from-kibana" ]; then ok "tagged apache + metrics + migrated-from-kibana"
else bad "tags are '$tags'"; fi

# ---------------------------------------------------------------- data landed
head_ "Series landed (5,760 scrapes x 25 points)"

expect "sum points = 40,320"     "40320" "SELECT count() FROM $SUM WHERE MetricName LIKE 'apache.%'"
expect "gauge points = 103,680"  "103680" "SELECT count() FROM $GAU WHERE MetricName LIKE 'apache.%'"
expect "2 hosts as resource attributes" "2" \
  "SELECT uniqExact(ResourceAttributes['host.name']) FROM $SUM WHERE MetricName='apache.requests'"
expect "counters cumulative + monotonic" "2 true" \
  "SELECT concat(toString(any(AggregationTemporality)),' ',toString(any(IsMonotonic)))
   FROM $SUM WHERE MetricName='apache.requests'"
expect "nginx metrics untouched" "25920 34560" \
  "SELECT concat(
     toString((SELECT count() FROM $SUM WHERE MetricName LIKE 'nginx.%')),' ',
     toString((SELECT count() FROM $GAU WHERE MetricName LIKE 'nginx.%')))"

# ---------------------------------------------------------------- counters vs Elastic
head_ "Raw counters vs Elasticsearch (the three sql number tiles)"

expect "Uptime max = 90,897"                   "90897" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='apache.uptime'"
expect "Total accesses max = 125,267"          "125267" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='apache.requests'"
expect "Total egress max = 25,069,294,039"     "25069294039" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='apache.traffic'"
note "total_bytes across both nodes is 49,005,925,124 -- the same figure verify-apache.sh"
note "checks as sum(body_bytes_sent) on the access log. The metrics and logs agree."

# ---------------------------------------------------------------- the reshape
head_ "The reshape: one metric + an attribute, per Elastic field family"

# Eleven scoreboard fields -> one metric keyed by `state`. Every average must survive.
expect "scoreboard — all 11 state averages match Elastic" \
  "closing=0 dnslookup=0 finishing=0.0262 idle_cleanup=0.0092 keepalive=0.5373 logging=0 open=1.9467 reading=0.0054 sending=2.0552 starting=0.0179 waiting=145.4021" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(s,'=',toString(v)))),' ') FROM
     (SELECT Attributes['state'] AS s, round(avg(Value),4) AS v FROM $GAU
      WHERE MetricName='apache.scoreboard' GROUP BY s)"
# `total` is not a state: it is MaxRequestWorkers, the sum of the other eleven.
expect "scoreboard.total derives as 150 (MaxRequestWorkers)" "150" \
  "SELECT toInt64(round(avg(t))) FROM
     (SELECT TimeUnix, ResourceAttributes['host.name'] AS h, sum(Value) AS t FROM $GAU
      WHERE MetricName='apache.scoreboard' GROUP BY TimeUnix, h)"

expect "workers — busy/idle match Elastic" "busy=2.5979 idle=147.4021" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(s,'=',toString(v)))),' ') FROM
     (SELECT Attributes['state'] AS s, round(avg(Value),4) AS v FROM $GAU
      WHERE MetricName='apache.workers' GROUP BY s)"

# Five cpu fields -> one Sum keyed by level+mode, plus a separate gauge. Three decimals:
# the OTLP JSON round-trip moves the 4th by <0.0001.
expect "cpu.time — all 4 level/mode combinations match Elastic" \
  "children/system=1.531 children/user=3.061 self/system=9.916 self/user=22.886" \
  "SELECT arrayStringConcat(arraySort(groupArray(concat(l,'/',m,'=',toString(v)))),' ') FROM
     (SELECT Attributes['level'] AS l, Attributes['mode'] AS m, round(avg(Value),3) AS v
      FROM $SUM WHERE MetricName='apache.cpu.time' GROUP BY l, m)"
expect "cpu.load = 0.0553"  "0.0553" \
  "SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='apache.cpu.load'"
expect "load 1/5/15 = 0.4154 / 0.4147 / 0.4148" "0.4154 0.4147 0.4148" \
  "SELECT concat(
     toString((SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='apache.load.1')),' ',
     toString((SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='apache.load.5')),' ',
     toString((SELECT round(avg(Value),4) FROM $GAU WHERE MetricName='apache.load.15')))"
expect "current_connections avg 2.4075 / max 7" "2.4075 7" \
  "SELECT concat(toString(round(avg(Value),4)),' ',toString(toInt64(max(Value))))
   FROM $GAU WHERE MetricName='apache.current_connections'"

# ---------------------------------------------------------------- derived rates
head_ "Panels with NO metric on the target, derived from the counters"

# mod_status computes ReqPerSec/BytesPerSec as counter / uptime -- an average since server
# start. The OTel receiver emits neither, and deriving them reproduces Elastic exactly.
expect "requests_per_sec derives to 0.849, as Elastic reports" "0.849" \
  "WITH j AS (SELECT TimeUnix, ResourceAttributes['host.name'] AS h,
                maxIf(Value, MetricName='apache.requests') AS n,
                maxIf(Value, MetricName='apache.uptime')   AS u
              FROM $SUM WHERE MetricName IN ('apache.requests','apache.uptime')
              GROUP BY TimeUnix, h)
   SELECT round(avg(n/u),4) FROM j"
expect "bytes_per_sec derives to 168,998.1229, as Elastic reports" "168998.1229" \
  "WITH j AS (SELECT TimeUnix, ResourceAttributes['host.name'] AS h,
                maxIf(Value, MetricName='apache.traffic') AS n,
                maxIf(Value, MetricName='apache.uptime')  AS u
              FROM $SUM WHERE MetricName IN ('apache.traffic','apache.uptime')
              GROUP BY TimeUnix, h)
   SELECT round(avg(n/u),4) FROM j"

# ---------------------------------------------------------------- stored SQL
head_ "Stored tile SQL (run what the tile saves, not an equivalent)"

SQL=$(q "sqltemplate:$DASH_METRICS:Scoreboard")
if [ -z "$SQL" ]; then
  bad "could not read the Scoreboard tile's sqlTemplate"
else
  EXP=$(printf '%s' "$SQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g" \
    | sed 's/\$__timeInterval(TimeUnix)/toStartOfInterval(TimeUnix, INTERVAL 3600 second)/g')
  got=$(ch "SELECT concat(toString(count()),' ',toString(uniqExact(series)),' ',toString(uniqExact(ts)))
              FROM ($EXP)" | tr -d '[:space:]')
  if [ "$got" = "5282224" ]; then
    ok "Scoreboard tile's own SQL: 528 rows = 22 series x 24 buckets, none dropped"
  else
    bad "Scoreboard tile's own SQL gives '$got', expected '5282224'" \
        "22 series x 24 buckets; a builder groupBy truncates this to ~60 rows"
  fi
fi

# The CPU panel carries terms(host.hostname) in Kibana, so its series are (metric x host).
# An earlier version averaged across hosts: 5 series instead of 10, and the per-node
# divergence the panel exists to show was gone. A terms breakdown is a dimension, not
# decoration.
CSQL=$(q "sqltemplate:$DASH_METRICS:CPU")
if [ -z "$CSQL" ]; then
  bad "could not read the CPU usage tile's sqlTemplate"
else
  CEXP=$(printf '%s' "$CSQL" \
    | sed "s/\$__timeFilter(TimeUnix)/TimeUnix >= '$WIN_FROM' AND TimeUnix <= '$WIN_TO'/g" \
    | sed 's/\$__timeInterval(t)/toStartOfInterval(t, INTERVAL 3600 second)/g')
  got=$(ch "SELECT concat(toString(count()),' ',toString(uniqExact(series)),' ',toString(uniqExact(ts)))
              FROM ($CEXP)" | tr -d '[:space:]')
  if [ "$got" = "2401024" ]; then
    ok "CPU tile's own SQL: 240 rows = 10 series (5 metrics x 2 hosts) x 24 buckets"
  else
    bad "CPU tile's own SQL gives '$got', expected '2401024'" \
        "5 metrics x 2 hosts x 24 buckets; averaging across hosts gives only 5 series"
  fi
fi

# ---------------------------------------------------------------- platform semantics
head_ "Platform semantics (why 7 of 10 data tiles are SQL)"

# Same de-cumulation as nginx: aggFn on a Sum sees the increase, not the value.
expect "a Sum tile cannot show the raw counter (it is 125,267, not a bucket delta)" "125267" \
  "SELECT toInt64(max(Value)) FROM $SUM WHERE MetricName='apache.requests'"
note "max(apache.requests)=125,267 but a builder Sum tile returns per-bucket increases."
note "Gauges are unaffected: max on apache.current_connections is an ordinary tile."
note "Connections (async writing/keep_alive/closing) has NO OTel metric -- a collection"
note "gap, not a chart-type gap. It is the one panel of eleven with no target at all."

# ---------------------------------------------------------------- gauge tiles must be SQL
head_ "Gauge panels are SQL tiles (regression guard added 2026-09-16)"

# Found during the mysql migration: on a metric source HyperDX collapses a gauge to ONE
# sample per bucket (the last) BEFORE aggFn runs, so `avg`/`max`/`min`/`sum`/`last_value` all
# return the same number and none of them is Kibana's average()/max() over the bucket's
# samples. These tiles were builder tiles until then and disagreed with Elastic in
# 92/144 (connections) and 144/144 (server load) buckets -- while the whole-window averages checked above still matched, which is
# exactly why nothing caught it. If someone "simplifies" them back, these checks fail.
for t in "Total connections" "Workers" "Average server load"; do
  if [ -n "$(q "sqltemplate:$DASH_METRICS:$t")" ]; then ok "$t is a SQL tile"
  else bad "$t must be a SQL tile" "a builder gauge tile cannot aggregate within a bucket"; fi
done


# ---------------------------------------------------------------- summary
printf '\n'
if [ "$fail" -eq 0 ]; then
  printf '\033[32m%s checks passed.\033[0m The apache migration reproduced cleanly.\n' "$pass"
  printf 'Dashboards: http://localhost:8080/dashboards\n'
  exit 0
else
  printf '\033[31m%s passed, %s failed.\033[0m\n' "$pass" "$fail"
  exit 1
fi
