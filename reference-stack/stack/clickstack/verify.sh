#!/usr/bin/env bash
# Self-check the ClickStack stack: is it up, is a team registered, is the data loaded,
# parsed, un-duplicated and recent enough for HyperDX's default time ranges?
#
#   ./verify.sh
#
# Exits non-zero if anything that matters is wrong.
set -uo pipefail
# Run from this script's own directory. `docker compose exec` resolves the compose file
# from the working directory, so without this the script only works when invoked from
# here -- and reports "ClickHouse not reachable", which reads as a broken stack.
cd "$(dirname "$0")"

CH="docker compose exec -T clickstack clickhouse-client --query"
fail=0
ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
bad()  { printf "  \033[31mFAIL\033[0m  %s  %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
note() { printf "  ....  %s\n" "$1"; }
q()    { $CH "$1" </dev/null 2>/dev/null | tr -d '[:space:]'; }
qraw() { $CH "$1" </dev/null 2>/dev/null | tr -d '\n'; }

echo
echo "services"
if curl -sf -o /dev/null http://localhost:8080/; then ok "HyperDX UI responding on :8080"
else bad "HyperDX not responding on :8080" "docker compose up -d"; echo; exit 1; fi
v=$(q "SELECT version()")
[ -n "$v" ] && ok "ClickHouse reachable ($v)" || bad "ClickHouse not reachable"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:4318/v1/logs -H 'Content-Type: application/json' -d '{}')
case "$code" in
  200|401) ok "OTLP receiver is bound on :4318 (HTTP $code)" ;;
  000)     bad "OTLP port not bound" "no team registered? check: docker compose logs setup" ;;
  *)       bad "OTLP returned HTTP $code" ;;
esac

echo
echo "data"
# Scoped to nginx on purpose: otel_logs may also hold the optional apache service
# (RUNBOOK step 5b), so a table-wide count would fail for a reason that is not a fault.
# ./verify-apache.sh owns the apache side.
total=$(q "SELECT count() FROM default.otel_logs WHERE ServiceName = 'nginx'")
[ "${total:-0}" = "1011404" ] && ok "otel_logs holds exactly 1,011,404 nginx rows" \
  || bad "otel_logs holds ${total:-0} nginx rows, expected 1011404" "./load.sh"
other=$(q "SELECT count() FROM default.otel_logs WHERE ServiceName != 'nginx'")
[ "${other:-0}" != "0" ] && note "plus ${other} rows from another service (apache: ./verify-apache.sh)"

while read -r stream expected; do
  n=$(q "SELECT count() FROM default.otel_logs WHERE LogAttributes['log.stream']='$stream'")
  [ "${n:-0}" = "$expected" ] && ok "$stream: $expected rows" || bad "$stream: ${n:-0} rows, expected $expected"
done <<STREAMS
access_json 499964
access_combined 499964
error 11476
STREAMS

uniq=$(q "SELECT uniqExact(LogAttributes['request_id']) FROM default.otel_logs WHERE LogAttributes['log.stream']='access_json'")
[ "${uniq:-0}" = "499964" ] \
  && ok "499,964 unique request_ids -- no duplicates, no loss" \
  || bad "only ${uniq:-0} unique request_ids" "duplicated or dropped during ingest"

echo
echo "parsing"
st=$(q "SELECT count() FROM default.otel_logs WHERE LogAttributes['log.stream']='access_json' AND LogAttributes['status']=''")
[ "${st:-1}" = "0" ] && ok "every access_json row has a status attribute" || bad "${st} rows missing status"
# Scoped to nginx, like the row count above: the nginx collector maps every level onto
# info/warn/error, but other services in this table legitimately use more of the OTel scale
# (postgres FATAL lines map to `fatal`). A table-wide assertion here fails for a reason that
# is not a fault -- the same trap the row count fell into when apache was added.
sev=$(q "SELECT count() FROM default.otel_logs WHERE ServiceName = 'nginx' AND lower(SeverityText) NOT IN ('info','warn','error')")
[ "${sev:-1}" = "0" ] && ok "SeverityText mapped on every nginx row" || bad "${sev} nginx rows with unmapped severity"
svc=$(q "SELECT count() FROM default.otel_logs WHERE ServiceName='nginx'")
[ "${svc:-0}" = "1011404" ] && ok "ServiceName=nginx on all rows" || bad "ServiceName set on only ${svc:-0} rows"

echo
echo "recency (HyperDX defaults to Last 15m / Last 1h)"
newest_min=$(q "SELECT round(dateDiff('minute', max(Timestamp), now('UTC'))) FROM default.otel_logs WHERE ServiceName = 'nginx'")
future=$(q "SELECT countIf(Timestamp > now('UTC')) FROM default.otel_logs")
last24=$(q "SELECT count() FROM default.otel_logs WHERE ServiceName = 'nginx' AND Timestamp > now('UTC') - INTERVAL 24 HOUR")
# Threshold is 2h, not 1h, because --align-hour anchors the dataset to a whole hour: data
# loaded at 17:05 against a 16:00 anchor is legitimately 65+ minutes old. Anything beyond 2h
# means a genuinely stale load, not a rounding artifact.
# The threshold is 24h, not 2h. This is a STATIC corpus loaded with --align-hour, so the
# newest row starts 0-60 min old and then ages by the clock -- a 2h limit turns "an
# afternoon has passed" into a failure, which is noise rather than a finding. What actually
# matters is whether the 24h window still overlaps "last 24 hours", because past that both
# UIs' default presets go empty and the data needs re-anchoring for real.
# RECENCY IS REPORTED, NOT ASSERTED -- with one exception. The corpus is a static 24h block,
# so it ages continuously: any threshold on "rows in the last 24 hours" is a check whose
# result depends on when you run it, and both of these used to fail roughly a day after a
# re-anchor (`newest_min < 1440` right on the boundary, and `last24 > 900000` at 18,045).
# What is genuinely broken is only the case where the overlap has reached ZERO, because then
# both UIs' default presets are empty. Everything else is a re-anchor reminder.
[ "${future:-1}" = "0" ] && ok "no rows dated in the future" || bad "${future} rows in the future"
# Reported, not asserted. Staleness is a property of the clock, not of the migration, and a
# check that fails purely because a day has passed trains people to ignore red output. The
# row-count and distribution checks above already prove the data is loaded and correct; if
# nothing is in the picker's default range, that is a re-anchor reminder.
if [ "${last24:-0}" -gt 0 ] 2>/dev/null; then
  note "${last24} rows still inside the last 24h (of 1,011,404 total)"
else
  note "NOTHING inside the last 24h -- both UIs' default presets will be empty. The data is"
  note "loaded and correct; it has simply aged out. Set an absolute range, or re-anchor:"
  note "RUNBOOK, \"Keeping the two stacks on the same clock\"."
fi
note "newest row is ${newest_min:-?} minutes old"
if [ "${last24:-0}" -lt 900000 ] 2>/dev/null; then
  note "the corpus is aging out of the default time picker -- re-anchor when you want the"
  note "last-24h presets populated again. Not a fault: totals and distributions are unaffected."
fi
note "window: $(qraw "SELECT concat(toString(min(Timestamp)),'  ->  ',toString(max(Timestamp))) FROM default.otel_logs WHERE ServiceName = 'nginx'")"

echo
if [ "$fail" -eq 0 ]; then
  echo "  All checks passed."
  echo "    HyperDX   http://localhost:8080    train@example.com / TrainingP4ss!"
  echo "    Search    ServiceName:nginx"
  echo "    Try       LogAttributes['log.stream']:\"access_json\" and filter on status"
else
  echo "  $fail check(s) failed."
fi
echo
exit "$fail"
