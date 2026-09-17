#!/usr/bin/env bash
# Parse user agents into ua_browser / ua_os / ua_device columns on otel_logs, using the SAME
# uap-core regex corpus Elasticsearch itself runs. The counterpart to ./geoip.sh.
#
#   ./ua.sh
#
# Idempotent. Takes about a minute (a backfill mutation over 1M rows).
#
# The corpus is extracted from the running Elasticsearch container's ingest-user-agent jar,
# because uap-core is versioned and different releases classify some agents differently.
# Using the other stack's own copy is what makes the two agree by construction. If Elastic is
# not running, set UA_REGEXES to a regexes.yml you supply.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ES_CONTAINER="${ES_CONTAINER:-nginx-training-es}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { echo "[ua] $*"; }

# ---------------------------------------------------------------- 1. get the corpus
if [ -n "${UA_REGEXES:-}" ]; then
  [ -r "$UA_REGEXES" ] || { say "cannot read UA_REGEXES=$UA_REGEXES"; exit 1; }
  cp "$UA_REGEXES" "$WORK/regexes.yml"
  say "using corpus from $UA_REGEXES"
elif docker ps --format '{{.Names}}' | grep -qx "$ES_CONTAINER"; then
  say "extracting uap-core regexes from $ES_CONTAINER (Elastic's own copy)"
  docker exec "$ES_CONTAINER" sh -c '
    set -e
    JAR=$(find /usr/share/elasticsearch/modules/ingest-user-agent -name "*.jar" | head -1)
    D=$(mktemp -d); cd "$D"; cp "$JAR" .; unzip -qo "$(basename "$JAR")"
    cat regexes.yml' > "$WORK/regexes.yml"
  say "got $(wc -c < "$WORK/regexes.yml" | tr -d " ") bytes"
else
  say "Elasticsearch container '$ES_CONTAINER' is not running, and UA_REGEXES is unset."
  say "Either start ../elastic, or point UA_REGEXES at a uap-core regexes.yml:"
  say "  UA_REGEXES=/path/to/regexes.yml ./ua.sh"
  exit 1
fi

# ---------------------------------------------------------------- 2. convert
say "converting to ClickHouse regexp_tree format"
mkdir -p "$WORK/out"
python3 ../../ingest/clickstack/ua/convert-regexes.py "$WORK/regexes.yml" "$WORK/out" \
  | sed 's/^/[ua]   /'

# ---------------------------------------------------------------- 3. install + build
# user_files is the only path a YAMLRegExpTree source may read from.
say "installing the dictionaries"
docker compose exec -T clickstack sh -c 'mkdir -p /var/lib/clickhouse/user_files/ua' </dev/null
for f in ua-browser.yaml ua-os.yaml ua-device.yaml; do
  docker cp "$WORK/out/$f" "$(docker compose ps -q clickstack):/var/lib/clickhouse/user_files/ua/$f" >/dev/null
done

docker compose exec -T clickstack clickhouse-client --multiquery < ua.sql

say "waiting for the backfill mutation to finish"
until [ "$(docker compose exec -T clickstack clickhouse-client --query \
      "SELECT count() FROM system.mutations WHERE table='otel_logs' AND NOT is_done" </dev/null | tr -d '[:space:]')" = "0" ]; do
  sleep 5
done

# ---------------------------------------------------------------- 4. report
# Reported per service: the two corpora share a UA pool by construction (see
# generator/generate-apache.py), so a coverage gap between them means a parsing problem on
# one side, not a data difference.
docker compose exec -T clickstack clickhouse-client --query "
SELECT
    ServiceName                                                                       AS service,
    concat(toString(round(100. * countIf(ua_browser != 'Other') / count(), 1)),'%')   AS browser,
    concat(toString(round(100. * countIf(ua_browser_version != '') / count(), 1)),'%') AS browser_ver,
    concat(toString(round(100. * countIf(ua_os != 'Other') / count(), 1)),'%')         AS os,
    concat(toString(round(100. * countIf(ua_os_version != '') / count(), 1)),'%')      AS os_ver
FROM default.otel_logs
WHERE LogAttributes['log.stream'] IN ('access_json', 'apache_access')
GROUP BY service ORDER BY service FORMAT PrettyCompact" </dev/null
say "browser patterns: $(docker compose exec -T clickstack clickhouse-client --query \
  'SELECT count() FROM ua.browser' </dev/null | tr -d '[:space:]')"
say "done"
