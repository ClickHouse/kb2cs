#!/usr/bin/env bash
# Load DB-IP City Lite into a ClickHouse ip_trie dictionary and geo-enrich otel_logs.
# Idempotent; takes a few minutes the first time (87 MB download + a backfill mutation).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "[geoip] loading DB-IP City Lite and building the dictionary (a few minutes)..."
docker compose exec -T clickstack clickhouse-client --multiquery < geoip.sql

echo "[geoip] waiting for the backfill mutation to finish"
until [ "$(docker compose exec -T clickstack clickhouse-client --query \
      "SELECT count() FROM system.mutations WHERE table='otel_logs' AND NOT is_done" </dev/null | tr -d '[:space:]')" = "0" ]; do
  sleep 5
done

docker compose exec -T clickstack clickhouse-client --query "
SELECT
    formatReadableQuantity((SELECT count() FROM geo.dbip_city))                    AS cidr_blocks,
    formatReadableQuantity(countIf(geo_country_code != ''))                        AS rows_located,
    concat(toString(round(100. * countIf(geo_country_code != '') / count(), 1)),'%') AS coverage
FROM default.otel_logs FORMAT Vertical" </dev/null
echo "[geoip] done"
