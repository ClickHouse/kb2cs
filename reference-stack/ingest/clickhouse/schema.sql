-- Purpose-built ClickHouse schema for the nginx dataset.
--
-- This is the "what it looks like when you stop pretending logs are documents" side of the
-- migration. Two tables:
--
--   nginx_access  -- the JSON access log, typed and column-oriented
--   nginx_error   -- the error log, with the cause extracted at query time
--
-- Note this is NOT ClickStack's own `otel_logs` table. Ingesting through the OTel collector
-- (see ../clickstack/otel-collector-nginx.yaml) puts everything in `otel_logs` with the
-- fields as Map entries, which is what HyperDX queries. These tables are the hand-modelled
-- alternative -- useful precisely because comparing the two is the interesting part.

CREATE DATABASE IF NOT EXISTS nginx_training;

DROP TABLE IF EXISTS nginx_training.nginx_access;

CREATE TABLE nginx_training.nginx_access
(
    -- $msec, parsed. DateTime64(3) keeps the millisecond precision that the combined
    -- format throws away.
    timestamp              DateTime64(3, 'UTC') CODEC(Delta(8), ZSTD(1)),

    hostname               LowCardinality(String),
    remote_addr            String CODEC(ZSTD(1)),
    remote_user            LowCardinality(String),
    http_x_forwarded_for   String CODEC(ZSTD(1)),

    request_method         LowCardinality(String),
    request_uri            String CODEC(ZSTD(3)),
    server_protocol        LowCardinality(String),
    scheme                 LowCardinality(String),
    host                   LowCardinality(String),
    server_name            LowCardinality(String),

    status                 UInt16 CODEC(T64, ZSTD(1)),
    body_bytes_sent        UInt64 CODEC(T64, ZSTD(1)),
    bytes_sent             UInt64 CODEC(T64, ZSTD(1)),
    request_length         UInt32 CODEC(T64, ZSTD(1)),
    request_time           Float32 CODEC(Gorilla, ZSTD(1)),

    -- nginx writes '-' when it served the request itself, and a comma-joined list when it
    -- retried another upstream ("10.0.2.11:8080, 10.0.2.13:8080"). So these stay strings,
    -- with numeric views alongside. This is the single most common surprise when moving
    -- nginx logs into a typed store.
    upstream_addr          String CODEC(ZSTD(1)),
    upstream_status        String CODEC(ZSTD(1)),
    upstream_response_time String CODEC(ZSTD(1)),
    upstream_connect_time  String CODEC(ZSTD(1)),
    upstream_header_time   String CODEC(ZSTD(1)),

    http_referer           String CODEC(ZSTD(3)),
    http_user_agent        String CODEC(ZSTD(3)),
    request_id             String CODEC(ZSTD(1)),
    ssl_protocol           LowCardinality(String),
    ssl_cipher             LowCardinality(String),
    gzip_ratio             String CODEC(ZSTD(1)),
    connection             UInt64 CODEC(Delta(8), ZSTD(1)),
    connection_requests    UInt32 CODEC(T64, ZSTD(1)),

    -- ---- derived columns: computed on insert, stored, free at query time -------------
    -- The ES equivalent of each of these is either a pipeline processor (fixed at index
    -- time) or a runtime script field (slow). Here they are just columns.
    url_path       String  MATERIALIZED splitByChar('?', request_uri)[1],
    url_query      String  MATERIALIZED if(position(request_uri, '?') > 0,
                                          substring(request_uri, position(request_uri, '?') + 1), ''),
    status_class   LowCardinality(String) MATERIALIZED concat(substring(toString(status), 1, 1), 'xx'),
    is_error       UInt8   MATERIALIZED status >= 500,

    -- single-upstream latency; NULL when nginx served locally or retried a second peer
    upstream_rt    Nullable(Float32) MATERIALIZED toFloat32OrNull(upstream_response_time),
    -- time nginx itself added on top of the upstream
    nginx_overhead Nullable(Float32) MATERIALIZED request_time - toFloat32OrNull(upstream_response_time),

    gzip_ratio_num Nullable(Float32) MATERIALIZED toFloat32OrNull(gzip_ratio),
    is_bot         UInt8   MATERIALIZED multiSearchAnyCaseInsensitive(
                               http_user_agent,
                               ['bot', 'crawler', 'spider', 'curl/', 'python-requests',
                                'python-urllib', 'go-http-client', 'zgrab', 'masscan',
                                'kube-probe', 'prometheus', 'healthchecker']),
    client_ip      Nullable(IPv6) MATERIALIZED toIPv6OrNull(remote_addr)
)
ENGINE = MergeTree
PARTITION BY toDate(timestamp)
-- status first so the "show me the errors" queries skip almost every granule; then the
-- path, because grouping by endpoint is the other thing everyone does.
ORDER BY (status, url_path, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- A skip index for the free-text-ish lookups (a single client IP, a single request id),
-- which the primary key does not help with.
ALTER TABLE nginx_training.nginx_access
    ADD INDEX IF NOT EXISTS idx_remote_addr remote_addr TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE nginx_training.nginx_access
    ADD INDEX IF NOT EXISTS idx_request_id request_id TYPE bloom_filter(0.01) GRANULARITY 1;
ALTER TABLE nginx_training.nginx_access
    ADD INDEX IF NOT EXISTS idx_connection connection TYPE minmax GRANULARITY 4;


DROP TABLE IF EXISTS nginx_training.nginx_error;

CREATE TABLE nginx_training.nginx_error
(
    timestamp   DateTime('UTC') CODEC(Delta(4), ZSTD(1)),
    level       LowCardinality(String),
    pid         UInt32 CODEC(T64, ZSTD(1)),
    tid         UInt32 CODEC(T64, ZSTD(1)),
    connection  UInt64 CODEC(Delta(8), ZSTD(1)),   -- 0 when the line has no *<conn>
    message     String CODEC(ZSTD(3)),

    -- extracted from the message, since nginx's error log has no structure to speak of
    remote_addr String MATERIALIZED extract(message, 'client: ([^,]+)'),
    request     String MATERIALIZED extract(message, 'request: "([^"]*)"'),
    upstream    String MATERIALIZED extract(message, 'upstream: "([^"]*)"'),
    cause       LowCardinality(String) MATERIALIZED multiIf(
                    position(message, 'upstream timed out') > 0,        'upstream_timeout',
                    position(message, 'Connection refused') > 0,        'upstream_refused',
                    position(message, 'no live upstreams') > 0,         'no_live_upstreams',
                    -- limit_req has two outcomes and they are NOT the same event:
                    -- "delaying request" [warn] = inside the burst, request still succeeded;
                    -- "limiting requests" [error] = past the burst, client got a 503.
                    position(message, 'delaying request') > 0,          'rate_limit_delayed',
                    position(message, 'limiting requests') > 0,         'rate_limited',
                    -- max_fails reached, peer pulled from the pool for fail_timeout
                    position(message, 'upstream server temporarily disabled') > 0, 'upstream_disabled',
                    position(message, 'SSL_do_handshake') > 0,          'tls_handshake',
                    position(message, 'while SSL handshaking') > 0,     'tls_handshake',
                    -- order matters: "upstream prematurely closed" is the upstream giving
                    -- up, "client prematurely closed" is the user hitting stop. A single
                    -- 'prematurely closed' test conflates two unrelated causes.
                    position(message, 'upstream prematurely closed') > 0, 'upstream_closed',
                    position(message, 'client prematurely closed') > 0,   'client_aborted',
                    position(message, 'client closed connection') > 0,    'client_aborted',
                    position(message, 'No such file or directory') > 0, 'missing_file',
                    position(message, 'buffered to a temporary file') > 0, 'response_buffered',
                    position(message, 'worker_connections') > 0,        'worker_saturation',
                    position(message, 'upstream sent too big header') > 0, 'upstream_bad_header',
                    position(message, 'Connection reset by peer') > 0,  'upstream_reset',
                    position(message, 'access forbidden by rule') > 0,  'forbidden_by_rule',
                    match(message, 'gracefully shutting down|reconfiguring|start worker|exiting|exit$|SIGCHLD|signal|event method'), 'lifecycle',
                    'other')
)
ENGINE = MergeTree
PARTITION BY toDate(timestamp)
ORDER BY (level, timestamp)
SETTINGS index_granularity = 8192;

ALTER TABLE nginx_training.nginx_error
    ADD INDEX IF NOT EXISTS idx_err_connection connection TYPE minmax GRANULARITY 4;
