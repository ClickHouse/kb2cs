#!/usr/bin/env bash
# Verify the APACHE half of the migration: the data landed, and the expressions stored
# inside the migrated tiles still return the numbers Elasticsearch returns.
#
#   ./verify-apache.sh
#
# The counterpart to ./verify-migration.sh, which does the same job for nginx. Both share
# verify-migration.py for JSON inspection.
#
# Every expected value below was measured against Elasticsearch on 2026-09-16, with the
# apache integration 3.0.2 pipelines doing the parsing. Three of them deliberately do NOT
# match Elastic, and the "source-platform gaps" section explains why: in each case the
# Elastic side is the one that is wrong, so matching it would mean reproducing a bug.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
DASH="[Logs Apache] Access and error logs (migrated)"

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
q() { python3 verify-migration.py "$1" < /tmp/hdx-apache-objects.txt; }

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

# ---------------------------------------------------------------- summary
echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m%d checks passed.\033[0m The apache migration reproduced cleanly.\n' "$pass"
  echo "Dashboard: http://localhost:8080/dashboards"
  exit 0
else
  printf '\033[31m%d passed, %d FAILED.\033[0m\n' "$pass" "$fail"
  exit 1
fi
