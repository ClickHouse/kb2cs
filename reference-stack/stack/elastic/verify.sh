#!/usr/bin/env bash
# Self-check the stack: is it up, is the integration installed, is the data loaded and
# parsed, and would the dashboard panels actually render?
#
#   ./verify.sh
#
# Exits non-zero if anything that matters is wrong.
set -uo pipefail

ES="${ES:-http://localhost:9200}"
KB="${KB:-http://localhost:5601}"
AUTH="${AUTH:-elastic:changeme}"
J=(-s -u "$AUTH" -H 'Content-Type: application/json')
K=(-s -u "$AUTH" -H 'kbn-xsrf: true')

fail=0
ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
bad()  { printf "  \033[31mFAIL\033[0m  %s  %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
note() { printf "  ....  %s\n" "$1"; }

jqf() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

echo
echo "services"
curl -sf "$ES" -u "$AUTH" >/dev/null 2>&1 \
  && ok "elasticsearch reachable ($(curl "${J[@]}" "$ES" | jqf "d['version']['number']"))" \
  || { bad "elasticsearch unreachable at $ES"; echo; exit 1; }
lvl=$(curl -s "$KB/api/status" | jqf "d['status']['overall']['level']")
[ "$lvl" = "available" ] && ok "kibana available" || bad "kibana not available" "(status: ${lvl:-no response})"

echo
echo "integration"
pipes=$(curl "${J[@]}" "$ES/_ingest/pipeline/logs-nginx*" | jqf "len(d)")
[ "${pipes:-0}" -ge 2 ] && ok "nginx ingest pipelines installed ($pipes)" || bad "nginx pipelines missing" "found ${pipes:-0}"
dash=$(curl "${K[@]}" "$KB/api/saved_objects/_find?type=dashboard&search=nginx&search_fields=title" | jqf "d['total']")
[ "${dash:-0}" -ge 3 ] && ok "nginx dashboards installed ($dash)" || bad "dashboards missing" "found ${dash:-0}"

echo
echo "data"
for pair in "nginx.access 499964" "nginx.error 11476"; do
  set -- $pair
  n=$(curl "${J[@]}" "$ES/logs-*/_count" -d "{\"query\":{\"term\":{\"data_stream.dataset\":\"$1\"}}}" | jqf "d['count']")
  if [ "${n:-0}" = "$2" ]; then ok "$1 has exactly $2 docs"
  elif [ "${n:-0}" = "0" ]; then bad "$1 has no docs" "run: docker compose run --rm load"
  else bad "$1 has ${n:-0} docs, expected $2" "reload: docker compose run --rm load"; fi
done

badparse=$(curl "${J[@]}" "$ES/logs-nginx.*/_count" \
  -d '{"query":{"bool":{"should":[{"exists":{"field":"error.message"}},{"term":{"tags":"_grokparsefailure"}}]}}}' | jqf "d['count']")
[ "${badparse:-1}" = "0" ] && ok "no grok/pipeline failures" || bad "$badparse documents failed to parse"

echo
echo "would the dashboards render?"
# NOTE the absence of a `now-24h` range here. These four checks ask "is the field populated
# at all", which is a property of the CORPUS, not of the wall clock -- and querying the last
# 24 hours made all four go red the moment the dataset aged past a day. The overlap with the
# default time picker is reported separately below.
read -r hits buckets geo ua st <<<"$(curl "${J[@]}" "$ES/logs-*/_search?size=0" -d '{
 "query":{"bool":{"filter":[{"term":{"data_stream.dataset":"nginx.access"}}]}},
 "track_total_hits":true,
 "aggs":{"t":{"date_histogram":{"field":"@timestamp","fixed_interval":"1h"}},
         "g":{"terms":{"field":"source.geo.country_iso_code","size":1}},
         "u":{"terms":{"field":"user_agent.name","size":1}},
         "s":{"terms":{"field":"http.response.status_code","size":1}}}}' \
 | jqf "'%s %s %s %s %s' % (d['hits']['total']['value'], sum(1 for b in d['aggregations']['t']['buckets'] if b['doc_count']), len(d['aggregations']['g']['buckets']), len(d['aggregations']['u']['buckets']), len(d['aggregations']['s']['buckets']))")"

[ "${hits:-0}" -gt 0 ] && ok "$hits access docs in the corpus" \
  || bad "no access docs at all" "the load did not land -- docker compose run --rm load"
last24=$(curl "${J[@]}" "$ES/logs-*/_count" -d '{"query":{"bool":{"filter":[{"term":{"data_stream.dataset":"nginx.access"}},{"range":{"@timestamp":{"gte":"now-24h"}}}]}}}' | jqf "d['count']")
note "${last24:-0} of those fall in Kibana's default last-24h window"
# CONTINUITY is asserted over the data's OWN span, not over a wall-clock window. This check
# used to require >= 20 non-empty hourly buckets inside Kibana's default last-24h range, and
# that decays: the corpus ends on a fixed instant, so the overlap shrinks by one bucket per
# hour of elapsed real time and the check failed ~5 h after a re-anchor. It was the repo's own
# verifier breaking the rule the migration docs state -- never assert on a relative window.
allb=$(curl "${J[@]}" "$ES/logs-*/_search?size=0" -d '{
 "query":{"bool":{"filter":[{"term":{"data_stream.dataset":"nginx.access"}}]}},
 "aggs":{"t":{"date_histogram":{"field":"@timestamp","fixed_interval":"1h"}}}}' \
 | jqf "sum(1 for b in d['aggregations']['t']['buckets'] if b['doc_count'])")
[ "${allb:-0}" -ge 24 ] && ok "$allb non-empty hourly buckets across the corpus (time-series panels)" \
  || bad "only ${allb:-0} hourly buckets across the corpus" "the 24h curve has gaps"
# ...and the last-24h figure is REPORTED, because it legitimately shrinks as the data ages.
if [ "${buckets:-0}" -ge 20 ]; then
  note "${buckets} of those fall in Kibana's default last-24h window"
else
  note "only ${buckets:-0} of those fall in Kibana's default last-24h window -- the corpus is"
  note "aging out of the default time picker. Not a fault; re-anchor if you want it populated:"
  note "RUNBOOK.md, \"Keeping the two stacks on the same clock\"."
fi
[ "${geo:-0}" -gt 0 ]  && ok "source.geo populated (the map panel)"        || bad "no geo data -- map panel will be blank"
[ "${ua:-0}" -gt 0 ]   && ok "user_agent populated (browser/OS panels)"    || bad "no user_agent data"
[ "${st:-0}" -gt 0 ]   && ok "http.response.status_code populated"         || bad "no status codes"

err=$(curl "${J[@]}" "$ES/logs-*/_count" -d '{"query":{"bool":{"filter":[{"term":{"data_stream.dataset":"nginx.error"}}]}}}' | jqf "d['count']")
[ "${err:-0}" -gt 0 ] && ok "$err error docs in the corpus (error panels)" || bad "no error docs at all"

newest=$(curl "${J[@]}" "$ES/logs-nginx.access-default/_search?size=0" -d '{"aggs":{"m":{"max":{"field":"@timestamp"}}}}' | jqf "d['aggregations']['m']['value_as_string']")
note "newest document: ${newest:-unknown}"

echo
if [ "$fail" -eq 0 ]; then
  echo "  All checks passed. Open these with the time range preset to Last 24 hours:"
  for d in "Logs Nginx Overview:nginx-55a9e6e0-a29e-11e7-928f-5dbe6f6f5519" \
           "Access and error logs:nginx-046212a0-a2a1-11e7-928f-5dbe6f6f5519"; do
    echo "    ${d%%:*}"
    echo "      $KB/app/dashboards#/view/${d##*:}?_g=(time:(from:now-24h,to:now))"
  done
else
  echo "  $fail check(s) failed."
fi
echo
exit "$fail"
