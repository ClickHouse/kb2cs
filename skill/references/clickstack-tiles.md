# ClickStack tiles: schema, chart types, and the filter trap

Everything below was read out of the live `clickstack_save_dashboard` tool schema on
**ClickStack 2.35.0-beta**. The schema is version-dependent, so re-check it rather than
trusting this file:

```bash
CLICKSTACK_PERSONAL_API_KEY=... python3 scripts/introspect-clickstack.py
python3 scripts/introspect-clickstack.py --url https://<host>/api/mcp --key <personal-key>
```

A new chart type appearing (a map, above all) would change step 3 of the procedure.

## Chart type vocabulary

`displayType` accepts exactly **ten** values:

`line` · `stacked_bar` · `table` · `number` · `pie` · `bar` · `heatmap` · `search` ·
`event_patterns` · `markdown`

**`sql` is not a `displayType`.** A raw SQL tile is its own tile branch, identified by
`configType: "sql"`, carrying a `sqlTemplate` plus a `displayType` restricted to the six
chart types (`line`, `stacked_bar`, `table`, `number`, `pie`, `bar`). So a SQL tile *renders
as* a chart; it is not a chart type. Listing `sql` alongside the others will send you looking
for a `displayType` that does not exist.

There is **no map type**, which is the one gap that makes a panel unmigratable.

### Kibana → ClickStack chart mapping

| Kibana | ClickStack | note |
|---|---|---|
| `lnsXY` / `histogram` / `line` | `line`, `bar`, `stacked_bar` | pick from `preferredSeriesType` / `seriesType` — see the warning below |
| `lnsPie` / `pie` (incl. donut) | `pie` | the donut/pie distinction is cosmetic and does not survive |
| `lnsDatatable` / `table` | `table` | |
| `lnsMetric` / `metric` | `number` | |
| `lnsHeatmap` / `heatmap` | `heatmap` **only if the source has no `terms` breakdown** | see the warning below — ClickStack's heatmap has no categorical axis |
| saved search | `search` | plus `clickstack_save_saved_search` for a standalone object |
| `lnsGauge` / `goal` / `gauge` | `number` | **degrades** — no gauge rendering |
| `area` | `line` | **degrades** — no fill |
| `tagcloud` | `table` | **degrades** — same data, different form |
| `region_map` / `coordinate_map` / `map` | — | **no equivalent.** Best effort: `bar`/`table` on a country column |
| `vega`, `timelion` | `sql` or re-author | arbitrary expressions; translate by hand |
| `links`, control panels | — | not data; do not count as panels |

Distinguish **degrades** (data survives, appearance changes — usually acceptable) from **no
equivalent** (the panel cannot be reproduced). Only the second kind belongs in the loss list.

**`seriesType: bar` / `bar_stacked` on an `lnsXY` with a date_histogram is a TIME-SERIES bar,
and it maps to `stacked_bar`.** ClickStack's `bar` is *categorical* and needs a `groupBy`; only
Kibana's `bar_horizontal` means that. `inventory-panels.py` already derives this and prints it
in the **Target displayType** column — so:

> **Do not override the inventory's target displayType by hand.** On the mysql
> `Database Overview` migration all 15 XY panels were `bar_stacked` and the inventory said
> `stacked_bar` for all 15; four were nonetheless built as `line` because a ratio or a
> gauge-plus-rate mix "felt" like a line chart. The user spotted it immediately. Nothing
> numeric catches this — every value was correct — and it is a gratuitous difference from the
> source. If a different chart type really is better, change it *after* the faithful migration
> is verified, and say so.

**ClickStack's `heatmap` has no categorical axis, and this is the second rendering-layer
gap after maps.** Read off the live `save_dashboard` schema: a heatmap tile takes
`select` with **exactly one** item, a required numeric `valueExpression`, and **no `groupBy`
at all**. It is a *value-distribution* heatmap — the shape a trace-latency chart has, where
the y-axis is value buckets.

A Kibana heatmap that puts `terms(<field>)` on an axis therefore has no target. Measured on
`[Metrics System] Overview`, whose two panels are `terms(host.name)` x date_histogram x
`average(...)`: both degrade to a **line chart grouped by that field** — same data, different
rendering. Declare it in step 3 alongside maps.

> The trap is that the displayType *name* matches, so a naive mapping table calls these
> "ready". `inventory-panels.py --triage` now flags any heatmap carrying a `terms()`
> breakdown; before that fix it reported both panels as directly translatable.

**A Kibana saved search is a document table, not an aggregation.** It shows one row per
document, newest first. Translating it with a `GROUP BY` over columns that happen to be
constant collapses thousands of rows into one — which looks like a working tile. On the mysql
`Replica Status` migration this turned a 2,760-row panel into a single row, because the
replica's host/port/uuid/binlog columns never change. If the source is a `search`, the target
needs `ORDER BY <time> DESC LIMIT N`, not an aggregation.

Note this also means a saved search over a **metric** source cannot use the `search`
displayType at all — that tile type reads a log source (it wants `Body` and a timestamp). Use
a `sql` tile with `displayType: "table"`.

## Tile structure

A dashboard is `{id?, name, tiles: [...], tags: [...], filters: [...], containers: [...]}`.

**`filters` at dashboard level is the natural home for a Kibana dashboard-level filter** — the
one that panels inherit (see `kibana-export.md`). Using it preserves the source's structure
instead of copying the same predicate onto every tile.

Each tile carries `id`, `name`, `x`/`y`/`w`/`h`, optional `containerId`/`tabId`, and a
`config`:

```jsonc
{
  "id": "tile-1",
  "name": "Response codes over time",       // tile level on input; persists to config.name
  "x": 0, "y": 0, "w": 6, "h": 3,           // Kibana gridData maps onto this directly
  "config": {
    "displayType": "stacked_bar",
    "sourceId": "<logs source id>",         // from clickstack_list_sources / describe_source
    "select": [
      {
        "aggFn": "count",
        "alias": "2xx",
        "where": "LogAttributes['log.stream'] = 'access_json' AND toUInt16(LogAttributes['status']) BETWEEN 200 AND 299",
        "whereLanguage": "sql"              // REQUIRED -- the default is lucene
      }
    ],
    "groupBy": "concat(toString(intDiv(toUInt16(LogAttributes['status']),100)),'xx')"
    // NO tile-level `where` here: forbidden on stacked_bar. See "The filter trap" below.
  }
}
```

Other keys worth knowing, per variant: `orderBy` and `limit` (`pie`, `bar`), `having` and
`groupByColumnsOnLeft` (`table`), `seriesLimit`, `fillNulls`, `asRatio` and
`compareToPreviousPeriod` (`line`, `stacked_bar`), `colorRules` and `backgroundChart`
(`number`). There is **no** `granularity` key — bucket width is not a per-tile setting.

Select items also take `valueExpression` (required for every `aggFn` except `count`),
`numberFormat`, and for metric sources `metricName`, `metricType` and `isDelta`.

Carry `gridData` from the source panel into `x/y/w/h` rather than re-laying-out by hand — the
migrated dashboard should be recognizable to someone who used the original. Note the grid
widths differ (Kibana is 48 columns wide), so scale rather than copy.

## The filter trap

Filter placement depends on the tile type, and the schema is explicit about it — the six
builder chart types declare `where` as `{"not": {}}` (JSON Schema for "nothing validates
here") with the description *"Not supported on this tile type. Filter via each select item's
`where`."*

| tile type | tile-level `config.where` | per-`select`-item `where` |
|---|---|---|
| `line`, `stacked_bar`, `table`, `number`, `pie`, `bar` | **forbidden** | **yes — put it here** (compiles to `countIf(…)`) |
| `heatmap` | **allowed** | no |
| `search`, `event_patterns` | **allowed** (and `select` is a *string* column list, not an array) | no |
| `markdown` | n/a | n/a |
| SQL tile (`configType: "sql"`) | n/a | n/a — filter inside `sqlTemplate` |

Note `heatmap` sits with `search`, not with the chart types it resembles.

For the six builder charts, a predicate at tile level does not produce a useful error — it is
simply not where the filter belongs, the tile renders, and every number is wrong, typically
doubled if the target ingests the same events by two routes. This is the single most
expensive mistake in the migration and the hardest to spot, because nothing fails visibly.

## `seriesLimit` bypasses the select-item `where`, and can silently empty a chart

`seriesLimit` (on `line` and `stacked_bar`) adds a top-N series ranking pass that does **not**
apply each select item's `where`. Verified on ClickStack 2.35.0-beta with two tiles identical
but for that one key, both grouping on a bare `toUInt16(LogAttributes['status'])`: without
`seriesLimit` the query returns rows, with it the cast throws.

Two consequences, and the second is the dangerous one:

1. **The `groupBy` expression sees every row in the window**, including rows that lack the
   attribute entirely, so a cast in it can fail on data the tile is not counting:

   ```
   Cannot parse UInt16 from String, because value is too short:
     while executing 'FUNCTION toUInt16(LogAttributes.key_status)'
   ```

2. **The top N series are ranked by unconditional volume.** If the target table holds more
   than one dataset — which is the normal case, and the whole reason each select item carries
   a scoping `where` — the ranking is decided by whichever dataset is biggest. The N series it
   picks can be almost entirely empty once the tile's own filter is applied, and the tile then
   **queries successfully and draws nothing.**

Making the cast null-safe (`toUInt16OrZero`) fixes only the error and leaves the empty chart,
which is worse: it converts a loud failure into a silent one.

**So: do not set `seriesLimit` on a tile whose scope comes from a select-item `where`.** On a
shared table that means every tile. Group on the raw string where you can
(`LogAttributes['status']` needs no cast, so null-safety is automatic) and accept showing the
whole distribution instead of the source's `terms size=N`.

> `query_tiles` cannot catch this. It reported `status: ok` and `hasData: true` for the broken
> tile at every time range, because the query did return rows — just rows of the wrong series.
> This is the canonical example of the class **step 6b** exists for: compare the rendered
> charts, not only the numbers. See `verification.md`, "The visual pass".

Prototyping with `clickstack_table` will not reproduce it either: the table tool pushes a
select-item filter down into a row-level `WHERE`, so the cast never meets the bad rows, and
`clickstack_timeseries` does not accept `seriesLimit` at all.

### It has now fired twice, and nothing catches it after the fact

Second occurrence, 2026-09-17: `Syslog events by hostname` on a migrated `system` dashboard
came back blank. `seriesLimit: 5` ranked `host.hostname` over the **whole** table, whose top
bucket is the **empty string** with 1,399,267 rows — every nginx/apache/postgres/mysql row,
none of which has that attribute. The real five hosts were crowded out.

What makes this worth a hard rule rather than care: **no verification path can reproduce it.**

- `clickstack_timeseries` and `clickstack_table` have **no `seriesLimit` parameter**, so
  re-issuing the tile's own `select`/`groupBy`/`where` through the builder tools — the method
  that catches everything else — cannot express the setting that breaks the tile. The
  distributions come back perfect.
- `clickstack_query_tiles` reported `status: ok` and `hasData: true`.
- A full-distribution diff against the source passed 13/13.

So the only defence is structural: **assert the key is absent** on every `line` and
`stacked_bar` tile. In the reference repo that is a `serieslimit:<dashboard>` verb on the
saved-object query helper, and the integration verifiers assert every dashboard reports
nothing.

> Set `seriesLimit` only when the tile's scope comes from the *data itself* rather than from a
> select-item `where` — which on a shared `otel_logs` is almost never.

### Read-back trap: a pie/bar `limit` is PERSISTED as `seriesLimit`

Auditing for the above has a false-positive mode. A `pie` or `bar` tile's `limit` is stored in
the dashboard document under the name **`seriesLimit`**; the MCP `clickstack_get_dashboard`
hands it back as `limit`, but the HyperDX REST API (`/dashboards`) does not. So a raw-document
scan for `seriesLimit` flags every pie and bar as a violation.

On `pie`/`bar` that key *is* the legitimate row cap. Scope any such audit by `displayType` —
only `line` and `stacked_bar` carry the dangerous meaning. This is the same class as the
`where` -> `aggCondition` mismatch documented below: what you save is not what you read back.

## A multi-dimension `groupBy` silently truncates the time axis

Measured on a metric gauge over a 24h window at 1-hour granularity:

| `groupBy` | series | rows returned | buckets per series |
|---|---:|---:|---|
| `Attributes['state']` | 11 | 264 | 24 — complete |
| `Attributes['state'], ResourceAttributes['host.name']` | 22 | **60** | **2–3** |

All 22 series come back; each one loses almost every bucket. The chart is then a scatter of
fragments rather than 22 lines, and `query_tiles` reports `status: ok` with a plausible row
count either way.

So: **a two-dimension `groupBy` on a builder tile is not safe above ~a dozen series.** A
Kibana panel that splits N metric series by a `terms` breakdown (a very common shape — one
metric family plus "split by host") lands exactly here. Use a `sql` tile with an explicit
`LIMIT` and your own `concat(dim1, ' @ ', dim2)` series key; that returns the full 528 rows.

Verify it by running the tile's own SQL and counting `uniqExact(series)` **and**
`uniqExact(ts)` — the series count alone looks healthy while the buckets are gone.

## De-cumulation applies to Sums only — but Gauges have their own trap

De-cumulation is Sum-only: `aggFn` on a Gauge is not applied to a per-bucket increase.

**That does not make a Gauge panel a builder tile.** On a metric source HyperDX collapses a
gauge to **one sample per bucket — the last one — *before* `aggFn` runs.** So `aggFn`
aggregates across *series* (and, in a `number`/`table` tile, across buckets), never across the
samples inside a bucket. Measured on ClickStack 2.35.0-beta, one series, one bucket whose
samples give `max=14, avg=10.8, last=10`:

```
aggFn = max | min | avg | sum | last_value   ->   10, 10, 10, 10, 10
```

All five agree with each other and none is the answer Kibana gives. In a `number`/`table` tile
the aggFn *is* honoured across buckets but each bucket is still a last-value, so a 23h window
whose true `max=79, avg=37.696` returns `max=62, avg=37.196` — close enough to look right.

Consequences:

- `average(some.gauge)` and `max(some.gauge)` over a bucket are **not builder shapes**. They
  need a `sql` tile doing plain `avg(Value)` / `max(Value)` with a `GROUP BY` on the bucket.
- `last_value(some.gauge)` **is** a builder shape, and matches Kibana exactly. Kibana's
  `last_value()` panels are the gauge case that migrates cleanly.
- A single-series gauge tile is where this hides best: with one series there is nothing to
  aggregate across, so every aggFn returns the same number and the chart still looks plausible.

This was found in the mysql migration and it had already produced **wrong tiles in two earlier,
fully-verified migrations**: the nginx `Active connections` tile disagreed with Elastic in
141 of 144 buckets and apache's `Average server load` in 144 of 144 — while both suites passed,
because they compared whole-window averages rather than buckets. The errors were small
(3.63 vs 3.67), so neither the charts nor `query_tiles` showed anything.

> If a Kibana metric panel uses `average()`, `max()` or `min()`, write a SQL tile regardless of
> whether the field is a counter or a gauge. Only `last_value()` and counter `differences()`
> have faithful builder equivalents.

## Sum vs Gauge is decided by the PANEL, not by the source's metric type

The integration's mapping (`time_series_metric: counter` vs `gauge`) tells you what the field
*is*. It does not tell you what to emit, because de-cumulation means the choice is about what
the panel *reads*:

| the source panel does | emit as | why |
|---|---|---|
| `differences(max(f))`, or any rate | **Sum** | de-cumulation *is* the differencing |
| `max(f)` / `last_value(f)` on a counter | **Gauge** | a Sum would return the bucket's increase, not the value |

So a counter whose panel reads its absolute value has to be shipped as a **gauge**, and a
constant whose panel `differences()` it has to be shipped as a **Sum**. Both cases occurred in
one dashboard (mysql `Database Overview`):

- `max_used_connections` — a high-water mark, typed `counter`, read with `max()` → gauge
- `innodb.buffer_pool.pool.reads` and `.read.requests` — two halves of a lifetime ratio, read
  with `max()` → gauges
- `replica_status.source.log_position.{read,exec}` — binlog offsets, typed `counter`, read with
  `last_value()` → gauges
- `cache.ssl.size` — a constant 128, typed `counter`, and its panel `differences()` it, so
  Kibana plots a flat zero. Shipped as a **Sum** so ClickStack plots zero too; as a gauge it
  would have plotted 128 and disagreed silently.

If you control the ingest side, this is a decision you get to make per field, and the source
panel is the only thing that should decide it.

## One attribute that moves with the value destroys series identity

A data-point attribute whose value changes every scrape gives every point its own attribute
set, so a metric with one logical series becomes thousands. **Nothing errors.** The series just
fragment, and since a gauge is aggregated across series *after* being collapsed to a
last-value, every tile then reads an arbitrary point per bucket.

Measured: attaching `source.file_info` (`"<binlog file> <byte position>"`) to each replica
data point produced **2,879 distinct series for one replica** instead of 1. It surfaced as
`last_value()` tiles disagreeing with Kibana by a few thousand bytes — a plausible wrong number.

```sql
-- run this on any metric you are about to chart
SELECT MetricName, uniqExact(Attributes) AS series, count() AS points
FROM otel_metrics_gauge GROUP BY MetricName ORDER BY series DESC
```

If `series` is close to `points`, an attribute is carrying data rather than identity. Keep the
constant identity attributes; derive the moving one in the tile's SQL instead.

## Row limits: which tile types have one, and the `table` gap

Three different fields, on disjoint sets of tile types, and one type with neither:

| tile type | limit field |
|---|---|
| `line`, `stacked_bar` | `seriesLimit` (three-state: omit = default cap, `0` = unlimited, N = top N) |
| `pie`, `bar` | `limit` |
| **`table`** | **neither** |

So a Kibana **datatable with `terms(field) size=N`** has no builder equivalent — a builder
`table` returns every group. Either accept the full list or write a `sql` tile with an explicit
`ORDER BY ... LIMIT N`. This bit the mysql `Top slowest queries` panel, which is
`terms(query) size=5`.

Remember `seriesLimit` also ranks by *unconditional* volume and bypasses the select-item
`where` — see its own section above.

## `clickstack_query_tiles` is a smoke test, not verification

It returns, per tile, `status`, `hasData` and `rowCount`. **It does not return values.** So it
answers "does this tile run and produce rows" and nothing else — which is worth running on
every tile after a save, and is not evidence that a tile is correct.

Every "valid query, wrong series" bug found in this project's migrations had `status: ok` and a
plausible `rowCount`. To check values you have to either

- read the tile's stored `sqlTemplate` back (`clickstack_get_dashboard`), expand the macros and
  run it — this is what "run the tile, not an equivalent" means in `verification.md`; or
- for a builder tile, which has no `sqlTemplate`, re-issue its own `select`/`groupBy`/`where`
  through `clickstack_timeseries` / `clickstack_table`, which is the same code path.

Two gotchas while doing that:

- **`clickstack_query_tiles` defaults to the last 15 minutes** and ignores unknown parameters
  like `duration`. Pass `startTime`/`endTime` as ISO strings, and make them an absolute window.
- **`clickstack_get_dashboard` takes `id`, not `dashboardId`** — unlike every other dashboard
  tool. Passing the wrong key does not error: it falls back to LISTING all dashboards, which
  reads downstream as "this dashboard has no tiles".

## Builder `increase` has two edge buckets Kibana does not

Comparing an `aggFn: increase` tile against Kibana's `differences(max(f))` over the same
absolute window, the two agree on every bucket except the two at the edges:

- **leading** — `increase` reads the counter from *before* `startTime`, so it emits a real
  value for the first bucket. Kibana's `differences()` has no predecessor inside the window and
  yields null there.
- **trailing** — the builder emits one extra bucket *at* `endTime`, fed by data past the window
  end.

Neither is a migration error, and ClickStack's leading bucket is arguably the better answer.
But a bucket-for-bucket verifier has to trim both edges or it reports a failure on a tile that
is right.

## Writing a rate or delta in a `sql` tile

Three things go wrong, in rising order of how long they take to notice.

**1. Detect "no previous bucket" structurally.** `lagInFrame(x)` returns the column default
(0 for a number) when there is no previous row, so `WHERE prev != 0` is tempting and wrong: it
means "has a predecessor" only while the metric never legitimately holds 0. Use

```sql
row_number() OVER (ORDER BY ts) AS rn   -- then: WHERE rn > 1
```

Measured cost of getting this wrong on counters that sit at zero (postgres `conflicts`,
`deadlocks`): 48 buckets became 31, and the dropped rows were exactly the ones where the
counter first moved — every spike. The query stayed valid; `query_tiles` stayed green.

**2. `normalize_by_unit(..., unit='s')` is a division by bucket width.** No `aggFn` expresses
it; `$__interval_s` does.

**3. `pick_max(x, 0)` is `greatest(x, 0)`** — it floors counter resets at zero rather than
charting a negative rate.

So the full translation of Kibana's most common metrics formula,
`pick_max(normalize_by_unit(differences(max(X)), unit='s'), 0)`:

```sql
WITH b AS (
  SELECT $__timeInterval(TimeUnix) AS ts, max(Value) AS mx
  FROM otel_metrics_sum
  WHERE MetricName = 'X' AND $__timeFilter(TimeUnix)
  GROUP BY ts
), d AS (
  SELECT ts, mx, lagInFrame(mx) OVER (ORDER BY ts) AS prev,
         row_number() OVER (ORDER BY ts) AS rn
  FROM b
)
SELECT ts, greatest((mx - prev) / $__interval_s, 0) AS "rate"
FROM d WHERE rn > 1 ORDER BY ts LIMIT 5000
```

Eight of nine panels on the reference PostgreSQL metrics dashboard are this one template.
**Read each series' aggregate out of its own formula, though** — that dashboard's
Conflict/Deadlock panel uses `average()` for one series and `max()` for the other, and using
one for both flattens the series that needed the other.

## Dashboard-level `filters` are dropdowns, not predicates

A Kibana dashboard-level filter is *applied*: every panel inherits it. ClickStack's
dashboard-level `filters` are **interactive filter-bar controls** — each entry takes an
`expression` naming a column whose values populate a dropdown, plus `type:
"QUERY_EXPRESSION"`, `name` and `sourceId`. Nothing is filtered until a user picks a value.

So a dashboard-level filter cannot carry the source's scoping. Any inherited Kibana filter has
to be pushed onto **every** tile's select items, and dashboard `filters` are then a usability
extra rather than a translation of anything. This is easy to get backwards, because the key is
named the same thing on both platforms.

## Two Kibana shapes that flatten, and are worth declaring

Both are degradations rather than losses — the data survives, the structure does not — but
they change what the panel *says*, so declare them in step 3 alongside the map:

- **A multi-ring donut becomes a flat pie.** Kibana nests `terms` buckets into concentric
  rings (e.g. `user_agent.name` outside, `user_agent.version` inside). ClickStack's `groupBy`
  takes a comma-separated list and renders one slice per *combination*, so the hierarchy is
  gone and the slice count multiplies.
- **A datatable with nested `terms` buckets becomes a global top-N.** Two `terms` aggs of
  size 5 mean "top 5 URLs *within* each of the top 5 statuses" — 25 rows chosen per-group. A
  ClickStack table is flat: ordered by the metric across all combinations. Every row it shows
  has the right number, but it is not the same row set, and there is no `limit` key on the
  table variant to bound it with.

## `whereLanguage` defaults to `lucene`, not SQL

`whereLanguage` is an enum of `sql` | `lucene` and **defaults to `lucene`** on every select
item. So a ClickHouse expression written into `where` without setting
`whereLanguage: "sql"` is parsed as a Lucene query — it does not throw, it just does not mean
what you wrote. Any filter using `toUInt16(...)`, `LogAttributes['k']` or a comparison
operator must set it explicitly.

## Aggregation functions

`aggFn` on a select item is one of:

`avg` · `count` · `count_distinct` · `last_value` · `max` · `min` · `quantile` · `sum` ·
`any` · `none` · `increase`

`count` needs no `valueExpression`; every other function requires one. Three that matter when
translating Kibana:

| Kibana | ClickStack |
|---|---|
| `unique_count` / `cardinality` | `count_distinct` |
| `percentile` / TSVB percentile | `quantile` — but `level` is an **enum of `0.5`, `0.9`, `0.95`, `0.99` only** |
| `differences(of max(counter))` on a Sum metric | `increase` for the fleet total, `max` for the biggest single series — **neither is Kibana's number on multi-series data.** See below |

**The `quantile` restriction is a real migration limitation.** A Kibana panel showing p75,
p90.5 or p99.9 cannot be reproduced through a builder tile; it needs a SQL tile using
`quantile(0.75)(...)`. Record it as a degradation rather than silently rounding to p95.

## Cumulative Sums: every `aggFn` operates on the increase, not the value

**This is the single biggest surprise in the metrics path, and an earlier version of this file
had it wrong.** It claimed `differences(of max(counter))` was "exactly `increase`". It is not.

ClickStack **de-cumulates a Sum before applying `aggFn`**. Measured on one 1-hour bucket of a
cumulative counter whose raw value there was **12,002,184**, across three host series:

| aggFn | returns | what it actually is |
|---|---|---|
| `max` | 2,163 | the largest per-series increase in the bucket |
| `min` | 2,028 | the smallest |
| `avg` | 2,092.3 | the mean of the three |
| `sum` | 6,277 | the fleet total increase |
| `increase` | 6,277 | identical to `sum` here |
| `last_value` | 2,086 | one series' increase |

Two consequences, both of which change what you can promise:

**1. A Sum tile cannot chart a raw cumulative counter at all.** A Kibana panel doing
`max(some.counter)` — which is how integrations chart "total requests since start" — has no
builder equivalent. It needs a `sql` tile selecting `max(Value)` directly. Two of the eight
panels in the reference metrics dashboard were like this.

**2. `differences(of max(V))` ≠ `increase`.** Kibana collapses series with `max()` *first* and
differences the result: Δ(maxₕ V). ClickStack differences each series and then aggregates:
Σₕ ΔV for `increase`, maxₕ ΔV for `max`. On single-series data these coincide. On the
reference three-host dataset they do not: 499,915 fleet-wide against 165,050 for the
highest-numbered counter alone.

Neither is wrong; they answer different questions. The fleet total is almost always the one
the user wants, and Kibana's figure is an artifact of `max()` over hosts — so migrate to
`increase` and **record it as a declared deviation**, or reproduce Kibana exactly with a `sql`
tile doing `max()` per timestamp then `lagInFrame` across buckets. Do not report it as a match.

**A counter must arrive as CUMULATIVE and monotonic** (`aggregationTemporality: 2`,
`isMonotonic: true`) for any of this to work. Check it before blaming a tile:

```sql
SELECT any(AggregationTemporality), any(IsMonotonic) FROM otel_metrics_sum WHERE MetricName = '...'
```

`isDelta`, incidentally, is documented for **gauges** — a cumulative gauge you want charted as
growth per bucket. It is not the Sum knob it looks like.

So for a dashboard where every panel is scoped to one dataset, the scoping predicate must be
repeated on every select item of every tile. Verify it mechanically rather than by eye:

```python
# flag any tile whose filters do not mention the scoping field
for tile in dashboard["tiles"]:
    cfg = tile.get("config", {})
    if cfg.get("displayType") == "markdown":
        continue
    blob = " ".join([cfg.get("where") or "", cfg.get("sqlTemplate") or ""] +
                    [(s.get("where") or "") + (s.get("aggCondition") or "")
                     for s in cfg.get("select") or [] if isinstance(s, dict)])
    if SCOPE_FIELD not in blob:
        print("unscoped:", cfg.get("name"))
```

## Read-back mismatch: `where` vs `aggCondition`

The MCP API and the persisted schema **use different key names**:

| | on `save_dashboard` input | as persisted / read back |
|---|---|---|
| select-item filter | `where`, `whereLanguage` | `aggCondition`, `aggConditionLanguage` |
| tile title | accepted at tile level | stored at `config.name` |

A verification script that checks only for `where` reports every correctly-filtered tile as
unfiltered — a false alarm that looks exactly like the real bug above. Read both keys, as in
the snippet.

## Builder tools vs. raw SQL

Prefer `clickstack_table` and `clickstack_timeseries` over `clickstack_sql`: the server's own
guidance is that the builder tools are more reliable and return chart-ready results. Use them
to prototype each panel's numbers *before* saving any tile — a failing builder call is a fast,
legible error, while a bad saved tile has to be found by querying it back.

Reach for a `sql` tile only when the shape genuinely cannot be expressed: multi-level
aggregations, window functions, joins against a dictionary that is not exposed as a column.

`clickstack_describe_source` surfaces `MATERIALIZED` columns as ordinary top-level columns, so
enrichment columns (`geo_*`, `ua_*`) are reachable from the builder tools and do **not** force
a `sql` fallback. Check before assuming you need one.

## Create, then patch

`clickstack_save_dashboard` **without** `id` creates; with one, replaces. For fixing a single
tile use `clickstack_patch_dashboard` — resubmitting the whole object to change one expression
risks dropping tiles you are not thinking about. Tag every migrated dashboard
(`migrated-from-kibana` plus a domain tag) so `clickstack_search_dashboards` can retrieve the
set.
