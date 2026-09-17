# The reference stack — Elasticsearch → ClickStack migration, end to end

> Part of [**kb2cs**](../README.md). This directory is the *reference migration*: two real
> stacks over a deterministic synthetic corpus, which is how every finding in the tooling was
> discovered and how "the migration is correct" becomes a measurable claim. The generic,
> reusable parts live in [`../skill/`](../skill/) and [`../verify/`](../verify/).


A synthetic but internally consistent nginx log dataset: **24 hours, 499,964 requests**, shipped
in three views of *the same request stream*. Built so that trainees can stand up the same
data on both sides of a migration and compare parsing, storage, and query ergonomics
directly.

Generated `2026-08-18` for the training day. Everything here is synthetic — no real traffic,
no real users, no PII. It is deterministic: one seed, no wall-clock reads, so regenerating
gives byte-identical files.

---

## Contents

| File | Lines | Raw | Gzipped | What it is |
|---|---:|---:|---:|---|
| `data/access.log` | 499,964 | 107.2 MB | 5.9 MB | Stock nginx `combined`. Matches grok `%{HTTPD_COMBINEDLOG}` exactly. |
| `data/access.json.log` | 499,964 | 433.7 MB | 31.7 MB | The same 499,964 requests, as `log_format json escape=json`, with upstream/timing/TLS fields. |
| `data/error.log` | 11,476 | 3.4 MB | 0.4 MB | nginx error log, correlated to the 5xx/499/rate-limit events in the access logs. |

Plus an optional **second service** — an apache-served documentation site, added 2026-09-16 so
the Elastic `apache` integration's dashboard has something to migrate against. Independent of
the three files above; generate it with `generator/generate-apache.py` (see
[RUNBOOK](RUNBOOK.md) step 5b) and skip it if you only care about nginx.

| File | Lines | Raw | What it is |
|---|---:|---:|---|
| `data/apache/access.log` | 249,997 | 61.7 MB | Apache `combined`. Byte-compatible with nginx's, with one visible difference: `%b` logs `-` for a 304, not `0`. Also carries 206 range responses. |
| `data/apache/error.log` | 14,665 | 2.5 MB | Apache 2.4 error log — `[module:level] [pid N:tid N] [client ip:port]`. The source of `apache.error.module` and `log.level`, which nginx's error format has no equivalent for. |

It is a *different* service, not a re-skin: lower traffic, static-file heavy, 77.7% 2xx and
16.6% 304 against nginx's shop-shaped mix. Two services that genuinely differ are what make a
mis-scoped dashboard tile visible. It does share nginx's diurnal curve, client-IP pool and
user-agent corpus — imported from `generate.py` — so geo and UA coverage stay comparable.

Supporting files:

```
ingest/nginx-log-formats.conf              the log_format directives that produce these files
ingest/elasticsearch/filebeat.yml          ES side: shipper
ingest/elasticsearch/ingest-pipeline-combined.json    grok → ECS
ingest/elasticsearch/ingest-pipeline-error.json       error log → ECS + cause
ingest/elasticsearch/index-template.json   mappings
ingest/clickstack/otel-collector-nginx.yaml  ClickStack side: filelog → OTLP
ingest/clickstack/otel-collector-{apache,postgres,mysql,system}.yaml
                                           one collector per service (service.name is per-pipeline)
ingest/clickstack/shift-ns.sh              computes SHIFT_NS to move the data to the present
ingest/clickhouse/schema.sql               hand-modelled MergeTree tables
ingest/clickhouse/load.sh                  loads all three files, no collector needed
stack/elastic/docker-compose.yml           ES + Kibana + the Fleet nginx integration,
                                           for the prebuilt nginx dashboards (see its README)
stack/clickstack/docker-compose.yml        ClickStack + HyperDX, same shape (see its README)
stack/clickstack/geoip.sh                  DB-IP + ip_trie dictionary -> geo columns on otel_logs
stack/clickstack/ua.sh                     uap-core + regexp_tree dictionary -> ua_* columns
stack/clickstack/write-mcp-config.sh       regenerates .mcp.json with the live HyperDX key
stack/clickstack/verify-migration.sh       18 checks on the migrated nginx dashboards
stack/clickstack/verify-{apache,metrics,apache-metrics,postgres,mysql,system}.sh
                                           the other six migrations (see INTEGRATIONS.md)
../verify/verify-tiles-vs-elastic.py       diffs 200 tile series against ES bucket-for-bucket
                                           (mysql, nginx, apache, postgres; takes names)
../verify/tilediff.py                      that harness's machinery
../verify/expect_*.py                      its per-integration Elastic expectations
../verify/verify-controls.py               diffs every dashboard dropdown's OPTION SET vs ES
                                           (--mutate proves each check can fail)
../verify/conf.py                          every endpoint/credential, from the environment
../verify/mcp.py                           call a ClickStack MCP tool over plain HTTP
../.mcp.json.example                       template for the MCP server config (the real
                                           .mcp.json is generated and not committed)
MIGRATION.md                               moving the Kibana nginx dashboards to ClickStack
INTEGRATIONS.md                            which integrations are migrated vs. parsed only
RUNBOOK.md                                 rebuild both stacks from zero and re-run the migration
generator/generate.py                      how the data was made (seed 20260817)
generator/validate.py                      checks every invariant below; exits non-zero on failure
checksums.txt                              sha256 of all 24 generated files
EXERCISES.md                               graded exercises with verified answers
```

> **`data/` is not in the repository.** The corpus is 704 MB and one file is 434 MB against
> GitHub's 100 MB per-file limit, so it is generated rather than committed — which costs
> nothing, because the generators are seeded and produce byte-identical output:
>
> ```bash
> cd reference-stack
> python3 generator/generate.py      # writes all 24 files into data/
> python3 generator/validate.py      # checks every invariant listed below
> shasum -a 256 -c checksums.txt     # proves the bytes match what these docs describe
> ```
>
> `generate.py` also writes the `data/*.gz` copies. Note that both Filebeat's `filestream`
> and the OTel `filelog` receiver read **plain text only** — decompress before ingesting.

---

## The scenario

`shop.example.com`, a mid-sized online store, on **three nginx edge nodes**
(`web-edge-01..03`) reverse-proxying to two upstream pools:

- `web` — 4 backends on `10.0.2.0/24:8080`, serving pages
- `api` — 6 backends on `10.0.3.0/24:9000`, serving `/api/v1/*`, `/cart`, `/checkout`, `/login` POSTs

Static assets are served by nginx directly (no upstream). Traffic is session-based: visitors
enter, walk a page graph (`/` → `/search` → `/product/…` → `/cart` → `/checkout`), pull the
static assets each page needs, and fire background XHR. Alongside them: search-engine
crawlers, mobile-app API clients, vulnerability scanners, and a steady 10-second `/health`
probe from two load balancers.

**There is no planted incident.** This is a realistic baseline: diurnal traffic peaking
around 19:00–20:00 at ~7.2× the 03:00 trough, an ordinary error budget, and background noise.
Exercises are about learning the two platforms, not about finding a needle someone hid.

Observed shape:

| | |
|---|---|
| Window | `2026-08-17 00:00:00` → `23:59:59` UTC |
| Status mix | 2xx 75.4% · 3xx 19.0% (mostly 304) · 4xx 5.1% · 5xx 0.51% |
| Distinct client IPs | 12,659 (Zipf-distributed; ~4% IPv6) |
| TCP connections | 67,453, averaging 7.4 requests each (`keepalive_timeout 75s`) |
| Bot / automated share | 17.5% |
| Peak minute | `20:04` with 787 requests |
| Worst endpoint | `/checkout` at 3.97% 5xx (vs 0.51% overall) |
| Static asset traffic | 53% of all requests, 33.7% of which are 304s |
| `error.log` level mix | `warn` 5,438 · `error` 5,097 · `info` 622 · `crit` 172 · `notice` 135 · `alert` 12 |

Note that `warn` is the **largest** level in `error.log`, and 4,720 of those 5,438 warnings are
one message: `an upstream response is buffered to a temporary file`. That is `proxy_buffers`
left at the stock `8 4k`, so every proxied response over 32 KB spools to disk. Nothing is
broken. This is deliberate — a real nginx error log is mostly tuning noise, and an alert on
"error.log is busy" would fire on it every day of the week.

---

## Field reference — `access.json.log`

Every line is one JSON object. `-` is nginx's "not applicable", never an empty string.

| Field | Type | Notes |
|---|---|---|
| `time_iso8601` | string | RFC3339, **second resolution** — nginx does not offer sub-second here |
| `msec` | string | Epoch seconds with milliseconds. **Use this one.** Truncated, never rounded |
| `hostname` | string | Which edge node wrote the line |
| `remote_addr` | string | IPv4 or IPv6 |
| `remote_user` | string | Always `-` (no HTTP basic auth in this scenario) |
| `http_x_forwarded_for` | string | Present on ~9% of requests |
| `request_method` | string | GET 88% · POST · HEAD · PUT · DELETE · OPTIONS · PROPFIND |
| `request_uri` | string | Path **and** query. Not the full request line |
| `server_protocol` | string | `HTTP/2.0` 62% of human traffic, else `HTTP/1.1`, some `HTTP/1.0` from scanners |
| `scheme`, `host`, `server_name` | string | |
| `status` | number | |
| `body_bytes_sent` | number | Response body **as sent** — post-gzip |
| `bytes_sent` | number | Body + response headers |
| `request_length` | number | Request line + headers + body |
| `request_time` | number | Seconds, ms resolution. Includes writing the body to the client |
| `upstream_addr` | string | `-` when nginx served locally; `"a:8080, b:8080"` when it retried |
| `upstream_status` | string | Same comma-joining |
| `upstream_response_time` | string | ⚠️ **String, not number** — see below |
| `upstream_connect_time` | string | |
| `upstream_header_time` | string | |
| `http_referer` | string | Internal page URLs, search engines, or `-` |
| `http_user_agent` | string | Realistic 2026-era browser/bot/client mix |
| `request_id` | string | 32 hex chars, unique per request |
| `ssl_protocol`, `ssl_cipher` | string | `-` on plain HTTP |
| `gzip_ratio` | string | e.g. `"3.41"`, or `-` when not compressed |
| `connection` | number | Per-TCP-connection serial |
| `connection_requests` | number | 1..n within that connection |

### The `upstream_*` trap

`upstream_response_time` is the field that breaks naive migrations. It holds three shapes:

```
"-"              nginx served the request itself (static, 304, 404, 403)
"0.038"          one upstream handled it
"0.002, 0.002"   nginx gave up on one upstream and retried the next
```

Every 502 in this dataset (661 of them) is the two-upstream shape, because
`proxy_next_upstream` retried. So `"type": "float"` in Elasticsearch and `Float32` in
ClickHouse both reject a real slice of the data. Both configs here store it as a string with
a nullable numeric view alongside; making that decision consciously is the point.

---

## Verified invariants

`generator/validate.py` asserts all of these against the shipped files and currently passes.
Trainees can rely on them, and can use them to check their own pipelines:

- Line *N* of `access.log` and line *N* of `access.json.log` describe **the same request** —
  matching address, status, bytes, request line, referrer and user agent.
- Every line of `access.log` matches `%{HTTPD_COMBINEDLOG}`; every line of
  `access.json.log` is valid JSON; every line of `error.log` matches nginx's error format.
- Both access logs are ordered by request *completion* time, as a real nginx log is.
- All 11,476 error-log lines fall in the same 24-hour window and are time-ordered.
- All 10,903 request-bearing error lines reference a `*<connection>` that exists in the
  access log.
- `(connection, connection_requests)` uniquely identifies a request;
  `connection_requests` runs 1..n with no gaps within each connection.
- `body_bytes_sent × gzip_ratio` reproduces the uncompressed size on all 291,985 compressed
  responses.
- `upstream_addr` is `-` if and only if `upstream_response_time` is `-`.
- `msec` and `time_local` never disagree about which second a request finished in.

## Deliberate edge cases

Not bugs. Each one exists because it breaks something real:

| In the data | Breaks |
|---|---|
| 1,249 URIs with a literal `"`, a `\`, or non-ASCII bytes — rendered `\x22` / `\x5C` / `\xC3\xA9` in combined, JSON-escaped in JSON | Naive `"([^"]*)"` grok; naive CSV/TSV parsing |
| Every 504 has `request_time` ≈ 60.0 (`proxy_read_timeout 60s`) | p99 latency charts, if you don't exclude or bucket them |
| All 661 502s list two comma-joined upstreams | Numeric casts, `GROUP BY upstream_addr` |
| 1,029 × status 499 (client aborted) with `upstream_status: "-"` | "count all non-2xx as server errors" |
| A rolling config reload at ~09:41 rotates all 24 worker PIDs | Dashboards keyed on PID |
| `/health` at exactly 0.1 req/s all day, 3.5% of volume | Availability SLOs that don't exclude probes |
| 304s carry `body_bytes_sent: 0` | Bandwidth sums; cache-hit ratios |
| 147 error lines (TLS handshake failures, lifecycle) have **no** connection number and **no** access-log counterpart | Inner joins |

---

## Quick start

### Turnkey stacks (start here)

Two `docker compose` stacks bring up a whole platform, provision it, load the dataset and
check the result. Each has its own README. Ports do not collide, so both can run at once.

```bash
cd stack/elastic          # Elasticsearch + Kibana + the Fleet nginx integration
docker compose up -d
docker compose run --rm load
./verify.sh               # 13 checks, then prints the dashboard URLs

cd stack/clickstack       # ClickStack (HyperDX + ClickHouse + OTel collector)
docker compose up -d
docker compose run --rm load   # or ./load.sh, which stops itself when the load finishes
./verify.sh               # 15 checks
```

| | `stack/elastic` | `stack/clickstack` |
|---|---|---|
| UI | Kibana, http://localhost:5601 (`elastic`/`changeme`) | HyperDX, http://localhost:8080 (`train@example.com`/`TrainingP4ss!`) |
| Data lands in | `logs-nginx.access-*` data streams | `otel_logs` |
| Parsed by | the nginx integration's own pipelines | the collector config in `ingest/clickstack/` |
| Gets you | the **prebuilt nginx dashboards**, populated | HyperDX search over all three streams |
| Loads | `access.log` + `error.log` | all three files |

Everything below is the same work done by hand, which is what you want when the point is to
see the moving parts rather than to get a UI up.


### ClickStack, by hand

Verified end to end against `clickhouse/clickstack-all-in-one:latest` (ClickHouse 26.5,
collector 0.142.0). The full ingest of all three files took **under 25 seconds**.

This is the by-hand version, kept because the four provisioning steps below are the ones that
are easy to get wrong. `stack/clickstack` automates exactly this; use that unless you want to
watch the moving parts. Both bind ports 8080 and 4317/4318.

```bash
# 1. Bring up ClickStack.
docker run -d --name cs \
  -p 8080:8080 -p 4317:4317 -p 4318:4318 -p 8123:8123 -p 9000:9000 \
  clickhouse/clickstack-all-in-one:latest

# 2. Register a team. OTLP ingest DOES NOT EXIST until you do -- see below.
#    Either use the UI at localhost:8080, or:
docker exec cs curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"ChangeMe123!","confirmPassword":"ChangeMe123!"}' \
  http://localhost:8000/register/password

# 3. Read the team's ingestion API key out of Mongo.
docker exec cs mongo --quiet hyperdx \
  --eval 'db.teams.find({},{apiKey:1}).forEach(t => print(t.apiKey))'

# 4. Ship the logs with a SEPARATE collector -- do not edit ClickStack's bundled one,
#    it is opamp-managed and will overwrite your changes.
mkdir -p .otel-storage
docker run --rm --network host \
  -v "$PWD/data:/data:ro" \
  -v "$PWD/.otel-storage:/storage" \
  -v "$PWD/ingest/clickstack/otel-collector-nginx.yaml:/etc/otel/config.yaml:ro" \
  -e CLICKSTACK_API_KEY=<the key from step 3> \
  otel/opentelemetry-collector-contrib:latest --config /etc/otel/config.yaml
```

The config shifts the data's timestamps to the present automatically; see below.

Four things that will each cost you an hour if you meet them cold. All four were confirmed by
experiment on this image, and none are in the docs:

1. **Ports 4317/4318 are not listening until a team exists.** The bundled collector is
   opamp-managed and runs with `receivers: [nop]` until then, so the ports simply are not
   bound — you get connection-refused, not an auth error. After registering, the supervisor
   pushes the real config within a few seconds.
2. **The auth header is the bare API key, not `Bearer <key>`.** `authorization: <uuid>` gets a
   200; `authorization: Bearer <uuid>` gets a 401 saying *"provided authorization does not
   match expected scheme"*, which reads like a malformed-token error and is not.
3. **The key lives in Mongo, and only the legacy `mongo` client is in the image** — there is no
   `mongosh`.
4. **The `/storage` mount is required, not optional.** The contrib image runs as uid 10001 with
   a read-only root, so the collector cannot create its own checkpoint directory and exits at
   startup with `mkdir: permission denied`.

A fifth, only if you put the collector in ClickStack's network namespace
(`--network container:cs`, which is what you need on Docker Desktop since `--network host` does
not reach container ports there): your collector's own metrics endpoint collides with the
bundled one on port 8888. Add `--set service.telemetry.metrics.level=none` rather than editing
the config.

### Backfilling 1M records into an all-in-one is a load test

The all-in-one container runs ClickHouse, Mongo, HyperDX and a collector in one memory budget,
and this dataset is a 1M-record burst. The collector settings in
`ingest/clickstack/otel-collector-nginx.yaml` are tuned for that, and the defaults are not.

Measured on the same instance, same 2500 MB cap, changing only the batch/queue settings:

| | rows ingested | duplicates | outcome |
|---|---:|---:|---|
| **Shipped settings** (batch 1,000 · 1 consumer · `timeout: 120s` · infinite retry) | **1,011,404** | **0** | clean, zero 503s, zero timeouts |
| Defaults (batch 10,000/20,000 · 10 consumers · 30s timeout · `max_elapsed_time: 300s`) | 561,340 | — | **ClickStack OOM-killed**, ~450k records lost |

The failure is not always that loud. The other shape it takes, seen on a real instance:

```
503 ... data refused due to high memory usage        <- safe: server refused, retry is clean
context deadline exceeded while awaiting headers     <- NOT safe: server may have written it
```

That second one is the dangerous one. OTLP delivery is at-least-once and `otel_logs` is a
plain MergeTree with no deduplication, so an ambiguous timeout writes the batch **twice**.
That instance ended up with 40,000 duplicate rows — exactly 4 × the 10,000 batch size — *and*
51,264 requests missing, because retries that exceeded the default `max_elapsed_time: 300s`
were dropped outright. Loss and duplication from the same cause, in the same run.

Hence the four settings, each of which is doing a specific job:

- `send_batch_size: 1000` — the batch size is your blast radius. One ambiguous failure
  duplicates a batch, so make batches small.
- `num_consumers: 1` — don't pile 10 concurrent batches onto a receiver that is already
  refusing data.
- `timeout: 120s` — wait for a slow server instead of giving up ambiguously. The default 30s
  is what turns backpressure into duplicates.
- `max_elapsed_time: 0` — never drop. Backpressure then reaches the filelog receiver, which
  simply reads more slowly.

**Always verify a backfill.** `count()` alone will not tell you:

```sql
SELECT count() AS rows, uniqExact(LogAttributes['request_id']) AS unique_requests
FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json';
-- must be 499964, 499964.  rows > unique => duplicates.  unique < 499964 => loss.
```

If it is wrong, `TRUNCATE TABLE otel_logs`, clear the `.otel-storage` checkpoint directory,
and re-run — a partial re-ingest on top of existing rows makes it worse, not better.

### Expected result

A fresh instance starts with **`otel_logs` empty**, and it does not grow on its own — there is
no bundled demo data and no self-telemetry landing in that table (verified over 90 idle
seconds). So after ingesting this dataset:

```sql
SELECT count() FROM otel_logs;                       -- 1,011,404
```

| `ServiceName` | `LogAttributes['log.stream']` | rows | timestamp range |
|---|---|---:|---|
| nginx | access_combined | 499,964 | `00:00:00.000` → `23:59:59.000` |
| nginx | access_json | 499,964 | `00:00:00.220` → `23:59:59.913` |
| nginx | error | 11,476 | `00:00:23` → `23:59:29` |

Note the ranges: the JSON stream keeps its milliseconds all the way through OTLP into
ClickHouse, and the combined stream is second-resolution, exactly as the formats imply.

`SeverityText` should be `info` 944,823 · `warn` 56,232 · `error` 10,349 — which reconciles
exactly against the per-stream figures (`error` = 2×2,534 5xx + 5,281 error/crit/alert lines,
and so on for the others).

**`count()` is not a request count.** All three streams share one table, and two of them are
the *same* 499,964 requests in different formats. Always filter:

```sql
SELECT count() FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json';
```

To prove an ingest was clean rather than partially duplicated, use the fact that `request_id`
is unique per request:

```sql
SELECT count(), uniqExact(LogAttributes['request_id'])
FROM otel_logs WHERE LogAttributes['log.stream'] = 'access_json';
-- must be 499964, 499964
```

### Which schema `otel_logs` uses

**The stock one.** Nothing in this repo creates or alters `otel_logs` — the collector config's
only exporter is `otlphttp/clickstack`, so ClickStack's own bundled collector writes every row
with its built-in DDL. Verified: `Map(LowCardinality(String), String)` attributes,
`ORDER BY (toStartOfFiveMinutes(Timestamp), ServiceName, Timestamp)`, `TTL … + 30 DAY`, plus
HyperDX's own materialized `k8s.*` columns and text indices.

The DDL in `ingest/clickhouse/schema.sql` is **optional and separate**: it builds
`nginx_training.nginx_access` / `nginx_error`, hand-modelled typed tables in their own
database, for the side-by-side modelling comparison. You never need it to use ClickStack.

Because every field then lives in `LogAttributes` as a **string**, the exercise SQL differs —
`toFloat64(LogAttributes['request_time'])` rather than a typed column, and no `url_path`.
`EXERCISES.md` has an appendix with `otel_logs`-native versions of every query, each verified
to return the same answers.

Storage for the same 1,011,404 records, all three measured:

| Store | Modelling required | Compressed |
|---|---|---:|
| Elasticsearch 8.15 (3 indices) | template + 2 grok pipelines | 230.5 MiB |
| ClickStack default `otel_logs` | **none** | **93.7 MiB** (12.4× compression) |
| Hand-modelled ClickHouse columns | explicit DDL per field | **35.5 MiB** |

Worth being clear-eyed about: 2.5× better than Elasticsearch with **no modelling at all**, and
typed columns buy a further 2.6× if you want to pay in DDL.

### Shifting the data to the present

`otel_logs` has a 30-day TTL and the dataset is dated 2026-08-17, so ingesting it later than
mid-September silently drops the parts. It also means HyperDX's default "Last 15 minutes" and
"Last 24 hours" views are empty.

This is handled in the collector config, with **no env var and nothing to remember** — the
`transform/shift_time` processor computes the offset from the dataset's own last event:

```yaml
- set(log.time_unix_nano, log.time_unix_nano +
    (UnixNano(Now()) - UnixNano(Time("2026-08-17T23:59:59Z", "%Y-%m-%dT%H:%M:%SZ"))))
```

Comment that line out to keep the original dates; change the reference if you regenerate with
a different `DAY`.

`Now()` is evaluated per record rather than once, so this stretches the window very slightly
instead of translating it rigidly. Measured over a full ingest: **4–8 s of spread across 24 h**
(0.009%), and up to **3.4 s of skew** between the two access streams, which the collector reads
concurrently. Neither matters here — the correlation exercise joins on `$connection`, not on
timestamps.

If you do need a rigid translation — or want to shift by whole days so the diurnal peak stays
at the same wall-clock hour — `ingest/clickstack/shift-ns.sh` computes a constant offset from
the data, and the config has the matching statement commented out beside the default:

```bash
export SHIFT_NS=$(./ingest/clickstack/shift-ns.sh --whole-days)   # then -e SHIFT_NS=$SHIFT_NS
```

Verified end to end with a rigid 1.829-day shift:

| | |
|---|---|
| Window after shift | `2026-08-18 19:53:34` → `2026-08-19 19:53:34` |
| Newest record | 160 s before `now()` |
| Records in the future | **0** |
| Counts / uniqueness | 1,011,404 rows, 499,964 unique `request_id`, unchanged |
| Millisecond precision | preserved (`.220`, `.232`, `.037` …) |
| Diurnal curve | identical shape — 4.7k trough, 34.3k peak |

The two modes trade off against each other. The default lands the last event at now, so every
default time picker is populated, but the peak moves to an arbitrary wall-clock hour (in the
run above, 19:00 became 15:00). `--whole-days` shifts by whole days so 19:00 stays 19:00 and
the curve reads correctly against real time, at the cost of the window ending at its original
time of day rather than now.

**One consequence to know about.** The shift moves the OTLP `Timestamp` only. The original
dates are still inside the record — `Body` holds the verbatim log line, and
`LogAttributes['msec']`, `['time_iso8601']` and `['ts']` keep their original values. So after
shifting, `Timestamp` and `msec` differ by exactly `SHIFT_NS`. That is fine for dashboards and
for the TTL, and it is a nuisance for exercise 1.2, which is about parsing those very fields.
If you want everything internally consistent, change `DAY` in `generate.py` and regenerate
instead — then re-run `validate.py` and refresh `checksums.txt`.

### ClickHouse directly

Skip the collector when you want to compare *modelling* rather than *ingest*. This loads all
three files into purpose-built tables — verified against ClickHouse 26.2:

```bash
docker run -d --name ch --ulimit nofile=262144:262144 clickhouse/clickhouse-server
CH="docker exec -i ch clickhouse-client" ./ingest/clickhouse/load.sh
```

Result on ClickHouse 26.2, no tuning beyond the codecs in `schema.sql`:

| Table | Rows | Uncompressed | Compressed | Ratio |
|---|---:|---:|---:|---:|
| `nginx_access` (JSON log, 29 columns + 9 derived) | 499,964 | 208.5 MiB | 27.7 MiB | 7.5× |
| `nginx_access_combined` (combined log, 10 columns) | 499,964 | 114.7 MiB | 7.1 MiB | 16.2× |
| `nginx_error` | 11,476 | 4.3 MiB | 0.7 MiB | 6.4× |

### Elasticsearch, by hand — the modelling comparison

**Not the dashboards path.** This builds custom `nginx-training-*` indices using the
hand-written pipelines in `ingest/elasticsearch/`, so Elasticsearch's storage cost and query
ergonomics can be compared like for like against the ClickHouse tables in
`ingest/clickhouse/`. It is the "before" side of the migration argument.

If you want the **prebuilt nginx dashboards**, use `stack/elastic` instead. That is a separate
path with its own indices (`logs-nginx.access-*`), the integration's own pipelines, and its own
compose file — the two do not share data, and neither one populates the other's views.

| | this section | `stack/elastic` |
|---|---|---|
| Indices | `nginx-training-access-json`, `-access-combined`, `-error` | `logs-nginx.access-*`, `logs-nginx.error-*` |
| Parsing | the grok pipelines in this repo | the nginx integration's |
| Security | off | on (Fleet requires it) |
| Answers | "what does this cost in ES vs ClickHouse?" | "do the shipped dashboards work?" |

Both bind port 9200, so stop one before starting the other.

Verified against Elasticsearch 8.15.0 and Filebeat 8.15.0:

```bash
docker run -d --name es -p 9200:9200 \
  -e discovery.type=single-node -e xpack.security.enabled=false \
  -e "ES_JAVA_OPTS=-Xms2g -Xmx2g" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.0

# pipelines and template first — Filebeat references them by name
curl -XPUT localhost:9200/_ingest/pipeline/nginx-combined \
  -H 'Content-Type: application/json' -d @ingest/elasticsearch/ingest-pipeline-combined.json
curl -XPUT localhost:9200/_ingest/pipeline/nginx-error \
  -H 'Content-Type: application/json' -d @ingest/elasticsearch/ingest-pipeline-error.json
curl -XPUT localhost:9200/_index_template/nginx-training \
  -H 'Content-Type: application/json' -d @ingest/elasticsearch/index-template.json

docker run --rm --network host -v "$PWD/data:/data:ro" \
  -v "$PWD/ingest/elasticsearch/filebeat.yml:/usr/share/filebeat/filebeat.yml:ro" \
  docker.elastic.co/beats/filebeat:8.15.0 -e --strict.perms=false
```

Filebeat writes one index per stream with **no date suffix** — `nginx-training-access-json`,
`nginx-training-access-combined`, `nginx-training-error`. Both parts of that are deliberate,
and both are traps the config comments explain: a `%{+yyyy.MM.dd}` suffix resolves
client-side *before* the server-side `date` processor runs, so the combined and error streams
would land under today's date rather than 2026-08-17; and per-stream indices make the storage
comparison against the ClickHouse tables apples-to-apples.

Result after `_forcemerge?max_num_segments=1`, with `index.codec: best_compression` from the
template, one shard, no replicas:

| Stream | Docs | Elasticsearch | ClickHouse | ES / CH |
|---|---:|---:|---:|---:|
| access, JSON | 499,964 | 147.1 MiB | 27.7 MiB | **5.3×** |
| access, combined | 499,964 | 81.1 MiB | 7.1 MiB | **11.4×** |
| error | 11,476 | 2.3 MiB | 0.7 MiB | 3.4× |

Worth noticing before you read anything into those ratios: the ES figures are only that good
because `add_host_metadata` is switched **off** in `filebeat.yml`. Measured like for like —
same template, both force-merged to one segment, only that processor differing — the JSON index
is **147.1 MiB without it and 221.7 MiB with it**. That is +74.6 MiB, a 51% increase, for the
shipper host's own identity repeated 499,964 times (`host.mac` alone: 29.3 MiB). Most Filebeat
tutorials enable it. See the comment in `filebeat.yml`.

### What has and hasn't been executed

Being precise about this, because the configs look equally finished:

| Artifact | Status |
|---|---|
| `generator/generate.py`, `validate.py` | Run; all invariants pass |
| `ingest/clickhouse/schema.sql` + `load.sh` | **Run end to end** against ClickHouse 26.2. All 1,011,404 lines load; parsed values spot-checked against the generator's own counts |
| `ingest/elasticsearch/*` | **Run end to end** against Elasticsearch 8.15.0 + Filebeat 8.15.0. All 1,011,404 lines indexed across three indices, **zero** parse failures and zero pipeline errors; aggregations cross-checked against the ClickHouse answers |
| `stack/elastic/` | **Run end to end.** ES 8.15 + Kibana 8.15 + nginx integration 3.2.2 from the package registry; 511,440 docs loaded with zero rejections and zero grok failures; all 13 `verify.sh` checks pass; both log dashboards' panel queries return data. Persistence confirmed across a full `down`/`up` |
| `stack/clickstack/` | **Run end to end.** Team auto-registered, API key captured and handed to the collector via the file provider; 1,011,404 rows with 499,964 unique `request_id`s; all 15 `verify.sh` checks pass |
| `ingest/clickstack/otel-collector-nginx.yaml` | **Run end to end against live ClickStack** (`clickhouse/clickstack-all-in-one:latest`, ClickHouse 26.5): team registered, real API key, unmodified config file. All 1,011,404 records in `otel_logs` in under 25 s, per-stream counts exact, millisecond timestamps preserved through OTLP, `SeverityText` totals reconciling, and `request_id` unique across all 499,964 JSON rows (no duplication) |
| `stack/clickstack/geoip.sh` | **Run end to end.** DB-IP City Lite loaded into an `ip_trie` dictionary, four `MATERIALIZED` geo columns backfilled by mutation. Close to Elastic's GeoLite2 on the largest countries (US −2.9%, CN +1.5%, JP +1.7%) but **not uniformly** — Canada differs by +42.7%. The two vendors disagree, which is the point of the comparison |
| `MIGRATION.md` | **Run end to end.** Both Kibana nginx dashboards migrated onto ClickStack via MCP; all 10 panels built and every tile validated with `clickstack_query_tiles`. Every panel with a numeric expectation matches Elastic exactly — including the two user-agent panels, diffed bucket-for-bucket against `terms` aggregations on `user_agent.name` / `.os.name` — except geo, which differs by vendor as above. Only the map could not be reproduced: ClickStack has no map tile type |

Every number quoted in this README and in `EXERCISES.md` comes from one of those runs
against the shipped files, so they are measured rather than estimated.

Both platforms independently agree on the data, which is the useful part: identical status
families (2xx 376,837 · 3xx 95,196 · 4xx 25,397 · 5xx 2,534), ES's cardinality aggregation
returning exactly the generator's 12,659 distinct client IPs, and the two error-cause
classifiers — one Painless, one SQL `multiIf` — agreeing on all **16 causes across all 11,476
lines with zero mismatches**. The `ingest/*` paths load the files as-is, with no time shift,
so those two also share an identical timestamp range **down to the millisecond**.

The `stack/*` turnkey paths are different, and this catches people out: each one shifts the
dataset forward to end at "now" and computes that shift when it runs, so the two end up apart
by however long elapsed between the two loads — 40 minutes on the reference run. The span and
the distribution are identical; the absolute placement is not. Compare totals and
distributions rather than wall-clock windows, or load both from a common shift — see
[Keeping the two stacks on the same clock](RUNBOOK.md#keeping-the-two-stacks-on-the-same-clock).

Getting to that last one took two rounds: the first comparison showed ES putting 24 lines in
`other` that ClickHouse called `lifecycle`, because `contains('exiting')` does not match a
bare `exit`. Cross-checking two implementations against each other is how you find that; a
single implementation just looks right.

One harness note if you re-run the Filebeat load: its registry lives inside the container, so
with `--rm` every run re-reads the files from the start. Delete the indices **by name** first
(`DELETE /nginx-training-access-json` etc.) — a wildcard `DELETE /nginx-training-*` is
rejected by default in ES 8 unless `action.destructive_requires_name=false`, and if you miss
that you silently end up with two copies of everything.

---

## Migration mapping

The concept translation trainees should end up internalising:

| Elasticsearch | ClickStack / ClickHouse |
|---|---|
| Ingest pipeline with grok | Collector operators, or `extract()` / `splitByChar()` at query time |
| Index template mapping | `CREATE TABLE` column types |
| `keyword` | `LowCardinality(String)` (low cardinality) or `String` |
| `date` | `DateTime64(3, 'UTC')` |
| Enrichment fixed at index time | `MATERIALIZED` columns, or plain SQL at query time |
| Runtime / script fields | Any expression in `SELECT` — no special mechanism |
| Shards, replicas, ILM | `PARTITION BY`, `ORDER BY`, `TTL` |
| `geoip` processor + GeoLite2 | `ip_trie` dictionary + `dictGetOrDefault`, in a `MATERIALIZED` column (`geoip.sh` uses DB-IP City Lite) |
| Index-time `user_agent` processor (uap-core) | `regexp_tree` dictionary over the same uap-core corpus, in a `MATERIALIZED` column (`ua.sh`) |
| Reindex to change a mapping | `ALTER TABLE … MODIFY COLUMN`, no reindex |
| KQL / Lucene | SQL, or Lucene-style search in the HyperDX search bar |
| Watcher | Scheduled queries / HyperDX alerts |

The single biggest conceptual shift: in Elasticsearch you decide what a field means at
**index time** and pay to change your mind. In ClickHouse most derivation is free at
**query time**, and the choices you do make up front (`ORDER BY`, codecs) are about physical
layout rather than semantics.

### Enrichment: geo

The three log files carry **no geo fields**, and deliberately so — baking a fixed
`geo.country_iso_code` into synthetic records would have frozen one vendor's answer into the
data and removed the interesting part. Geo is derived from `remote_addr` on each platform
instead, which turns it into a live comparison rather than a copied column. Both stacks have
it, by different mechanisms:

| | mechanism | when it is computed | fields |
|---|---|---|---|
| Elasticsearch | `geoip` processor + GeoLite2, from the Fleet nginx integration | index time, frozen into each document | `source.geo.country_iso_code`, `.country_name`, `.city_name`, `.region_name`, `.region_iso_code`, `.continent_name`, `.location`, plus `source.as.*` |
| ClickStack | `stack/clickstack/geoip.sh` — DB-IP City Lite loaded into an `ip_trie` dictionary | `MATERIALIZED` columns, repointable and recomputable with a mutation | `geo_country_code`, `geo_city`, `geo_latitude`, `geo_longitude` |

Two things to take from that table. The ClickStack side is **narrower** — DB-IP Lite ships no
country/region/continent names and no ASN, so those five Elastic fields have no equivalent.
But it is also **not frozen**: repoint the dictionary at a different database, run
`MATERIALIZE COLUMN`, and every historical row gets the new answer. In Elasticsearch the same
change means a reindex. Same panel, different operational properties.

The two databases also disagree, which is the most useful part of the exercise:

| | Elastic (GeoLite2) | ClickStack (DB-IP Lite) | Δ |
|---|---:|---:|---:|
| US | 182,300 | 177,022 | −2.9% |
| CN | 46,221 | 46,921 | +1.5% |
| JP | 25,800 | 26,249 | +1.7% |
| DE | 18,129 | 18,883 | +4.2% |
| FR | 12,753 | 13,363 | +4.8% |
| **CA** | **6,955** | **9,923** | **+42.7%** |

Same IPs, same day, different vendors. Note that the gap does **not** stay in the low single
digits: the big three agree closely, and Canada is off by 43% — DB-IP places roughly 3,000
more requests there than MaxMind does. Since the client IPs are synthetic, this is not a
statement about real Canadian traffic; it means the generator's address ranges fall into
blocks the two databases classify differently.

That is the exercise: geo is the one enrichment where identical data and an identical query
still produce different answers, because the answer lives in a third-party dataset that
belongs to neither platform. Worth internalising before anyone treats a geo dashboard as
ground truth.

The standing caveat still applies: the client IPs are synthetic, so none of these answers mean
anything *geographically*. They are internally consistent and cross-platform comparable, which
is all the exercise needs.

Also absent: traces, metrics, and application logs. This is nginx only — a deliberately
single-signal dataset so the migration mechanics stay in focus.

---

## Regenerating

```bash
python3 generator/generate.py                      # ~9s, no dependencies beyond stdlib
python3 generator/validate.py                      # ~8s, checks every invariant above
python3 generator/generate.py --requests 50000     # smaller variant for a constrained sandbox
```

Only Python 3 standard library. The seed (`SEED = 20260817`) and the date (`DAY`) are
constants at the top of `generate.py`; changing either changes every file, so re-run
`validate.py` and refresh `checksums.txt` afterwards.

To shift the data to a different day without regenerating — often what you want, since
observability UIs default to "last 24h" — edit `DAY` and regenerate rather than trying to
rewrite timestamps in place; three files with two timestamp representations and a correlated
error log will not survive a `sed`.
