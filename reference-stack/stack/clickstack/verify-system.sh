#!/usr/bin/env bash
# Verify the SYSTEM migration -- six dashboards, two log streams and eight metric streams.
#
#   ./verify-system.sh
#
# The ninth verifier here, and the widest: `system` monitors the fleet the other four
# integrations serve, so it has more data streams (10) than all of them combined (6).
#
# Every expectation was measured against the Elastic stack on 2026-09-17 over the absolute
# window 2026-09-15T15:00Z .. 2026-09-16T14:00Z. Counts that depend on the window say so.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

EMAIL="${HDX_EMAIL:-train@example.com}"
PASS="${HDX_PASSWORD:-TrainingP4ss!}"
D_SUDO="[Logs System] Sudo commands"
D_SYSLOG="[Logs System] Syslog dashboard"
D_USERS="[Logs System] New users and groups"
D_SSH="[Logs System] SSH login attempts"
D_HOST="[Metrics System] Host overview"
D_OVER="[Metrics System] Overview"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; fail=$((fail+1)); }
note() { printf '  \033[90mnote\033[0m %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

ch() { docker compose exec -T clickstack clickhouse-client --query "$1" </dev/null 2>/dev/null | tr -d '\r'; }
expect() {
  local got
  got=$(ch "$3" | tr '\n\t' '  ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/  */ /g')
  if [ "$got" = "$2" ]; then ok "$1"; else bad "$1" "got '$got', expected '$2'"; fi
}

SYS="ServiceName = 'system'"
AUTHL="$SYS AND LogAttributes['log.stream'] = 'system_auth'"
SYSL="$SYS AND LogAttributes['log.stream'] = 'system_syslog'"
G="default.otel_metrics_gauge"
S="default.otel_metrics_sum"

# ---------------------------------------------------------------- logs
head_ "Logs: syslog and auth"

expect "40,000 syslog + 16,224 auth rows" "40000 16224" \
  "SELECT countIf(LogAttributes['log.stream'] = 'system_syslog'),
          countIf(LogAttributes['log.stream'] = 'system_auth')
   FROM default.otel_logs WHERE $SYS"
expect "ssh events Accepted/Failed/Invalid" "2561 7487 3952" \
  "SELECT countIf(LogAttributes['ssh_event'] = 'Accepted'),
          countIf(LogAttributes['ssh_event'] = 'Failed'),
          countIf(LogAttributes['ssh_event'] = 'Invalid')
   FROM default.otel_logs WHERE $AUTHL"
expect "ssh methods password/publickey" "8043 2005" \
  "SELECT countIf(LogAttributes['ssh_method'] = 'password'),
          countIf(LogAttributes['ssh_method'] = 'publickey')
   FROM default.otel_logs WHERE $AUTHL"
expect "sudo: 2,097 clean + 103 refused" "2097 103" \
  "SELECT countIf(LogAttributes['sudo_command'] != '' AND LogAttributes['sudo_error'] = ''),
          countIf(LogAttributes['sudo_error'] != '')
   FROM default.otel_logs WHERE $AUTHL"
expect "12 new users == 12 new groups" "12 12" \
  "SELECT countIf(LogAttributes['useradd_shell'] != ''),
          countIf(LogAttributes['group_name'] != '')
   FROM default.otel_logs WHERE $AUTHL"
expect "5 hosts in syslog, 12 distinct sudo commands" "5 12" \
  "SELECT (SELECT uniqExact(LogAttributes['host.hostname']) FROM default.otel_logs WHERE $SYSL),
          (SELECT uniqExact(LogAttributes['sudo_command']) FROM default.otel_logs
           WHERE $AUTHL AND LogAttributes['sudo_command'] != '')"

# THE DELIBERATE DIVERGENCE. Elastic's grok captures `Failed password for invalid user admin`
# as user.name " admin" -- with a leading space -- so over the full corpus Kibana reports 27
# distinct failed usernames where there are really 14, splitting every bot target in two
# (admin 880 + " admin" 759). This migration parses the name cleanly, so `admin` reads 1,639.
# Asserted so that nobody "fixes" ClickStack into reproducing the bug. See INTEGRATIONS.md,
# "Where the source platform is wrong".
expect "no leading-space usernames (Elastic has 13 such buckets)" "0 14" \
  "SELECT countIf(startsWith(LogAttributes['user'], ' ')),
          uniqExact(LogAttributes['user'])
   FROM default.otel_logs
   WHERE $AUTHL AND LogAttributes['ssh_event'] IN ('Failed', 'Invalid')"
expect "admin = 880 + 759, Elastic's two buckets rejoined" "1639" \
  "SELECT count() FROM default.otel_logs
   WHERE $AUTHL AND LogAttributes['ssh_event'] IN ('Failed', 'Invalid')
     AND LogAttributes['user'] = 'admin'"

# geo comes free from the existing materialized columns because the collector emits the SSH
# source IP as `remote_addr` -- but that column answers ZZ, not '', for a row with no IP.
expect "geo: 11,439 located, 2,561 unlocatable, 42,224 have no IP" "11439 2561 42224" \
  "SELECT countIf(LogAttributes['ssh_event'] != '' AND geo_country_code NOT IN ('', 'ZZ')),
          countIf(LogAttributes['ssh_event'] != '' AND geo_country_code = 'ZZ'),
          countIf(LogAttributes['remote_addr'] = '')
   FROM default.otel_logs WHERE $SYS"
note "the 2,561 unlocatable are exactly the Accepted logins: operator IPs are RFC 5737"
note "documentation ranges, which DB-IP correctly declines to place. And ZZ is DB-IP's real"
note "answer for 0.0.0.0, which is what an empty remote_addr casts to -- so any country"
note "breakdown over this service must exclude ('', 'ZZ') or 42,224 IP-less rows dominate."

# ---------------------------------------------------------------- metrics
head_ "Metrics: eight streams"

expect "396,000 points (100,800 sum + 295,200 gauge)" "100800 295200" \
  "SELECT (SELECT count() FROM $S WHERE MetricName LIKE 'system.%'),
          (SELECT count() FROM $G WHERE MetricName LIKE 'system.%')"
expect "counters are Sums, everything else a Gauge" "3 1" \
  "SELECT (SELECT uniqExact(MetricName) FROM $S WHERE MetricName LIKE 'system.network.%'),
          (SELECT uniqExact(MetricName) FROM $S WHERE MetricName LIKE 'system.disk.%')"
expect "dimensions: 5 hosts, 2 ifaces, 1 disk, 2 mounts, 9 processes" "5 2 1 2 9" \
  "SELECT (SELECT uniqExact(ResourceAttributes['host.name']) FROM $G WHERE MetricName = 'system.cpu.utilization'),
          (SELECT uniqExact(Attributes['device']) FROM $S WHERE MetricName = 'system.network.io'),
          (SELECT uniqExact(Attributes['device']) FROM $S WHERE MetricName = 'system.disk.io'),
          (SELECT uniqExact(Attributes['mountpoint']) FROM $G WHERE MetricName = 'system.filesystem.utilization'),
          (SELECT uniqExact(Attributes['process.name']) FROM $G WHERE MetricName = 'system.process.cpu.utilization')"
expect "cpu.utilization carries all 7 states" "7" \
  "SELECT uniqExact(Attributes['state']) FROM $G WHERE MetricName = 'system.cpu.utilization'"

# Two fields the hostmetricsreceiver has no equivalent for, both DERIVED on the target. If
# either derivation drifts, the CPU gauge and the Disk Used tiles go wrong together.
head_ "Derived fields (no OTel equivalent)"
expect "cpu total == sum of the six non-idle states, every scrape" "0" \
  "SELECT countIf(abs(t - (1 - idle)) > 1e-9) FROM (
     SELECT TimeUnix, ResourceAttributes['host.name'] AS h,
            sumIf(Value, Attributes['state'] != 'idle') AS t,
            maxIf(Value, Attributes['state'] = 'idle')  AS idle
     FROM $G WHERE MetricName = 'system.cpu.utilization' GROUP BY TimeUnix, h)"
expect "every scrape has exactly 2 mounts to roll up into fsstat" "0" \
  "SELECT countIf(n != 2) FROM (
     SELECT TimeUnix, ResourceAttributes['host.name'] AS h, count() AS n
     FROM $G WHERE MetricName = 'system.filesystem.utilization' GROUP BY TimeUnix, h)"

# ---------------------------------------------------------------- dashboards
head_ "Migrated dashboards"

up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -sf -o /dev/null --max-time 5 http://localhost:8080/api/health </dev/null && { up=1; break; }
  sleep 2
done
[ "$up" = "1" ] || { echo "  ClickStack is not answering on :8080"; exit 1; }
API=$(docker compose exec -T clickstack sh -c "
  rm -f /tmp/ck
  curl -s -c /tmp/ck -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d '{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}' http://localhost:8000/login/password
  echo '<<<DASHBOARDS>>>'; curl -s -b /tmp/ck http://localhost:8000/dashboards
  echo '<<<SEARCHES>>>';   curl -s -b /tmp/ck http://localhost:8000/saved-search
" </dev/null 2>/dev/null)
[ -n "$API" ] || { echo "  could not log in to the HyperDX API as $EMAIL"; exit 1; }
printf '%s' "$API" > /tmp/hdx-system-objects.txt
q() { python3 hdx-objects.py "$1" < /tmp/hdx-system-objects.txt; }

for spec in "$D_SUDO:5" "$D_SYSLOG:5" "$D_USERS:8" "$D_SSH:7" "$D_HOST:30" "$D_OVER:11"; do
  name="${spec%:*}"; want="${spec##*:}"
  got=$(q "tilecount:$name")
  if [ "$got" = "$want" ]; then ok "$name — $want tiles"
  else bad "$name: expected $want tiles, found '${got:-none}'"; fi
done

# seriesLimit ranks series over UNCONDITIONAL volume, ignoring the select-item `where` that
# scopes each tile. otel_logs holds 1.4M rows with no `host.hostname` at all, so the
# empty-string bucket wins every ranking and the real series are crowded out -- which is
# exactly how `Syslog events by hostname` came back blank on 2026-09-17, the second time this
# trap has fired in this project. Nothing can catch it after the fact: clickstack_timeseries
# has no seriesLimit parameter so the builder path cannot reproduce it, and query_tiles
# reported hasData: true. Asserting its absence is the only defence.
for d in "$D_SUDO" "$D_SYSLOG" "$D_USERS" "$D_SSH" "$D_HOST" "$D_OVER"; do
  found=$(q "serieslimit:$d")
  if [ -z "$found" ]; then ok "$d sets no seriesLimit"
  else bad "$d sets seriesLimit" "$(echo "$found" | tr '\n' ' ') -- it bypasses the select-item where"; fi
done

types=$(q "displaytypes:$D_SSH")
if [ "$types" = "bar,markdown,markdown,search,stacked_bar,stacked_bar,table" ]; then
  ok "$D_SSH display types (map -> bar, tagcloud -> table)"
else bad "$D_SSH display types" "got '$types'"; fi

# THE SECOND RENDERING-LAYER GAP IN THIS PROJECT, after maps. Kibana's two Overview panels are
# heatmaps with a CATEGORICAL y-axis (terms(host.name) x time x average). ClickStack's heatmap
# displayType takes exactly one series, a numeric valueExpression, and NO groupBy -- it is a
# value-distribution heatmap, so it cannot put a dimension on an axis. Both degrade to a line
# chart grouped by host: same data, different rendering.
if [ "$(q "displaytypes:$D_OVER")" = "line,line,markdown,markdown,number,number,number,number,number,number,table" ]; then
  ok "$D_OVER: both heatmaps degraded to line (no categorical heatmap on the target)"
else bad "$D_OVER display types" "got '$(q "displaytypes:$D_OVER")'"; fi
for t in "Top hosts by CPU usage over time" "Top hosts by memory usage over time"; do
  if [ -n "$(q "sqltemplate:$D_OVER:$t")" ]; then ok "$t is a SQL line tile"
  else bad "$t should be SQL" "a heatmap cannot take a groupBy here"; fi
done

# TILE-TO-PANEL FIELD BINDING. The `Top processes by CPU usage` panel aggregates
# `process.cpu.pct`, which metricbeat does NOT normalise by core count -- while
# `system.process.cpu.total.norm.pct` is normalised. They differ by exactly the host's core
# count (4x, 8x and 16x across this fleet). The tile was built against the normalised field
# and was wrong by that factor on every row.
#
# It verified GREEN, because the harness derived its Elasticsearch expectation from the field
# the TILE read rather than the field the PANEL reads. That is the hole this check closes:
# assert the tile names the source panel's own field.
if q "sqltemplate:$D_HOST:Top processes by CPU usage" | grep -q "system.process.cpu.pct'"; then
  ok "Top processes by CPU reads process.cpu.pct (NOT the core-normalised field)"
else
  bad "Top processes by CPU reads the wrong field" \
      "the panel aggregates process.cpu.pct; the normalised field differs by core count"
fi
if q "sqltemplate:$D_HOST:Top processes by CPU usage" | grep -q 'cpu.utilization'; then
  bad "Top processes by CPU still references cpu.utilization" "that is the normalised field"
else
  ok "Top processes by CPU does not reference the normalised field"
fi

# `last_value` in these four panels collapses a dimension the data still carries: grouped
# only by process name, `nginx` has three rows at the newest scrape (one per edge node), and
# Kibana's top_metrics picks one ARBITRARILY among the tie. Neither platform is reproducible
# there, so the tiles average over the collapsed dimension at the newest scrape instead --
# deterministic, and the fleet reading the titles imply. Asserted so it is not "simplified"
# back to an argMax that silently picks one host.
for t in "Top processes by CPU usage" "Top processes by memory usage" \
         "Top mountpoints by disk usage"; do
  if q "sqltemplate:$D_HOST:$t" | grep -q 'avgIf(Value, TimeUnix ='; then
    ok "$t: last column is deterministic (avg at the newest scrape)"
  else
    bad "$t: last column is not deterministic" \
        "argMax picks one arbitrary member of the collapsed dimension"
  fi
done

# Every gauge chart on Host overview must stay SQL: HyperDX collapses a gauge to its last
# sample per bucket before aggFn, so average()/max() over a gauge is not a builder shape.
for t in "CPU usage over time" "System load" "Memory usage over time" \
         "Top processes by CPU usage" "Top mountpoints by disk usage"; do
  if [ -n "$(q "sqltemplate:$D_HOST:$t")" ]; then ok "$t is a SQL tile"
  else bad "$t should be a SQL tile" "a builder gauge tile cannot aggregate within a bucket"; fi
done
for t in "Rate of disk IO" "Network traffic (bytes)" "Network traffic (packets)"; do
  if q "sqltemplate:$D_HOST:$t" | grep -q 'interval_s'; then ok "$t normalises per second"
  else bad "$t must divide by \$__interval_s" "Kibana uses counter_rate()"; fi
done

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m%d checks passed.\033[0m The system migration reproduced cleanly.\n' "$pass"
  echo "Dashboards: http://localhost:8080/dashboards"
  exit 0
else
  printf '\033[31m%d passed, %d FAILED.\033[0m\n' "$pass" "$fail"
  exit 1
fi
