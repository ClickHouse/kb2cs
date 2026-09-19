-- Geo-enrich otel_logs using the free DB-IP City Lite dataset and a ClickHouse ip_trie
-- dictionary, so the Kibana nginx map panel has something to migrate to.
--
-- Run with ./geoip.sh (which just pipes this through clickhouse-client).
--
-- Why a dictionary rather than a geoip processor in the collector: on the Elastic side the
-- enrichment happens at INDEX time, baked into every document and fixed forever. Here it is
-- a lookup against a table you can replace whenever DB-IP publishes a new month, and the
-- materialized columns below are recomputed by a mutation rather than a reindex. That
-- difference is one of the better arguments in the whole migration.

CREATE DATABASE IF NOT EXISTS geo;

-- ---------------------------------------------------------------------------------------
-- 0. Unwind last run's objects, innermost dependency first.
--
-- The chain is  geo.dbip_city (table) <- geo.dbip (dictionary) <- otel_logs.geo_* (columns),
-- and ClickHouse refuses to drop anything that still has dependents:
--   Code 630 ... Cannot drop or rename geo.dbip_city, because some tables depend on it:
--   geo.dbip (HAVE_DEPENDENT_OBJECTS)
-- So the columns go first, then the dictionary, and only then may the source table be
-- replaced further down. Without this block a second run of geoip.sh fails outright.
-- ---------------------------------------------------------------------------------------

ALTER TABLE default.otel_logs
    DROP COLUMN IF EXISTS geo_country_code,
    DROP COLUMN IF EXISTS geo_city,
    DROP COLUMN IF EXISTS geo_latitude,
    DROP COLUMN IF EXISTS geo_longitude;

DROP DICTIONARY IF EXISTS geo.dbip;

-- ---------------------------------------------------------------------------------------
-- 1. Load DB-IP City Lite.
--
-- The CSV gives inclusive ranges, but ip_trie wants CIDRs, so collapse each range onto the
-- smallest covering block. Ranges that are not CIDR-aligned end up over-covering, which is
-- harmless: ip_trie resolves by LONGEST PREFIX, so a more specific block always wins. In
-- the sample below, 1.0.1.0-1.0.3.255 becomes 1.0.0.0/22 (CN) but 1.0.0.0/24 (AU) still
-- takes precedence for its own addresses.
--
-- IPv4 only. The file also carries IPv6 ranges, but collapsing those needs 128-bit
-- arithmetic; ~4% of clients in this dataset are IPv6 and they simply get no geo. The
-- Elastic side geolocates them, so expect a small discrepancy -- see MIGRATION.md.
-- ---------------------------------------------------------------------------------------

DROP TABLE IF EXISTS geo.dbip_city;

CREATE TABLE geo.dbip_city
(
    cidr         String,
    country_code LowCardinality(String),
    state        String,
    city         String,
    latitude     Float64,
    longitude    Float64
)
ENGINE = MergeTree
ORDER BY cidr;

INSERT INTO geo.dbip_city
WITH
    toUInt32(toIPv4(ip_range_start))                        AS s,
    toUInt32(toIPv4(ip_range_end))                          AS e,
    bitXor(s, e)                                            AS x,
    toUInt8(if(x != 0, ceil(log2(x)), 0))                   AS unmatched,
    toUInt8(32 - unmatched)                                 AS suffix,
    toUInt32(bitAnd(s, bitNot(toUInt32(pow(2, unmatched)) - 1))) AS net
SELECT
    concat(IPv4NumToString(net), '/', toString(suffix)) AS cidr,
    country_code,
    state,
    city,
    latitude,
    longitude
FROM url(
    'https://download.db-ip.com/free/dbip-city-lite-2026-08.csv.gz',
    'CSV',
    'ip_range_start String, ip_range_end String, continent_code String,
     country_code String, state String, city String, latitude Float64, longitude Float64'
)
WHERE position(ip_range_start, ':') = 0     -- IPv4 rows only
SETTINGS max_execution_time = 900;

-- ---------------------------------------------------------------------------------------
-- 2. The dictionary.
--
-- Sourced from the local table, NOT from the URL, so it reloads without network access.
-- That matters: the materialized columns below call dictGet on every insert into
-- otel_logs, so a dictionary that cannot load would break ClickStack's ingest.
-- ---------------------------------------------------------------------------------------

DROP DICTIONARY IF EXISTS geo.dbip;

CREATE DICTIONARY geo.dbip
(
    cidr         String,
    country_code String,
    state        String,
    city         String,
    latitude     Float64,
    longitude    Float64
)
PRIMARY KEY cidr
SOURCE(CLICKHOUSE(TABLE 'dbip_city' DB 'geo'))
LAYOUT(IP_TRIE)
LIFETIME(3600);

-- ---------------------------------------------------------------------------------------
-- 3. Materialized columns on ClickStack's own table.
--
-- Additive and reversible (DROP COLUMN), and HyperDX ignores columns it does not know
-- about. dictGetOrDefault, not dictGet, so an IPv6 client or an unmatched range yields ''
-- instead of failing the insert.
-- ---------------------------------------------------------------------------------------

-- These were dropped in step 0, not here -- they have to go before the dictionary they
-- depend on can be replaced. Re-adding them updates the definitions rather than silently
-- keeping stale ones.

-- The isIPv4 guard is load-bearing. Without it, toIPv4OrDefault turns an IPv6 client into
-- 0.0.0.0, which matches DB-IP's own 0.0.0.0/8 block and comes back as country "ZZ" -- so
-- all 34,301 IPv6 clients here would silently render as a real-looking country on the map.
-- Guarded, they read as "no data", which is the truth.
ALTER TABLE default.otel_logs
    ADD COLUMN geo_country_code LowCardinality(String)
        MATERIALIZED if(position(LogAttributes['remote_addr'], ':') > 0, '',
                     dictGetOrDefault('geo.dbip', 'country_code',
                     toIPv4OrDefault(LogAttributes['remote_addr']), '')),
    ADD COLUMN geo_city String
        MATERIALIZED if(position(LogAttributes['remote_addr'], ':') > 0, '',
                     dictGetOrDefault('geo.dbip', 'city',
                     toIPv4OrDefault(LogAttributes['remote_addr']), '')),
    ADD COLUMN geo_latitude Float64
        MATERIALIZED if(position(LogAttributes['remote_addr'], ':') > 0, 0.,
                     dictGetOrDefault('geo.dbip', 'latitude',
                     toIPv4OrDefault(LogAttributes['remote_addr']), 0.)),
    ADD COLUMN geo_longitude Float64
        MATERIALIZED if(position(LogAttributes['remote_addr'], ':') > 0, 0.,
                     dictGetOrDefault('geo.dbip', 'longitude',
                     toIPv4OrDefault(LogAttributes['remote_addr']), 0.));

-- Backfill the rows that are already there. MATERIALIZED only computes on insert, so
-- without this the existing 1M rows read back as empty. This is a mutation: asynchronous,
-- and visible in system.mutations.
ALTER TABLE default.otel_logs
    MATERIALIZE COLUMN geo_country_code,
    MATERIALIZE COLUMN geo_city,
    MATERIALIZE COLUMN geo_latitude,
    MATERIALIZE COLUMN geo_longitude;
