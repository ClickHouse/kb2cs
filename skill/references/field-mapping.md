# ECS → ClickStack field mapping

## First: find out which of two target shapes you have

This decides most of the work, and getting it wrong wastes a whole pass. Elastic is
predictable — an integration's ingest pipeline produces **ECS, flattened into typed fields at
index time**. The ClickStack side is not, because it depends entirely on how the data was
ingested:

| target shape | `LogAttributes` keys look like | when you get it |
|---|---|---|
| **A. OTel semantic conventions** | `http.response.status_code`, `url.path`, `client.address` | standard OTel SDKs, instrumentation libraries, the collector's own receivers — **the common case for a customer** |
| **B. Raw source names** | `status`, `request_uri`, `remote_addr` | a hand-written collector pipeline that regex/JSON-parses a log line and keeps the source's own vocabulary |

One query tells you which:

```sql
SELECT arrayJoin(mapKeys(LogAttributes)) AS k, count() FROM otel_logs GROUP BY k ORDER BY 2 DESC
```

Dotted, namespaced keys mean shape A; bare single words mean shape B. A table can hold both
at once if different services were onboarded differently — check per `ServiceName`, not just
globally.

**Shape A is much less work than this document's examples suggest.** ECS was donated to
OpenTelemetry in 2023 and the two vocabularies have largely converged, so a good share of the
mapping is *identity*: `http.request.method`, `http.response.status_code`, `url.original`,
`url.path`, `url.query`, `user_agent.original`, `host.name` and `service.name` are spelled the
same on both sides. The renames that remain are few and worth memorising:

| ECS | OTel semconv | note |
|---|---|---|
| `source.ip` / `source.address` | `client.address` | `source.*` is the ECS spelling; semconv uses `client.*` for the peer and `server.*` for the receiving side |
| `source.port` | `client.port` | |
| `destination.address` | `server.address` | |
| `http.version` | `network.protocol.version` | and `network.protocol.name` = `http` |
| `http.request.body.bytes` | `http.request.body.size` | `bytes` → `size` |
| `http.response.body.bytes` | `http.response.body.size` | |
| `event.duration` (ns) | `http.server.request.duration` (s, and a *metric*) | unit and signal both change |
| `url.domain` | `server.address` | |

> Treat that table as a starting point to **verify**, not as ground truth: semconv is
> versioned and some HTTP attributes moved during stabilisation, so the emitting SDK's version
> decides the spelling. Confirm every row against the `mapKeys` output before using it. It was
> derived from the semconv/ECS alignment, not measured against a customer deployment.

Two things are true in **both** shapes, and they are what actually breaks tiles:

- **Everything in the map is a `String`**, whatever the name suggests. `http.response.status_code` is still text and still needs a cast.
- **Resource-level vs log-level matters.** `service.name`, `host.name`, `k8s.*` and
  `deployment.environment` are *resource* attributes — `ResourceAttributes['...']`, or a
  promoted column like `ServiceName`. Reaching for them in `LogAttributes` returns the empty
  string silently. Some deployments also expose `__hdx_materialized_*` columns for hot
  resource keys; `describe_source` lists them.

So up to three things happen to every field: it may be **renamed**, it **loses its type**, and
it may **not exist at all** (if Elastic computed it in a pipeline). Handle those as separate
passes, not one.

Build the real map by inventorying both sides rather than trusting any table:

```sql
-- target: which keys exist, and how often
SELECT arrayJoin(mapKeys(LogAttributes)) AS k, count() FROM otel_logs GROUP BY k ORDER BY 2 DESC
```

```bash
# source: which ECS fields are actually populated (mappings list far more than are used)
curl -s -u "$U:$P" "$ES/$INDEX/_field_caps?fields=*" | python3 -c '...'
```

Then diff against `inventory-panels.py --fields`, which is the set the dashboards actually need.

## Standard OTel log columns (real columns, not map keys)

These are typed columns on `otel_logs`, so no cast is needed:

| ECS | ClickStack | type |
|---|---|---|
| `@timestamp` | `Timestamp` | `DateTime64(9)` |
| `message` | `Body` | `String` |
| `log.level` | `SeverityText` / `SeverityNumber` | often coarser than the source's own level field |
| `service.name` | `ServiceName` | |
| `trace.id` / `span.id` | `TraceId` / `SpanId` | |
| `host.name` | `ResourceAttributes['host.name']` | resource-level, not log-level |

**Check `SeverityText` against the raw level key before choosing.** The collector maps source
levels onto the OTel severity scale, which can merge distinct source levels. If the source
panel showed `warn`/`error`/`crit` separately, the raw attribute preserves them and
`SeverityText` may not.

## Casting: the rule that breaks the most tiles

Everything in `LogAttributes` is a `String`. Sorting, ranging or summing a string silently
gives lexicographic nonsense rather than an error — `'99' > '100'` is true.

| operation | wrong | right |
|---|---|---|
| numeric compare | `LogAttributes['status'] >= 500` | `toUInt16(LogAttributes['status']) >= 500` |
| sum | `sum(LogAttributes['bytes'])` | `sum(toUInt64(LogAttributes['bytes']))` |
| latency / float | `avg(LogAttributes['request_time'])` | `avg(toFloat64(LogAttributes['request_time']))` |
| absent key | `= ''` **and** `IS NULL` both look plausible | a missing map key returns the **empty string**, not NULL |

Use the `OrZero` / `OrNull` variants when a field is optional or carries a sentinel — nginx
writes `-` for absent values, and `toUInt64('-')` throws, killing the whole tile:

```sql
toUInt64OrZero(LogAttributes['upstream_status'])     -- '-' becomes 0
toFloat64OrNull(LogAttributes['request_time'])       -- excluded from avg() rather than skewing it
```

Prefer `OrNull` over `OrZero` for *averages* and `OrZero` for *sums* — a zero drags an average
down but is harmless in a total.

## Fields Elastic derived at index time

These have no key on the target; they become query-time expressions. The worked examples below
come from a **shape B** target (raw nginx names), so read the *pattern*, not the key names — on
a semconv target `url.path` and `url.query` usually already exist and need no expression at
all. Check before deriving something the target already has:

| ECS field | ClickStack expression |
|---|---|
| `url.path` (from a URI with a query string) | `splitByChar('?', LogAttributes['request_uri'])[1]` |
| `url.extension` | `extract(LogAttributes['request_uri'], '\\.([a-z0-9]+)(\\?\|$)')` |
| `event.outcome` | `if(toUInt16(LogAttributes['status']) >= 500, 'failure', 'success')` |
| `http.version` (`2.0` vs `HTTP/2.0`) | `replaceOne(LogAttributes['server_protocol'], 'HTTP/', '')` |
| status *family* (Kibana range filters) | `concat(toString(intDiv(toUInt16(LogAttributes['status']), 100)), 'xx')` |
| `user_agent.*`, `source.geo.*` | **not an expression** — see `enrichment.md` |

Watch for value-level differences even where the field maps cleanly: a protocol string
carrying its `HTTP/` prefix on one side, a `-` sentinel instead of an absent field, or a
dataset name spelled `nginx.access` on one side and `access_json` on the other. These produce
tiles that run, render, and disagree.

## Aggregations that are not aggregations

Lens pipeline operations do not map onto a ClickStack `aggFn`. They need a window function
over the grouped result, which in practice means a `sql` tile:

| Lens op | ClickHouse |
|---|---|
| `differences` | `x - lagInFrame(x) OVER (ORDER BY bucket)` |
| `cumulative_sum` | `sum(x) OVER (ORDER BY bucket ROWS UNBOUNDED PRECEDING)` |
| `moving_average` | `avg(x) OVER (ORDER BY bucket ROWS n PRECEDING)` |
| `counter_rate` | difference divided by the bucket width; for a monotonic counter, guard resets |
| `unique_count` | `uniqExact(x)` when comparing against the source exactly, `uniq(x)` otherwise |

`differences(of max(counter))` is the standard shape for a monotonic counter — the stock
`[Metrics Nginx] Overview` uses it on four of eight panels. Note that `max` inside the bucket
then a difference across buckets is **not** the same as a plain delta, and getting it wrong
produces a plausible-looking chart with the wrong magnitude.

## Where the ClickStack side is *richer*

Worth checking, because it reframes the job. The target often ingests a **different, wider
source file** than the source platform did — on the reference migration, Elastic parsed
`access.log` (stock combined, no latency at all) while the collector ingested
`access.json.log` with `request_time`, `upstream_*`, `request_id`, `ssl_*` and per-node
`hostname`.

So p95 latency, upstream error attribution and per-node breakdowns are dashboards the *source
could not express*. Offer them as additions once the migration verifies — but do not add them
before, or you lose the ability to say the migration is complete.

## Writing the mapping down

Produce a three-column table as a migration artifact: **source field · target expression ·
note**. The note column is the valuable one — it is where "string, cast it", "`-` when absent,
not null" and "absent, see enrichment" live. Anyone re-running the migration reads the notes,
not the mappings.
