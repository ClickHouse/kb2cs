#!/usr/bin/env bash
# Verify the migrated nginx dashboards -- logs AND metrics -- against Elasticsearch.
#
#   ./verify-nginx.sh
#
# One file per integration, matching verify-{apache,postgres,mysql,system}.sh. This was two
# scripts until 2026-09-17: `verify-migration.sh` (logs) and `verify-metrics.sh` (metrics).
# The first name dated from when this repo was only the nginx migration, so in a repo about
# migrating any Kibana estate it read as "the migration verifier" rather than "the nginx one".
#
# Deliberately different from ./verify.sh, which checks the DATA landed. This checks that the
# migrated objects exist, are shaped correctly, and -- the part that actually matters -- that
# the expressions stored inside the tiles still return the numbers Elasticsearch returns.
# A tile that renders is not a tile that is correct.
#
# For the per-bucket value diff of every tile series, see ../../../verify/.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
OVERVIEW="[Logs Nginx] Overview (migrated)"
LOGSDASH="[Logs Nginx] Access and error logs (migrated)"
DASH_METRICS="[Metrics Nginx] Overview (migrated)"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; fail=$((fail+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ch() { docker compose exec -T clickstack clickhouse-client --query "$1" </dev/null 2>/dev/null | tr -d '\r'; }

# `note` and `expect` come from the metrics half, which used to be a separate file. They are
# NOT optional: without `expect` every call is a "command not found", which prints nothing and
# increments nothing -- so eighteen checks vanished while the section header still appeared and
# the run still exited 0. A missing helper is indistinguishable from a passing section.
note() { printf '  \033[90mnote\033[0m %s\n' "$1"; }
expect() { # expect <label> <expected> <query>
  local got
  got=$(ch "$3" | tr '\n' ' ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "got '$got', expected '$2'"; fi
}

# ---------------------------------------------------------------- fetch the objects
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

printf '%s' "$API" > /tmp/hdx-objects.txt

# Everything below reads this one fetch; python does the JSON work.
inspect() { python3 ./hdx-objects.py "$1" </tmp/hdx-objects.txt 2>/dev/null; }
# The metrics half of this file was a separate script and reads the same objects
# under a different name. One fetch serves both: the payload is identical.
q() { inspect "$1"; }

for spec in "dash:$OVERVIEW:8" "dash:$LOGSDASH:4"; do
  IFS=: read -r _ name want <<<"${spec#dash:}xx" 2>/dev/null || true
done

# dashboards present, with the expected tile counts
for pair in "$OVERVIEW|8" "$LOGSDASH|4"; do
  name="${pair%|*}"; want="${pair#*|}"
  got=$(inspect "tilecount:$name")
  if [ -z "$got" ]; then
    bad "dashboard missing: $name" "re-run the migration (see ../../RUNBOOK.md step 7)"
  elif [ "$got" != "$want" ]; then
    bad "$name has $got tiles, expected $want"
  else
    ok "$name — $got tiles"
  fi
done

# saved searches present
for name in "Nginx access logs (migrated)" "Nginx error logs (migrated)"; do
  if [ "$(inspect "search:$name")" = "yes" ]; then ok "saved search — $name"
  else bad "saved search missing: $name"; fi
done

# ---------------------------------------------------------------- the double-count trap
head_ "Stream scoping (the trap that doubles every access number)"
unscoped=$(inspect "unscoped:$OVERVIEW")
if [ -z "$unscoped" ]; then
  ok "every queryable tile carries a log.stream predicate"
else
  bad "tiles with no log.stream filter: $unscoped" \
      "without it access_combined double-counts the same 499,964 requests"
fi

# ---------------------------------------------------------------- data invariants
head_ "Data invariants (independent of the dashboards)"
A="LogAttributes['log.stream']='access_json'"

check_num() { # label expected actual
  if [ "$3" = "$2" ]; then ok "$1 = $3"; else bad "$1 = $3, expected $2"; fi
}
check_num "access requests"  499964      "$(ch "SELECT count() FROM otel_logs WHERE $A")"
check_num "error entries"    11476       "$(ch "SELECT count() FROM otel_logs WHERE LogAttributes['log.stream']='error'")"
check_num "distinct IPs"     12659       "$(ch "SELECT uniqExact(LogAttributes['remote_addr']) FROM otel_logs WHERE $A")"
check_num "bytes sent"       10557094271 "$(ch "SELECT sum(toUInt64(LogAttributes['body_bytes_sent'])) FROM otel_logs WHERE $A")"
check_num "5xx"              2534        "$(ch "SELECT countIf(toUInt16(LogAttributes['status'])>=500) FROM otel_logs WHERE $A")"

want_status="2xx	376837
3xx	95196
4xx	25397
5xx	2534"
got_status=$(ch "SELECT concat(toString(intDiv(toUInt16(LogAttributes['status']),100)),'xx') AS f, count() FROM otel_logs WHERE $A GROUP BY f ORDER BY f")
if [ "$got_status" = "$want_status" ]; then ok "status families — all four exact"
else bad "status families differ" "got: $(echo "$got_status" | tr '\n' ' ')"; fi

want_levels="warn	5438
error	5097
info	622
crit	172
notice	135
alert	12"
got_levels=$(ch "SELECT LogAttributes['level'] AS l, count() AS c FROM otel_logs WHERE LogAttributes['log.stream']='error' GROUP BY l ORDER BY c DESC")
if [ "$got_levels" = "$want_levels" ]; then ok "error levels — all six exact"
else bad "error levels differ" "got: $(echo "$got_levels" | tr '\n' ' ')"; fi

# ---------------------------------------------------------------- the migrated expressions
# These run whatever groupBy is stored INSIDE the tile -- the ua_browser/ua_os columns that
# ua.sh builds, or a hand-written multiIf if ua.sh was never run -- so they fail if the
# migration regressed either way.
head_ "Migrated tile expressions vs Elastic's ua-parser output"

if [ "$(ch "SELECT count() FROM system.columns WHERE table='otel_logs' AND name='ua_browser'")" = "1" ]; then
  ok "ua_browser/ua_os columns present (ua.sh has been run)"
else
  printf '  \033[90mnote\033[0m ua.sh has not been run; tiles are using an inline multiIf\n'
fi

os_expr=$(inspect "groupby:$OVERVIEW:Operating systems breakdown")
if [ -z "$os_expr" ]; then
  bad "could not read the Operating systems tile expression"
else
  # Re-baselined 2026-09-16: the Kibana panel is a TWO-RING donut,
  # terms(user_agent.os.name) + terms(user_agent.os.version), and the migrated tile grouped on
  # the family alone until now -- the version ring had been silently dropped since 2026-08-20.
  # A ClickStack pie takes a SINGLE groupBy column, so the two rings flatten into one label.
  # These 8 buckets are Elastic's full (name, version) distribution; `Linux` carries no
  # user_agent.os.version, which is why it appears family-only rather than as "Linux ".
  want_os="Windows 10	127469
iOS 18.6	85434
Mac OS X 10.15.7	54539
Android 15	47414
Android 14	33234
iOS 17.6.1	18115
Linux	12076
Windows 7	6125"
  got_os=$(ch "SELECT $os_expr AS g, count() AS c FROM otel_logs WHERE $A GROUP BY g HAVING g!='Other' ORDER BY c DESC")
  if [ "$got_os" = "$want_os" ]; then ok "OS breakdown — all 8 name+version buckets match Elastic"
  else bad "OS breakdown differs from Elastic" "got: $(echo "$got_os" | tr '\n' ' ')"; fi
  printf '  \033[90mnote\033[0m the version ring is included since 2026-09-16; '\''Other'\'' is still\n'
  printf '       excluded because Elastic omits user_agent.os.name entirely on a UA it cannot\n'
  printf '       parse, where uap-core returns the literal '\''Other'\'' -- a dictionary\n'
  printf '       difference, not a migration error.\n' 
fi

br_expr=$(inspect "groupby:$OVERVIEW:Browsers breakdown")
if [ -z "$br_expr" ]; then
  bad "could not read the Browsers tile expression"
else
  # Re-baselined 2026-09-16 for the same reason as the OS tile: two-ring donut, flattened.
  # 25 buckets where the family-only version had 20 -- e.g. Go-http-client splits into
  # 2.0 (8,797) and 1.1 (3,045), which sum to the old 11,842.
  #
  # ONE ROW DELIBERATELY DISAGREES WITH ELASTIC: Elastic reports "Firefox 141.0." with a
  # trailing separator, because it appends one for a version capture group that matched the
  # EMPTY string. A ClickHouse back-reference cannot distinguish that from a group that did
  # not match, so reproducing it would mean reproducing the bug. Same 23,106 rows either way.
  # See INTEGRATIONS.md, "Where the source platform is wrong" -- verify-apache.sh asserts the
  # identical divergence.
  want_br="Chrome 139.0.0.0	94683
Mobile Safari 18.6	69961
Chrome Mobile 139.0.0.0	37960
Edge 139.0.0.0	36269
Chrome Mobile 138.0.0.0	33234
Other	23464
Firefox 141.0	23106
Safari 18.6	22293
Mobile Safari 17.6	18115
Chrome 138.0.0.0	17733
axios 1.7.4	16265
Mobile Safari UI/WKWebView	15473
Googlebot 2.1	14723
okhttp 4.12.0	11731
Python Requests 2.32	10257
Android 15	9454
Go-http-client 2.0	8797
bingbot 2.0	7981
Chrome 58.0.3029.110	6125
AhrefsBot 7.0	6094
SemrushBot 7	4432
Applebot 0.1	4318
Go-http-client 1.1	3045
curl 8.7.1	2762
masscan 1.3	1689"
  got_br=$(ch "SELECT $br_expr AS g, count() AS c FROM otel_logs WHERE $A GROUP BY g ORDER BY c DESC")
  if [ "$got_br" = "$want_br" ]; then ok "browser breakdown — all 25 name+version buckets match Elastic (bar the documented Firefox separator)"
  else
    bad "browser breakdown differs from Elastic"
    diff <(printf '%s\n' "$want_br") <(printf '%s\n' "$got_br") | head -12 | sed 's/^/       /'
  fi
fi

# ---------------------------------------------------------------- geo (vendor-dependent)
head_ "Geo (DB-IP vs MaxMind — expected to differ)"
if [ "$(ch "SELECT count() FROM system.columns WHERE table='otel_logs' AND name='geo_country_code'")" != "1" ]; then
  bad "geo_country_code column missing" "run ./geoip.sh"
else
  read -r us cn jp <<<"$(ch "SELECT countIf(geo_country_code='US'), countIf(geo_country_code='CN'), countIf(geo_country_code='JP') FROM otel_logs WHERE $A" | tr '\t' ' ')"
  # Elastic's GeoLite2 answers, for reference: 182300 / 46221 / 25800
  if [ "${us:-0}" -gt 150000 ] && [ "${cn:-0}" -gt 40000 ] && [ "${jp:-0}" -gt 20000 ]; then
    ok "geo populated — US $us · CN $cn · JP $jp (Elastic: 182300 · 46221 · 25800)"
    ok "reminder: CA differs by ~43% between the two databases; this is not a bug"
  else
    bad "geo looks unpopulated — US $us · CN $cn · JP $jp" "run ./geoip.sh"
  fi
fi

# ---------------------------------------------------------------- cross-stack alignment
# Advisory, not a pass/fail: this is about the two stacks agreeing on the CLOCK, which is a
# property of how they were loaded, not of the migration. Skipped when ES is not running.
head_ "Cross-stack time alignment (advisory)"
es_min=$(curl -s -u elastic:changeme --max-time 5 \
  "http://localhost:9200/logs-nginx.access-default/_search" \
  -H 'Content-Type: application/json' \
  -d '{"size":0,"aggs":{"mn":{"min":{"field":"@timestamp"}}}}' 2>/dev/null \
  | python3 -c "import json,sys; print(int(json.load(sys.stdin)['aggregations']['mn']['value']))" 2>/dev/null)

if [ -z "$es_min" ]; then
  printf '  \033[90mskip\033[0m Elasticsearch not reachable — cannot compare load offsets\n'
else
  ch_min=$(ch "SELECT toUnixTimestamp64Milli(min(Timestamp)) FROM otel_logs WHERE $A")
  skew=$(( (ch_min - es_min) / 1000 )); skew=${skew#-}
  if [ "$skew" -le 60 ]; then
    ok "both stacks start within ${skew}s of each other — wall-clock windows are comparable"
  else
    printf '  \033[33mwarn\033[0m the two stacks are %ss apart (%s min)\n' "$skew" "$((skew/60))"
    printf '       Same 499,964 requests, different absolute times: each loader computes its\n'
    printf '       own now-based shift, so the skew equals the gap between the two loads.\n'
    printf '       Totals and distributions still match; only wall-clock windows disagree.\n'
    printf '       See "Keeping the two stacks on the same clock" in ../../RUNBOOK.md.\n'
  fi
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
                                  FROM default.otel_metrics_sum WHERE MetricName LIKE 'nginx.%'")"
[ -n "${WIN_FROM:-}" ] || { echo "  could not derive the comparison window from default.otel_metrics_sum"; exit 1; }

SUM="default.otel_metrics_sum"
GAU="default.otel_metrics_gauge"

tiles=$(q "tilecount:$DASH_METRICS")
if [ "$tiles" = "9" ]; then ok "$DASH_METRICS — 9 tiles (8 panels + provenance note)"
else bad "expected 9 tiles, found '${tiles:-none}'" "re-run the migration"; fi

tags=$(q "tags:$DASH_METRICS")
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
SQL=$(q "sqltemplate:$DASH_METRICS:Drops")
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
RSQL=$(q "sqltemplate:$DASH_METRICS:Request Rate")
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

# ---------------------------------------------------------------- gauge tiles must be SQL
head_ "Gauge panels are SQL tiles (regression guard added 2026-09-16)"

# Found during the mysql migration: on a metric source HyperDX collapses a gauge to ONE
# sample per bucket (the last) BEFORE aggFn runs, so `avg`/`max`/`min`/`sum`/`last_value` all
# return the same number and none of them is Kibana's average()/max() over the bucket's
# samples. These tiles were builder tiles until then and disagreed with Elastic in
# 141/144 and 23-141/144 buckets respectively -- while the whole-window averages checked above still matched, which is
# exactly why nothing caught it. If someone "simplifies" them back, these checks fail.
for t in "Active connections" "Reading / Writing / Waiting Rates"; do
  if [ -n "$(q "sqltemplate:$DASH_METRICS:$t")" ]; then ok "$t is a SQL tile"
  else bad "$t must be a SQL tile" "a builder gauge tile cannot aggregate within a bucket"; fi
done


# ---------------------------------------------------------------- summary
printf '\n'
if [ "$fail" -eq 0 ]; then
  printf '\033[32m%s checks passed.\033[0m The nginx migration reproduced cleanly.\n' "$pass"
  printf 'Dashboards: http://localhost:8080/dashboards\n'
  exit 0
else
  printf '\033[31m%s passed, %s failed.\033[0m\n' "$pass" "$fail"
  exit 1
fi
