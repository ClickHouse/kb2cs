#!/usr/bin/env bash
# Load the dataset into the hand-modelled ClickHouse tables from schema.sql.
#
#   ./ingest/clickhouse/load.sh                       # against localhost:9000
#   CH="clickhouse-client --host ch.example.com" ./ingest/clickhouse/load.sh
#
# Works with either a local `clickhouse-client` or the docker image:
#   CH="docker run --rm -i --network host clickhouse/clickhouse-server clickhouse-client" \
#     ./ingest/clickhouse/load.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${DATA:-$HERE/../../data}"
CH="${CH:-clickhouse-client}"

echo "==> creating schema"
$CH --multiquery < "$HERE/schema.sql"

# ---------------------------------------------------------------------------------------
# access.json.log -> nginx_access
#
# JSONEachRow maps keys to columns by name. Two fields need converting rather than copying:
#   msec           -> timestamp   (epoch seconds with ms, as a string)
#   status et al.  -> already numeric in the JSON, so they land directly
# We therefore read through the `input()` table function and transform in the SELECT.
# ---------------------------------------------------------------------------------------
echo "==> loading access.json.log ($(wc -l < "$DATA/access.json.log" | tr -d ' ') rows)"
$CH --query "
INSERT INTO nginx_training.nginx_access
    (timestamp, hostname, remote_addr, remote_user, http_x_forwarded_for,
     request_method, request_uri, server_protocol, scheme, host, server_name,
     status, body_bytes_sent, bytes_sent, request_length, request_time,
     upstream_addr, upstream_status, upstream_response_time, upstream_connect_time,
     upstream_header_time, http_referer, http_user_agent, request_id,
     ssl_protocol, ssl_cipher, gzip_ratio, connection, connection_requests)
SELECT
    toDateTime64(toFloat64(msec), 3, 'UTC') AS timestamp,
    hostname, remote_addr, remote_user, http_x_forwarded_for,
    request_method, request_uri, server_protocol, scheme, host, server_name,
    status, body_bytes_sent, bytes_sent, request_length, request_time,
    upstream_addr, upstream_status, upstream_response_time, upstream_connect_time,
    upstream_header_time, http_referer, http_user_agent, request_id,
    ssl_protocol, ssl_cipher, gzip_ratio, connection, connection_requests
FROM input('
    time_iso8601 String, msec String, hostname LowCardinality(String),
    remote_addr String, remote_user String, http_x_forwarded_for String,
    request_method LowCardinality(String), request_uri String,
    server_protocol LowCardinality(String), scheme LowCardinality(String),
    host String, server_name String, status UInt16,
    body_bytes_sent UInt64, bytes_sent UInt64, request_length UInt32, request_time Float32,
    upstream_addr String, upstream_status String, upstream_response_time String,
    upstream_connect_time String, upstream_header_time String,
    http_referer String, http_user_agent String, request_id String,
    ssl_protocol String, ssl_cipher String, gzip_ratio String,
    connection UInt64, connection_requests UInt32
') FORMAT JSONEachRow" < "$DATA/access.json.log"

# ---------------------------------------------------------------------------------------
# error.log -> nginx_error
#
# Two things worth copying if you write your own version of this:
#
#  1. format_regexp goes on the COMMAND LINE, not in a SETTINGS clause. `INSERT ... FORMAT
#     Regexp SETTINGS ...` fails with NOT_IMPLEMENTED, because FORMAT must be the last
#     clause and anything after it is read as inline data.
#  2. Do NOT try to capture the *<connection> prefix with an optional group. Some lines
#     have no connection at all and begin with a number anyway ("4096 worker_connections
#     are not enough") -- an optional \*?(\d*) group silently eats that 4096 and truncates
#     the message. Capture the remainder whole, then split it in SQL.
# ---------------------------------------------------------------------------------------
echo "==> loading error.log ($(wc -l < "$DATA/error.log" | tr -d ' ') rows)"
$CH --query "
INSERT INTO nginx_training.nginx_error (timestamp, level, pid, tid, connection, message)
SELECT
    parseDateTimeBestEffortOrNull(replaceAll(ts, '/', '-'), 'UTC') AS timestamp,
    level, pid, tid,
    toUInt64OrZero(extract(rest, '^\\\\*(\\\\d+) ')) AS connection,
    replaceRegexpOne(rest, '^\\\\*\\\\d+ ', '')      AS message
FROM input('ts String, level String, pid UInt32, tid UInt32, rest String')
FORMAT Regexp" \
    --format_regexp='^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (\d+)#(\d+): (.*)$' \
    --format_regexp_escaping_rule='Raw' \
    --format_regexp_skip_unmatched=0 \
    < "$DATA/error.log"

# ---------------------------------------------------------------------------------------
# access.log (combined) -> a separate table, so you can compare parse cost and storage
# against the JSON path. Parsed entirely in SQL: no grok, no ingest pipeline, no restart.
# ---------------------------------------------------------------------------------------
echo "==> loading access.log (combined) into nginx_access_combined"
$CH --multiquery --query "
CREATE TABLE IF NOT EXISTS nginx_training.nginx_access_combined
(
    timestamp       DateTime('UTC') CODEC(Delta(4), ZSTD(1)),
    remote_addr     String CODEC(ZSTD(1)),
    remote_user     LowCardinality(String),
    request_method  LowCardinality(String),
    request_uri     String CODEC(ZSTD(3)),
    server_protocol LowCardinality(String),
    status          UInt16 CODEC(T64, ZSTD(1)),
    body_bytes_sent UInt64 CODEC(T64, ZSTD(1)),
    http_referer    String CODEC(ZSTD(3)),
    http_user_agent String CODEC(ZSTD(3)),
    url_path        String MATERIALIZED splitByChar('?', request_uri)[1],
    status_class    LowCardinality(String) MATERIALIZED concat(substring(toString(status), 1, 1), 'xx')
)
ENGINE = MergeTree PARTITION BY toDate(timestamp) ORDER BY (status, url_path, timestamp);
"

# parseDateTime('%d/%b/%Y:%H:%M:%S %z') does NOT work here -- ClickHouse's %z rejects
# nginx's "+0000". Swapping the date/time separator for a space and letting
# parseDateTimeBestEffort do the work is the reliable route.
$CH --query "
INSERT INTO nginx_training.nginx_access_combined
    (timestamp, remote_addr, remote_user, request_method, request_uri, server_protocol,
     status, body_bytes_sent, http_referer, http_user_agent)
SELECT
    parseDateTimeBestEffortOrNull(replaceOne(time_local, ':', ' '), 'UTC') AS timestamp,
    remote_addr, remote_user, request_method, request_uri, server_protocol,
    status, body_bytes_sent, http_referer, http_user_agent
FROM input('
    remote_addr String, remote_user String, time_local String,
    request_method LowCardinality(String), request_uri String,
    server_protocol LowCardinality(String), status UInt16, body_bytes_sent UInt64,
    http_referer String, http_user_agent String
')
FORMAT Regexp" \
    --format_regexp='^(\S+) (\S+) \S+ \[([^\]]+)\] "(\S+) (.*) (HTTP/[\d.]+)" (\d{3}) (\d+) "(.*)" "(.*)"$' \
    --format_regexp_escaping_rule='Raw' \
    --format_regexp_skip_unmatched=0 \
    < "$DATA/access.log"

echo
echo "==> loaded:"
$CH --query "
SELECT
    table,
    formatReadableQuantity(sum(rows))            AS rows,
    formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
    formatReadableSize(sum(data_compressed_bytes))   AS compressed,
    round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 2) AS ratio
FROM system.parts
WHERE database = 'nginx_training' AND active
GROUP BY table ORDER BY table
FORMAT PrettyCompactMonoBlock"
