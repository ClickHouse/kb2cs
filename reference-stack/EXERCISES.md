# Exercises

Answers below were produced by running the queries against the shipped files, loaded via
`ingest/clickhouse/load.sh` into ClickHouse 26.2. Numbers are exact for this dataset — if a
trainee's pipeline disagrees, their pipeline is wrong, which is the point.

Each exercise names the migration lesson it is really about. Do them in order; the later ones
assume the earlier ones landed.

---

## Part 1 — Parsing (get the data in correctly)

### 1.1 Parse the combined log, both ways

Ingest `data/access.log` on both platforms: grok on the Elasticsearch side, a
`regex_parser` operator (or `extract()` in SQL) on the ClickStack side.

**Verify:** you must end up with exactly **499,964** documents/rows, and zero parse failures.

**The catch, in two layers.** 1,249 requests contain a literal `"`, a `\`, or non-ASCII bytes
in the URI, which nginx escapes as `\x22`, `\x5C`, `\xC3\xA9`. Handling the escapes is the easy
layer.

The layer that actually breaks pipelines: **300 of those requests have a space *inside* the
URI** (scanners sending `/product?id=1\x22 OR \x221\x22=\x221`, across five HTTP methods). So
the obvious grok pattern

```
"%{WORD:method} %{NOTSPACE:url} HTTP/%{NUMBER:version}"
```

fails on all 300 — `NOTSPACE` cannot cross a space. Use a non-greedy `%{DATA:url}` anchored on
the literal ` HTTP/` that follows it. This is verified: with `DATA`, all 499,964 lines index
with zero grok failures; with `NOTSPACE`, 300 land in `on_failure`.

The same applies to the ClickStack collector's `regex_parser` and to any `extract()` you write
in SQL. Find them:

```sql
SELECT count() FROM nginx_access_combined WHERE request_uri LIKE '%\\x22%'
   OR request_uri LIKE '%\\x5C%' OR request_uri LIKE '%\\xC3%';   -- 1249
```

> *Lesson: escaping is a property of the log format, not of your regex. Read the nginx
> `escape=` docs before writing the parser.*

### 1.2 Get the timestamp right

Parse timestamps from both access logs.

**Answers:** the window is `2026-08-17 00:00:00.220` → `2026-08-17 23:59:59.913`.

**The catch:** `access.log` has **second** resolution; `access.json.log` has milliseconds in
`msec`. If you parse the JSON log via `time_iso8601` you silently throw the milliseconds
away — 787 requests share the peak minute `20:04`, and sub-second ordering matters when you
correlate with `error.log`.

Second catch: `parseDateTime(t, '%d/%b/%Y:%H:%M:%S %z')` **does not work** in ClickHouse —
`%z` rejects nginx's `+0000`. Use
`parseDateTimeBestEffortOrNull(replaceOne(time_local, ':', ' '), 'UTC')`. See
`ingest/clickhouse/load.sh`.

> *Lesson: timestamp precision is lost silently, not loudly.*

### 1.3 Type the `upstream_*` fields

Map `upstream_response_time` as a float on both platforms and ingest the JSON log.

**Answer:** it fails. `-` appears on **312,389** locally-served requests, and all **661** 502s
carry `"0.002, 0.002"` — two values, because `proxy_next_upstream` retried a second backend.

Now do it properly: store the string, expose a nullable numeric view.

```sql
upstream_rt Nullable(Float32) MATERIALIZED toFloat32OrNull(upstream_response_time)
```

**Verify:** `upstream_rt` is non-null on **186,914** rows.

> *Lesson: this is the field that breaks real nginx migrations. The strict-mapping instinct
> from Elasticsearch will bite you here too.*

### 1.4 Ship it, then check where it actually landed

Ingest all three files with Filebeat, using `%{+yyyy.MM.dd}` in the index name and
`layouts: [UNIX_MS, UNIX]` for the `nginx.msec` timestamp. Both are plausible-looking
choices. Then run `GET _cat/indices/nginx-training-*`.

**Answer:** you get two indices, and **neither name is right**:

```
nginx-training-2026.08.18     506082    <- combined + error, stamped with TODAY
nginx-training-1970.01.21     499964    <- the JSON stream, stamped with 1970
```

Two distinct failures, and **not one document errored**:

1. `UNIX_MS` read `msec` of `1786924820.070` as *milliseconds* since the epoch instead of
   seconds — 21 January 1970. Every document indexed successfully, into the wrong day. The
   correct layout is `UNIX`, which keeps the fractional milliseconds.
2. Filebeat resolves the index name from `@timestamp` **client-side, before the event is
   sent**. The combined and error streams get their `@timestamp` from a `date` processor in
   the *server-side* ingest pipeline, which has not run yet — so they fall back to ingest
   time.

Now find the equivalent class of bug on the ClickStack side. (The collector's
`layout_type: epoch` / `layout: s.ms` is the counterpart, and the OTel collector has no
index-naming step at all — the table is chosen by the exporter, not by the timestamp.)

> *Lesson: an ingest pipeline that reports zero errors has not told you the data is correct.
> Verify the timestamp range and the destination, every time. Both platforms will happily
> put your logs somewhere useless without complaining.*

---

## Part 2 — Querying (the same question on both platforms)

### 2.1 Traffic over time

Requests per minute for the day, as a chart.

**Answers:** diurnal, peaking **19:00–20:00** at **7.23×** the **03:00** trough. Hourly
counts run 6k · 5k · 4k · 4k · 6k · 9k · 13k · 19k · 22k · 26k · 29k · 30k · 30k · 29k · 28k ·
26k · 28k · 27k · 30k · 33k · 32k · 26k · 17k · 11k. Busiest single minute: **20:04, 787
requests**.

### 2.2 Latency percentiles per endpoint

p50/p95/p99 of `request_time`, grouped by path.

**Answers** (ms):

| path | requests | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| `/checkout/confirm` | 1,256 | 422 | 1,403 | 5,095 |
| `/checkout` | 2,319 | 233 | 884 | 3,515 |
| `/search` | 13,298 | 111 | 448 | 955 |
| `/cart` | 4,763 | 46 | 126 | 210 |
| `/` | 11,779 | 35 | 87 | 134 |
| `/health` | 17,280 | 1 | 1 | 1 |

**The catch:** those p99s include timeouts. Every one of the **184** 504s in the dataset sits
at `request_time` ≈ 60.0 — that is `proxy_read_timeout 60s` expiring, not a request that was
merely slow. Excluding them halves `/checkout`'s p99, from **3,515 ms to 1,742 ms**. Decide
deliberately whether a timeout belongs in your latency distribution; just don't do it by
accident.

Second catch: static-asset latency scales with size, from ~2 ms for a 41 KB font to ~11 ms
p50 / 47 ms p99 for the 512 KB vendor bundle. Averaging all of `/static/*` into one number
hides that.

**Third catch, and the interesting one — run this on both platforms and compare.** For
`/checkout` (2,319 requests, both platforms agree on the count):

| | p50 | p95 | p99 |
|---|---:|---:|---:|
| Exact (computed directly from the file) | 233 ms | 884 ms | **3,516 ms** |
| ClickHouse `quantile()` | 233 ms | 884 ms | **3,515 ms** |
| Elasticsearch `percentiles` | 237 ms | 902 ms | **4,210 ms** |

Neither platform is wrong and neither is lying to you: both aggregations are *approximate* by
design. Elasticsearch uses t-digest, which is coarse in the sparse tail and here overstates
p99 by **20%** — about 700 ms. ClickHouse's `quantile()` (reservoir sampling) lands within
1 ms at this cardinality; `quantileExact()` guarantees it at the cost of memory.

Discuss what a 20% p99 error means for an SLO dashboard, and how you would find out that your
p99 panel is approximate — because nothing in either UI tells you.

### 2.3 Where are the errors?

5xx rate by endpoint group and by upstream.

**Answers:** overall 5xx is **0.51%** (2,534 requests). By group:

| group | requests | 5xx % | 304 % |
|---|---:|---:|---:|
| static | 265,815 | 0.00 | 33.7 |
| api | 94,192 | 1.45 | 2.0 |
| product | 50,084 | 1.06 | 1.2 |
| health | 17,280 | 0.09 | 0.0 |
| **checkout** | 3,575 | **3.97** | 0.0 |

By upstream, the six `api` backends sit between **0.94% and 1.16%** — evenly spread, no
single bad node. That is the correct answer: this dataset has no planted incident. An
exercise that "finds" a guilty upstream here has found noise.

> *Lesson: establish what normal looks like before you go hunting. Most of observability
> practice is this.*

### 2.4 Separate nginx's cost from the backend's

`request_time − upstream_response_time` is the time nginx itself added.

**Answers:** p50 **2 ms**, p99 **5 ms**, across 186,914 single-upstream requests.

In ClickHouse this is one materialized column. In Elasticsearch it is either a script field
(slow) or a decision you had to make at index time. That asymmetry is the migration argument.

### 2.5 Bots and probes

What fraction of traffic is not a human?

**Answer:** **17.5%** (87,562 requests) — crawlers, API clients, scanners, and the `/health`
probe, which alone is 17,280 requests (3.5%) at a flat 0.1 req/s all day.

**The catch:** an availability SLO computed over all requests is diluted by health checks
that never fail interestingly. Exclude them, then recompute 2.3.

### 2.6 Cache behaviour and bandwidth

**Answers:** **33.7%** of static requests are 304s, carrying `body_bytes_sent: 0`. Total
bytes actually sent: **9.83 GiB**; uncompressed equivalent: **23.26 GiB** — recoverable
because `gzip_ratio` is present on **291,985** responses and
`body_bytes_sent × gzip_ratio` reconstructs the original size.

---

## Part 3 — Correlation (two sources, one story)

### 3.1 Join access and error logs

nginx's error log has **no `$request_id`**. It has `*<connection>`. Join the 504s in the
access log to their `upstream timed out` lines in the error log.

```sql
SELECT count() FROM nginx_error e
INNER JOIN nginx_access a ON e.connection = a.connection
WHERE e.cause = 'upstream_timeout' AND a.status = 504;
```

**Answer: 186 rows — but there are only 184 504s.**

Explain the extra two. (Because `connection` identifies a *TCP connection*, which carries
7.4 requests on average; two error lines matched a different request on the same keepalive
connection.) Then fix the join by adding time proximity, and note that the JSON access log's
`request_id` would have made this unambiguous — if only nginx wrote it to the error log.

> *Lesson: correlation keys are only as good as the weakest source. This is exactly why
> trace context exists.*

### 3.2 Classify errors by cause, not by level

Start by grouping the 11,476 lines by `level`:

```
warn 5,438 · error 5,097 · info 622 · crit 172 · notice 135 · alert 12
```

That tells you almost nothing useful. `warn` is the biggest bucket and none of it is a
failure; meanwhile 1,392 of the `[error]` lines are just scanners hitting a deny rule. Extract
a cause from the message text instead.

**Answers** (11,476 lines, no residual `other` bucket):

| cause | count | level |
|---|---:|---|
| response_buffered | 4,720 | warn |
| forbidden_by_rule | 1,392 | error |
| upstream_refused | 1,322 | error |
| missing_file | 977 | error |
| rate_limit_delayed | 422 | warn |
| client_aborted | 410 | info |
| upstream_bad_header | 393 | error |
| tls_handshake | 384 | crit / info |
| rate_limited | 315 | error |
| upstream_disabled | 296 | warn |
| upstream_closed | 246 | error |
| upstream_timeout | 184 | error |
| upstream_reset | 159 | error |
| lifecycle | 135 | notice |
| no_live_upstreams | 109 | error |
| worker_saturation | 12 | alert |

**The headline:** the single most common entry in this error log — 41% of it — is
`an upstream response is buffered to a temporary file`. Nothing failed. It means
`proxy_buffers` is at the stock `8 4k`, so all 4,720 proxied responses over 32 KB were spooled
to disk. An alert on "error.log volume" fires on this every day and teaches the team to ignore
the log.

**Then notice `rate_limit_delayed` (422, warn) sitting next to `rate_limited` (315, error).**
Same `limit_req` mechanism, two different outcomes: inside the configured burst nginx *delays*
the request and warns, and the client still got its 200; past the burst it rejects with 503 and
logs an error. Counting only the errors understates how much rate limiting is actually
happening by more than half.

And `upstream_disabled` (296, warn) is nginx pulling a backend out of the pool after
`max_fails` — the warning that *precedes* the `no_live_upstreams` errors and explains them.

**The catch:** `upstream prematurely closed connection` and `client prematurely closed
connection` are different failures — the backend giving up versus the user hitting stop. A
single `contains('prematurely closed')` test conflates 246 with 410. Order your conditions.

Same trap between `delaying request` and `limiting requests`: matching on the shared word
`request` merges the warn and error stages of rate limiting into one meaningless bucket.

A second catch worth reproducing before you read the fix: the classifier must be tested
against `4096 worker_connections are not enough`. That line has no `*<connection>` prefix but
*starts with a number*, so an optional `\*?(\d*)` capture group silently swallows the `4096`
and truncates the message. See the comment in `ingest/clickhouse/load.sh`.

### 3.3 Find the config reload

Something happened at ~09:41. Find it with no prior knowledge.

**Answer:** a rolling reload across the three nodes. All **24** worker PIDs before 09:41 are
replaced by 24 different PIDs after 09:55; `error.log` carries the `reconfiguring` /
`gracefully shutting down` / `start worker process` sequence, and old workers keep serving
in-flight requests for up to 18 s while draining.

**The catch:** any dashboard grouped by PID shows every series ending and new ones starting.
Nothing was wrong.

### 3.4 Reconstruct a session

Pick a busy client and reconstruct what it did.

**Answer:** `165.17.173.16` is the busiest non-probe client — 4,043 requests across 465
distinct paths. Follow `connection` + `connection_requests` to order requests within a
connection, and `http_referer` to chain page views.

Then: the funnel is `/cart` **4,763** → `/checkout` **2,319** → `/checkout/confirm`
**1,256**. Both platforms can compute this; compare how much work each one is.

---

## Part 4 — The migration argument

### 4.1 Compare storage

Load the same 499,964 requests into both and measure. Both sides here are measured, not
estimated: Elasticsearch 8.15 with `index.codec: best_compression`, one shard, no replicas,
force-merged to a single segment; ClickHouse 26.2 with the codecs in `schema.sql` and no other
tuning.

| Stream | Elasticsearch | ClickHouse | ES / CH |
|---|---:|---:|---:|
| access, JSON (29 fields) | **147.1 MiB** | **27.7 MiB** | 5.3× |
| access, combined (10 fields) | **81.1 MiB** | **7.1 MiB** | 11.4× |
| error | 2.3 MiB | 0.7 MiB | 3.4× |

Now the three-way version, all 1,011,404 records, all measured — because the middle column is
the one most teams will actually land on:

| Store | Modelling required | Compressed | vs ES |
|---|---|---:|---:|
| Elasticsearch 8.15 (3 indices, ECS + raw) | index template + 2 grok pipelines | 230.5 MiB | — |
| ClickStack default `otel_logs` (everything in a `Map`) | **none** | **93.7 MiB** | **2.5× smaller** |
| Hand-modelled ClickHouse columns (`schema.sql`) | explicit DDL per field | **35.5 MiB** | **6.5× smaller** |

That is the honest shape of the argument. You get 2.5× over Elasticsearch for free, having
modelled nothing at all — the stock `otel_logs` schema with `LogAttributes` as
`Map(LowCardinality(String), String)` compresses at 12.4×. Typed columns then buy another 2.6×
on top, and that is where you pay in DDL and migrations. Decide which of those two you are
actually arguing for.

Two things to dig into before drawing conclusions.

**First: the combined index is bigger than the file it came from.** `access.log` is 107.2 MB
on disk; its Elasticsearch index is 81.0 MiB *after* compression and force-merge — but the
index also stores `message`, the verbatim original line, alongside every parsed field. Check
`_disk_usage` and confirm. ClickHouse's equivalent table drops the raw line and keeps only
columns, which is most of why it is 11× smaller.

**Second: measure what the shipper adds — and control your comparison.** Run the load twice,
once with `add_host_metadata` enabled (the default in most Filebeat tutorials) and once
without, keeping everything else identical: same index template, both force-merged to a single
segment. Then:

```bash
POST /nginx-training-access-json/_disk_usage?run_expensive_tasks=true
```

**Answer:** 147.1 MiB without it, **221.7 MiB** with it. **+74.6 MiB, a 51% increase**, for the
shipper host's own identity repeated 499,964 times:

| field | with host metadata | without |
|---|---:|---:|
| `_source` | 87.4 MiB (39.4%) | 73.1 MiB (49.7%) |
| `host.mac` | **29.3 MiB (13.2%)** | — |
| `nginx.request_id` | 27.6 MiB (12.4%) | 27.6 MiB (18.8%) |
| `host.ip` | **19.9 MiB (9.0%)** | — |
| `host.ip.keyword` | **4.0 MiB (1.8%)** | — |
| all `host.*` | **56.8 MiB (25.6%)** | — |

Three things to take from that table. `host.mac` — the Docker host's MAC addresses, identical
on every document — is the second-largest field in the index. `host.ip` is paid for twice
because dynamic mapping gave it a redundant `.keyword` multifield. And the indexed fields are
only 56.8 MiB of the 74.6 MiB cost; the rest is their copy inside `_source`.

The lesson is as much about method as about storage: if you had compared a force-merged clean
index against a *non*-force-merged one, you would have credited the processor with a saving
that was really just segment merging. Control one variable at a time.

Then note `nginx.request_id` at 27.6 MiB — **18.8% of the clean index** for one
32-hex-character high-cardinality field. Compare it against the same column in ClickHouse and
work out why an inverted index over 499,964 unique values costs what it does, while a
`ZSTD`-compressed column of the same strings does not.

Finally, `_source` at 49.7% of the clean index is the obvious thing to disable — and doing so
breaks reindex, update, and document retrieval. Ask what the ClickHouse equivalent of that
trade-off is. (There isn't one — the columns *are* the source.)

### 4.2 Change your mind about a field

You now want the URL's path and query separated, and a `is_bot` flag.

- **Elasticsearch:** change the pipeline, then reindex 499,964 documents.
- **ClickHouse:** `ALTER TABLE … ADD COLUMN … MATERIALIZED …`. New parts compute it on
  write; use the expression directly for old ones. No reindex.

Time both. This is the exercise that usually sells the migration.

### 4.3 Ask a question nobody planned for

> "For requests where nginx retried a second upstream, what was the total latency, and which
> upstream pairs are involved?"

Nobody mapped a field for this. In ClickHouse it is `splitByString(', ', upstream_addr)` and
`arraySum` over the split times, written in the query. In Elasticsearch, without a
pre-existing mapping, it is a reindex or a slow script.

**Answer:** all **661** of them, across **42** distinct upstream pairs — 413 within the `api`
pool, 248 within the `web` pool. Every retry stayed inside the pool it started in, which is
what `proxy_next_upstream` does and a useful thing to be able to confirm from logs alone.

> *Lesson: the value of a query-time model is the questions you did not anticipate. Every
> observability investigation is an unanticipated question.*

---

## Appendix — running these on the default `otel_logs` schema

The exercise SQL above targets the hand-modelled tables in `ingest/clickhouse/schema.sql`, which
are **optional**. If you ingest through the OTel collector into ClickStack, everything lands in
the stock `otel_logs` table instead and nothing in this repo alters that schema.

The one thing to internalise: `LogAttributes` is `Map(LowCardinality(String), String)`, so
**every field is a string**. Numeric work needs an explicit cast, and there is no `url_path`
or `status_class` — you derive them in the query. All 29 access-log fields and all the
error-log fields are present under their nginx names.

Every query below was run against a live `clickhouse/clickstack-all-in-one:latest` and returns
the same answers as the hand-modelled versions.

```sql
-- 1.  Always filter by stream. count() alone double-counts: the combined and JSON
--     streams are the same 499,964 requests in two formats.
SELECT LogAttributes['log.stream'] AS stream, count()
FROM otel_logs GROUP BY stream;
--   access_combined 499964 | access_json 499964 | error 11476

-- 2.  Prove an ingest was clean, not partly duplicated (request_id is unique per request).
SELECT count(), uniqExact(LogAttributes['request_id'])
FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json';
--   499964, 499964

-- 3.  Status families -> 2xx 376837 | 3xx 95196 | 4xx 25397 | 5xx 2534
SELECT concat(substring(LogAttributes['status'], 1, 1), 'xx') AS family, count()
FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json'
GROUP BY family ORDER BY family;

-- 4.  Latency percentiles for one endpoint -> 2319 requests, p50 233 / p95 884 / p99 3516 ms.
--     Note this is 1 ms MORE accurate than the hand-modelled table, which stores
--     request_time as Float32; parsing the string as Float64 keeps the full value.
SELECT count() AS reqs,
       round(quantile(0.50)(toFloat64(LogAttributes['request_time'])) * 1000) AS p50_ms,
       round(quantile(0.95)(toFloat64(LogAttributes['request_time'])) * 1000) AS p95_ms,
       round(quantile(0.99)(toFloat64(LogAttributes['request_time'])) * 1000) AS p99_ms
FROM otel_logs
WHERE LogAttributes['log.stream'] = 'access_json'
  AND LogAttributes['request_uri'] = '/checkout';

-- 5.  5xx rate per upstream -> the six api peers spread 0.94% to 1.16%, no guilty node.
--     The position(...)=0 guard drops the 502s that list two comma-joined upstreams;
--     without it, toUInt16 on '10.0.3.14:9000, 10.0.3.13:9000' is meaningless.
SELECT LogAttributes['upstream_addr'] AS upstream, count() AS reqs,
       round(100. * countIf(toUInt16(LogAttributes['status']) >= 500) / count(), 2) AS pct_5xx
FROM otel_logs
WHERE LogAttributes['log.stream'] = 'access_json'
  AND LogAttributes['upstream_addr'] LIKE '10.0.3%'
  AND position(LogAttributes['upstream_addr'], ',') = 0
GROUP BY upstream ORDER BY pct_5xx DESC;

-- 6.  Endpoint grouping: no url_path column, so split it here.
SELECT splitByChar('?', LogAttributes['request_uri'])[1] AS path, count() AS reqs
FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json'
GROUP BY path ORDER BY reqs DESC LIMIT 20;

-- 7.  Bot share -> 87562 = 17.5%
SELECT countIf(multiSearchAnyCaseInsensitive(LogAttributes['http_user_agent'],
         ['bot','crawler','spider','curl/','python-requests','python-urllib',
          'go-http-client','zgrab','masscan','kube-probe','prometheus','healthchecker'])) AS bots,
       round(100. * bots / count(), 1) AS pct
FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json';

-- 8.  Error causes -> the same 16-cause table as exercise 3.2, to the row.
WITH LogAttributes['msg'] AS m
SELECT multiIf(
  position(m, 'upstream timed out') > 0,                  'upstream_timeout',
  position(m, 'Connection refused') > 0,                  'upstream_refused',
  position(m, 'no live upstreams') > 0,                    'no_live_upstreams',
  position(m, 'delaying request') > 0,                     'rate_limit_delayed',
  position(m, 'limiting requests') > 0,                    'rate_limited',
  position(m, 'upstream server temporarily disabled') > 0, 'upstream_disabled',
  position(m, 'SSL_do_handshake') > 0
    OR position(m, 'while SSL handshaking') > 0,           'tls_handshake',
  position(m, 'upstream prematurely closed') > 0,          'upstream_closed',
  position(m, 'client prematurely closed') > 0
    OR position(m, 'client closed connection') > 0,        'client_aborted',
  position(m, 'No such file or directory') > 0,            'missing_file',
  position(m, 'buffered to a temporary file') > 0,         'response_buffered',
  position(m, 'worker_connections') > 0,                   'worker_saturation',
  position(m, 'upstream sent too big header') > 0,         'upstream_bad_header',
  position(m, 'Connection reset by peer') > 0,             'upstream_reset',
  position(m, 'access forbidden by rule') > 0,             'forbidden_by_rule',
  match(m, 'gracefully shutting down|reconfiguring|start worker|exit|SIGCHLD|signal|event method'),
                                                          'lifecycle',
  'other') AS cause, count() AS c
FROM otel_logs WHERE LogAttributes['log.stream'] = 'error'
GROUP BY cause ORDER BY c DESC;

-- 9.  Exercise 3.1's correlation becomes a SELF-join, since both streams share one table.
--     Still returns 186 rows for 184 504s -- the keepalive ambiguity is a property of
--     nginx's $connection, not of how you stored the data.
SELECT count()
FROM otel_logs AS e
INNER JOIN otel_logs AS a
        ON e.LogAttributes['connection'] = a.LogAttributes['connection']
WHERE e.LogAttributes['log.stream'] = 'error'
  AND position(e.LogAttributes['msg'], 'upstream timed out') > 0
  AND a.LogAttributes['log.stream'] = 'access_json'
  AND a.LogAttributes['status'] = '504';
```

Two properties of the stock schema worth discussing while you are in here:

- `ORDER BY (toStartOfFiveMinutes(Timestamp), ServiceName, Timestamp)`. Every query above
  filters on a `LogAttributes` key, which the primary key cannot help with, so each one scans
  the partition. Add a `Timestamp` range and watch the difference. This is the cost of a
  schema that does not know what your fields mean — and the argument for the materialized
  columns in `schema.sql`.
- `TTL toDateTime(Timestamp) + toIntervalDay(30)`. The dataset is dated 2026-08-17. Ingest it
  more than 30 days after that date and ClickStack will drop the parts out from under you.
  Either regenerate with a current `DAY` in `generate.py` or raise the TTL before you teach
  from it.
