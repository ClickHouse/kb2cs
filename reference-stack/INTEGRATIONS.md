# Integration coverage

Which Elastic integrations this project has actually migrated to ClickStack, which it has only
*read*, and what separates the two. Written because the distinction is easy to lose: parsing a
dashboard proves the tooling works, it does not produce anything on the target.

**Summary: 5 integrations migrated — logs and metrics for each.** Seventeen dashboards exist
on ClickStack out of 37 read. `system` is migrated for Linux but not Windows; every other
integration is complete. Every number below was measured against the running stacks on
2026-09-17.

## Status by integration

| integration | version | dashboards | panels | fields | status |
|---|---|---:|---:|---:|---|
| **nginx** (logs) | 3.2.2 | 2 | 10 | 12 | **migrated** 2026-08-20 — 12 tiles + 2 saved searches, `verify-nginx.sh` 42/42, **12 series bucket-for-bucket** |
| **apache** (logs) | 3.0.2 | 1 | 7 | 12 | **migrated** 2026-09-16 — 8 tiles, `verify-apache.sh` 45/45, **11 series bucket-for-bucket** |
| **nginx** (metrics) | 3.2.2 | 1 | 8 | 10 | **migrated** 2026-09-16 — 9 tiles (7 `sql`), `verify-nginx.sh` 42/42, **11 series bucket-for-bucket** |
| **apache** (metrics) | 3.0.2 | 1 | 11 | 33 | **migrated** 2026-09-18 — 13 tiles (11 `sql`), nothing unmigratable, `verify-apache.sh` 46/46, **46 series bucket-for-bucket** |
| **postgresql** (logs) | 1.31.0 | 2 | 6 | 7 | **migrated** 2026-09-16 — 8 tiles, **all builder**, nothing degraded; covered by the same `verify-postgres.sh` 21/21, **7 series bucket-for-bucket** |
| **postgresql** (metrics) | 1.31.0 | 1 | 9 | 19 | **migrated** 2026-09-16 — 10 tiles (9 `sql`, one template ×8), `verify-postgres.sh` 21/21, **31 series bucket-for-bucket** |
| **mysql** (logs) | 1.28.1 | 1 | 6 | 6 | **migrated** 2026-09-16 — 7 tiles (1 `sql`), **multi-line** slow log; covered by the same `verify-mysql.sh` 36/36 (no series; search/terms tiles) |
| **mysql** (metrics) | 1.28.1 | 2 | 20 | 43 | **migrated** 2026-09-16 — 22 tiles (13 `sql`), `verify-mysql.sh` 36/36, **42 series bucket-for-bucket** |
| **system** (logs) | 1.62.1 | 4 | 21 | 17 | **migrated** 2026-09-17 — 25 tiles (4 `sql`), 1 panel unmigratable (map), 1 degraded (tag cloud), `verify-system.sh` 44/44, 13 distributions diffed |
| **system** (metrics) | 1.62.1 | 2 | 39 | 36 | **migrated** 2026-09-17 — 41 tiles (33 `sql`), 2 panels degraded (heatmaps), `verify-system.sh` 44/44, 20 comparisons diffed |
| system (Windows Security) | 1.62.1 | 5 | 80 | 23 | parsed only — a different OS corpus |
| kubernetes | 1.83.1 | 15 | 130 | 166 | parsed only |
| synthetics | 1.3.0 | 0 | — | — | installed; ships no dashboards |
| *301 others* | — | — | — | — | available in the registry, not installed |
| **total read** | | **37** | **347** | **346** | 17 dashboards · 154 tiles (77 `sql`) migrated · 20 parsed only |

`panels` counts data panels only — the 2 `links` navigation panels are excluded, since
counting them makes a finished migration look incomplete. `fields` is distinct source fields
the dashboards depend on, which is the list you diff against the target schema; the 346 total
is lower than the column sum because integrations share ECS fields like `@timestamp`.

> **The `fields` column was under-reported until 2026-09-16.** It read 271 across the estate
> where the real figure is 326 (346 including postgresql). `inventory-panels.py` collected
> only each Lens column's `sourceField`, and a Lens **formula** column has none — its fields
> are written inside the formula string, e.g.
> `pick_max(normalize_by_unit(differences(max(postgresql.database.rows.fetched)), unit='s'), 0)`.
> Formula-heavy dashboards therefore looked almost field-free: the stock PostgreSQL metrics
> dashboard reported 9 fields against a real surface of 25. Since `--fields` is the input to
> the field-mapping step, that gap is the difference between mapping a dashboard and believing
> you already had. Found by installing postgresql and not believing the number.

The Fleet registry offers **308** packages; **7** are installed here. Installation is only
needed to read a dashboard's definition — a migration target needs the *data* as well, which
is the actual constraint (see [Why the rest stop at parsing](#why-the-rest-stop-at-parsing)).

## Migrated

### nginx — logs

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Logs Nginx] Overview` | 7 + 1 links | `[Logs Nginx] Overview (migrated)` | 8 |
| `[Logs Nginx] Access and error logs` | 3 + 1 links | `[Logs Nginx] Access and error logs (migrated)` | 4 |

Plus two saved searches, `Nginx access logs (migrated)` and `Nginx error logs (migrated)`.
Both dashboards are tagged `nginx` + `migrated-from-kibana`. Tile counts exceed panel counts
by one each: a `markdown` provenance note was added to each dashboard.

Performed **2026-08-20**; the recipe and the field mapping are in
[`MIGRATION.md`](MIGRATION.md). Re-verified 2026-09-15 with
`./stack/clickstack/verify-nginx.sh` — **42/42 checks pass**, including that the
expressions *stored inside the tiles* still reproduce Elastic's numbers (499,964 · 11,476 ·
12,659 · 10,557,094,271 · 2,534), all 5 OS buckets and all 20 browser buckets.

One panel of the ten did not survive as itself: the geo map became a country `bar` tile.
See [Unmigratable panels](#unmigratable-panels).

**A second degradation, found on 2026-09-16 by `inventory-panels.py --triage` and not by any
verification script — now FIXED.** The Kibana `Operating systems breakdown` and
`Browsers breakdown` panels are *two-ring donuts* — `terms(user_agent.os.name)` **and**
`terms(user_agent.os.version)`, likewise for browser name and version. The migrated tiles
grouped on `ua_os` / `ua_browser` alone, so the version ring was silently dropped on
2026-08-20 and went unnoticed for four weeks.

`verify-nginx.sh` could not catch it: it verified the tiles that exist against Elastic and
never asked whether a tile reproduces its source panel's *shape*. Its "all 5 OS buckets and
all 20 browser buckets match exactly" claim was true and measured the wrong thing — the family
level, which was all the tile had.

**Repaired 2026-09-16.** A ClickStack pie takes a **single** `groupBy` column, so the two rings
flatten into one label:

```sql
if(ua_os_version = '', ua_os, concat(ua_os, ' ', ua_os_version))
```

The `if` reproduces how a donut renders a family whose inner ring has no value — `Linux`
carries no `user_agent.os.version`, so it stays family-only rather than becoming `Linux `. The
OS tile went from 5 buckets to **8**, the browser tile from 20 to **25** (e.g. `Go-http-client`
splits into 2.0 = 8,797 and 1.1 = 3,045, summing to the old 11,842). Every bucket matches
Elastic's own nested `terms` exactly, with two known exceptions already in the record:
ClickStack's extra `Other` OS bucket (uap-core returns the literal string where Elastic omits
the field) and `Firefox 141.0` against Elastic's `Firefox 141.0.` — see
[Where the source platform is wrong](#where-the-source-platform-is-wrong).
`verify-nginx.sh` was re-baselined and is still **18/18**.

### apache — logs

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Logs Apache] Access and error logs` | 7 | `[Logs Apache] Access and error logs (migrated)` | 8 |

Tagged `apache` + `migrated-from-kibana`. Eight tiles from seven panels: the extra one is the
provenance markdown. Unlike nginx there are **no standalone saved searches** — apache's
errors-log panel is stored by value inside the dashboard, not as a referenced `search` object.

Performed **2026-09-16**, verified with `./stack/clickstack/verify-apache.sh` — **45/45**.

This migration needed **data first**, which is what had kept apache at "parsed only": a
dashboard cannot be verified against a source that has no rows. `generator/generate-apache.py`
produces a second service — a documentation site, `docs.example.com`, 249,997 requests and
14,665 error entries over the same 24h window as nginx — rather than a re-skin of the nginx
stream. Two services that genuinely differ are what make a mis-scoped tile *visible*: if an
apache tile accidentally reads nginx rows, the numbers move. It shares nginx's diurnal curve,
IP pool and user-agent corpus by import, so geo and UA coverage stay comparable.

Three of its numbers deliberately disagree with Kibana, because the source is wrong — see
[Where the source platform is wrong](#where-the-source-platform-is-wrong).

Two pieces of groundwork it forced, both reusable:

- **UA version components.** The apache donuts break down by browser/OS name *and version*;
  the nginx ones group on family alone, so `convert-regexes.py` had only ever emitted
  families. It now emits `v1..v4` components and `ua.sql` exposes `ua_browser_version` /
  `ua_os_version`, reproducing Elastic's join rule (append while non-null, stop at the first
  gap). All 7 of Elastic's OS name×version buckets match exactly.
- **A second loader path.** `load-to-datastream.py --service apache` and
  `load-apache.sh` + `otel-collector-apache.yaml`. The apache collector emits nginx's
  attribute names (`remote_addr`, `http_user_agent`) on purpose, so the existing `geo_*` and
  `ua_*` materialized columns enrich both services with no second dictionary.

### nginx — metrics

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Metrics Nginx] Overview` | 8 | `[Metrics Nginx] Overview (migrated)` | 9 |

Tagged `nginx` + `metrics` + `migrated-from-kibana`. Performed **2026-09-16**, verified with
`./stack/clickstack/verify-nginx.sh` — **42/42**.

> **Corrected 2026-09-16, after the mysql migration.** Five tiles across these two dashboards
> — nginx `Active connections` and `Reading / Writing / Waiting Rates`, apache
> `Total connections`, `Workers` and `Average server load` — were builder **gauge** tiles, and
> builder gauge tiles cannot reproduce a Kibana panel that aggregates a gauge's samples within
> a bucket. HyperDX collapses a gauge to one sample per bucket (the last) *before* `aggFn`
> runs, so `avg`/`max`/`min`/`sum`/`last_value` all return the same number. Measured per
> five-minute bucket over 12 h:
>
> | series | buckets disagreeing with Elastic |
> |---|---:|
> | nginx `Active connections`, `Waiting` | 141 / 144 each |
> | nginx `Writing` | 74 / 144 |
> | nginx `Reading` | 23 / 144 |
> | apache `Total connections` | 92 / 144 |
> | apache `Average server load` ×3 | 144 / 144 each |
>
> All ten series are now SQL tiles and match Elastic in **144/144** buckets. Why it survived
> two sign-offs: the errors are small (3.63 against 3.67), so the charts looked right and the
> **visual pass could not see them**; and both verifiers asserted whole-window averages, which
> match to four decimal places. Only a per-bucket diff finds this. Both suites now also assert
> that these tiles stay SQL — `verify-nginx.sh` is 24 checks and
> `verify-apache.sh` 25.
 The first *metrics* migration here, and
the first use of `sql` tiles: **five of the eight panels** needed one, which is itself the
headline — the logs dashboards migrated almost entirely into builder tiles.

Data: `generator/generate-nginx-metrics.py` derives the `stub_status` series from
`data/access.json.log`, so the metrics and the logs describe the same traffic. 8,640 scrapes
(30 s × 24 h × 3 nodes) and one checkable link between the two datasets — the per-bucket
increases of `requests` sum to the access log's own line count. Loaded by
`stack/elastic/load-metrics.py` and `stack/clickstack/load-metrics.py`.

Five things this established that the logs path never touched:

- **A cumulative Sum cannot be charted raw.** ClickStack de-cumulates a Sum *before* applying
  `aggFn`, so every function operates on the per-series per-bucket **increase**. Measured on
  one bucket whose raw counter read 12,002,184: `max`=2,163 (largest per-host increase),
  `min`=2,028, `avg`=2,092.3, `sum`=`increase`=6,277. So `Total requests` and `Processed
  requests` — `max(nginx.stubstatus.requests)` in Kibana — have **no builder equivalent** and
  are `sql` tiles selecting `max(Value)`.
- **`differences(of max(counter))` is not `increase`.** Kibana collapses hosts with `max()`
  then differences: Δ(maxₕ V), one node's rate. ClickStack differences per series then
  aggregates: Σₕ ΔV, the fleet total. Over three nodes that is 164,498 against 499,915. This
  **corrected a wrong claim** in the migration skill, which had said the two were equivalent.

  The rate tiles were first shipped using `increase`, on the argument that a fleet total is
  the more useful number. That was wrong as a *migration* and visibly so: a single-node burst
  that Kibana renders as a 1.8x spike is diluted to noise once three nodes are summed. They
  are now SQL tiles reproducing Kibana bucket-for-bucket — all 144 ten-minute buckets, peak
  2,368. The lesson is in the skill now: reproduce the source, offer the improvement
  separately.
- **A SQL tile that subtracts across a dimension collapses onto one series.** `Drops Rate`
  computed `max(accepted) - max(handled)` across hosts, which always resolved to the
  highest-numbered node: 1 spike where Kibana shows 4. It must difference *per host* and then
  take the max. Neither `query_tiles` (which reported `ok`, 49 rows) nor the verification
  script caught it — the script had hand-written an equivalent query instead of running the
  tile's stored SQL. It now runs the stored SQL, for this tile and for Request Rate.
- **Four gauge fields collapse into one metric.** Elastic's
  `nginx.stubstatus.{active,reading,writing,waiting}` become one OTel
  `nginx.connections_current` carrying a `state` attribute — so the three-series Kibana panel
  is three select items filtered on `state`, and all four averages match Elastic to 4 d.p.
- **TSDB rejects a backfill by default.** `metrics-nginx.stubstatus` is `index.mode:
  time_series`, whose write index only accepts timestamps within `look_back_time` (**2 h**).
  The loader sets `look_back_time: 30h` on the data stream's `@custom` component template
  *before* creating the stream, which is the supported override point.

Deliberately **not** identity-mapped: the loader emits the **OTel nginx receiver's** metric
names, not Elastic's field names, because that is the shape a customer on standard OTel
instrumentation actually has. The mapping table is in the loader's docstring.

### apache — metrics

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Metrics Apache] Overview` | 11 | `[Metrics Apache] Overview (migrated)` | 12 |

Tagged `apache` + `metrics` + `migrated-from-kibana`. Performed **2026-09-16**, verified with
`./stack/clickstack/verify-apache.sh` — **45/45**. See the gauge-tile correction under
[nginx — metrics](#nginx--metrics); three of this dashboard's tiles were affected. 11 panels → 10 data tiles, a gap
note for the one that could not be migrated, and a provenance note.

Data: `generator/generate-apache-metrics.py`, 5,760 scrapes (30 s × 24 h × 2 nodes), with
`total_accesses` and `total_bytes` derived from `data/apache/access.log`. Two invariants tie
it to the logs: accesses sum to the log's 249,997 lines, and `total_bytes` across both nodes
is **49,005,925,124** — the identical figure `verify-apache.sh` checks as
`sum(body_bytes_sent)`. The metrics and the logs describe the same traffic.

**The headline: 7 of the 10 data tiles are SQL, and one panel has no target at all.** The logs
dashboards went almost entirely into builder tiles. The richer the metrics dashboard, the less
survives — counters and derived rates dominate, and neither is a builder shape.

| panel group | outcome |
|---|---|
| `Uptime`, `Total accesses`, `Total egress` | `sql` **number** — `max()` of a cumulative Sum has no builder equivalent |
| `Requests per sec`, `Bytes per sec` | `sql` — **no metric on the target**; mod_status computes them as counter ÷ uptime and the OTel receiver emits neither. Derived, and they match Elastic exactly (0.849 · 168,998.1229) |
| `CPU usage` | `sql` — mixes a gauge with four values inside a cumulative Sum |
| `Scoreboard` | `sql`, but for a different reason: 22 series (11 states × 2 hosts) **truncate to 2–3 buckets each** in a builder tile — and then truncated again at its own `LIMIT 5000`. See below |
| `Total connections`, `Workers`, `Average server load` | **`sql` since 2026-09-16** — were builder gauge tiles, but a gauge is collapsed to one sample per bucket before `aggFn`, so `avg()`/`max()` over the bucket's samples is not a builder shape either |
| `Connections` (async writing/keep-alive/closing) | **`sql`** — `apache.connections.async` + `connection_state`. Recorded as unmigratable until 2026-09-18; see below |

Four findings, on top of what nginx metrics established:

- **De-cumulation is Sum-only — but that does not make a gauge panel a builder tile.**
  This was recorded here as "`max` on a gauge is the ordinary maximum, so
  `max(apache.current_connections)` is a one-line builder tile". **That was wrong**, and the
  mysql migration measured it: a gauge is collapsed to one sample per bucket before `aggFn`
  runs. The right rule is that `average()`/`max()`/`min()` of a metric's value needs SQL
  whatever the field's type, and only `last_value()` and counter `differences()` have faithful
  builder equivalents.
- **A two-dimension `groupBy` silently truncates the time axis.** 11 series returned 264 rows
  (24 buckets each); 22 series returned **60 rows, 2–3 buckets each**. All the series are
  present and nearly all the buckets are gone, and `query_tiles` reports `ok` either way. The
  Scoreboard tile is SQL with an explicit `LIMIT` for this reason, and the verifier asserts
  `uniqExact(series)` **and** `uniqExact(ts)`.

  **The explicit `LIMIT` then became the same bug again, at a different scale (2026-09-17).**
  `LIMIT 5000` is fine at the hourly granularity the verifier checks — 22 × 24 = 528 rows —
  and truncates at the 5-minute granularity a dashboard actually uses: 22 × 276 = **6,072
  rows, so the chart lost its last 48 buckets.** Nothing in the repo caught it for a day.
  Every value check passed, because every bucket the tile *did* return was correct; the
  structural audit passed, because a `LIMIT` is legitimate syntax; and the verifier passed
  because it asserted at a granularity where the cap does not bite. Raised to `LIMIT 100000`
  and `tilediff.check_row_caps()` now compares each time-series tile's capped row count
  against its uncapped one, estate-wide. Scoped to queries selecting a `ts` column, because on
  a top-N table truncation is the *point*: three `[Logs System]` tiles carry `LIMIT 5` and
  their Kibana panels specify `size: 5`, so flagging those would have produced three false
  positives and trained everyone to ignore the check.
- **A new residue class: the collection gap — and it was claimed on a false premise.**
  `Connections` was recorded as unmigratable because "the standard OTel apachereceiver emits
  no async-connection metric". **It does**: `apache.connections.async` with a
  `connection_state` attribute, *enabled by default*. Reading the receiver's `metadata.yaml`
  on 2026-09-18 settled it in one fetch.

  The data had been there all along — `generate-apache-metrics.py` emits
  `conn_async_{writing,keep_alive,closing}` and the Elasticsearch loader loads them as
  `apache.status.connections.async.*`. Only the ClickStack loader skipped them, on an
  assumption about the target that was never checked against the target. That is the exact
  failure mode this document keeps recording, applied to itself, and it cost one panel for
  two days.

  Fixed: the loader emits the metric, the dashboard has an eleventh data tile (`max`, not
  `avg` — that is the operation the Kibana panel uses), `expect_apache.py` diffs all three
  series bucket-for-bucket, and `verify-apache.sh` asserts the metric's shape so it cannot go
  missing again. Apache is now **0 unmigratable panels**.

  The residue class survives but is **reframed**, because its example was false and its
  replacement nearly was too: `postgresqlreceiver` has no per-statement metrics, but
  `sqlqueryreceiver` scrapes `pg_stat_statements` directly. What is genuinely a collection gap
  is narrower — *the metric is not in the collector's default set*: either optional and off
  (`system.cpu.utilization`; the default is cumulative `system.cpu.time`) or needing a
  receiver nobody configured. Both are answered by collector configuration, not by abandoning
  a panel.
- **The attribute KEYS are not receiver-faithful, and that is still open.** The receiver
  names each old-format attribute after its own metric — `connection_state`,
  `scoreboard_state`, `workers_state` — where this repo uses plain `state` for
  `apache.scoreboard` and `apache.workers`. The new `apache.connections.async` uses the
  correct `connection_state` rather than propagating the deviation, so the apache metrics
  currently carry two conventions. Fixing the other two means rewriting their tiles and their
  22 + 2 expectations; recorded rather than done quietly.

  **Every such deviation across all five integrations is now listed in one place** —
  `../skill/references/integration-to-receiver.md`, "Where this repo's reference stack
  deviates". mysql and system have their own entries there (`mysql.opened_resources`,
  `system.process.*` → `process.*`, `state` values `irq`/`iowait` → `interrupt`/`wait`, and
  others). None of them affects correctness against Elasticsearch — the tiles and the
  expectations agree, and 203 series are diffed bucket-for-bucket. They affect
  **portability**: a customer's collector will not emit these identifiers, so what transfers
  from the postgres, mysql and system tile SQL is the technique, not the names.

- **The reshape lost nothing.** Elastic spreads mod_status across one field per dimension
  value; OTel carries the dimension as an attribute. Eleven `scoreboard.*` fields became one
  `apache.scoreboard` + `state`; five `cpu.*` fields became `apache.cpu.time` + `level` +
  `mode` plus `apache.cpu.load`. Every one of the eleven state averages and all four
  level/mode combinations match Elastic. `scoreboard.total` is not a state — it is
  MaxRequestWorkers (150), the sum of the other eleven, derived rather than faked.

### postgresql — logs and metrics

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Logs PostgreSQL] Overview` | 3 | `[Logs PostgreSQL] Overview (migrated)` | 4 |
| `[Logs PostgreSQL] Query Duration Overview` | 3 | `… Query Duration Overview (migrated)` | 4 |
| `[Metrics PostgreSQL] Database Overview` | 9 | `[Metrics PostgreSQL] Database Overview (migrated)` | 10 |

Performed **2026-09-16**, verified with `./stack/clickstack/verify-postgres.sh` — **21/21**.
The first integration migrated in full with **nothing blocked**: no map, no unknown panel
type, no missing target metric.

Data: `generator/generate-postgres.py` writes all three files — a query log plus the two
pg_stat_* series — and the **metrics are derived from the log**, because pg_stat_statements is
exactly the aggregate of the statements postgres ran. Three invariants tie them together, all
asserted on both platforms:

| invariant | value |
|---|---|
| `sum(final statement.query.calls)` == logged statements | **120,000** |
| `sum(final query.time.total.ms)` == summed log durations | **3,213,228.3 ms** |
| `sum(final transactions.rollback)` == ERROR lines | **83** |

**The two logs dashboards are the cleanest migration in the repo: 6 panels, 8 tiles, all
builder, nothing degraded.** Field mapping is near-identity (`log.level` → `['level']`,
`postgresql.log.database` → `['database']`, and so on).

**The metrics dashboard is the opposite of apache's: 9 SQL tiles, but eight are one template.**
Eight panels share a single Lens formula —
`pick_max(normalize_by_unit(differences(max(X)), unit='s'), 0)`, the per-second rate of a
counter floored at zero. Solved once, applied eight times. Apache's six SQL tiles were six
different problems; postgres's nine are two.

Four things it added to the record:

- **A third realistic target shape for metric names.** apache emits the standard
  apachereceiver's model; postgres does not, because `postgresqlreceiver` has **no
  per-statement metrics at all**. A customer shipping those numbers uses the generic
  `sqlqueryreceiver`, whose metric names are **whatever the operator's SELECT aliased the
  columns to** — so the mapping here is near-identity, and that is realistic rather than lazy.

  **Verified 2026-09-18, and one claim corrected.** `sqlqueryreceiver` does emit metrics from
  arbitrary SQL — `value_column` for the value, `attribute_columns` for the dimensions, with
  `data_type`, `monotonic` and `aggregation` all settable — and supports postgres and mysql.
  So the modelling choice above stands. But this section also used to say the receiver "covers
  pg_stat_database only partially", implying most of the dashboard had no target. That
  overstates it: `postgresql.tup_{fetched,returned,inserted,updated,deleted}` exist as optional
  per-database metrics, as do `postgresql.commits`, `postgresql.rollbacks`,
  `postgresql.deadlocks` and `postgresql.query.conflicts`. The real per-tile verdict, for the
  nine data tiles:

  | tiles | verdict |
  |---:|---|
  | 4 | map onto `postgresqlreceiver` cleanly — Database Transactions, Rows Fetched/Returned, Rows Inserted/Deleted/Updated, Conflict/Deadlock Rates |
  | 2 | **degrade** — Local and Shared block cache stats become per-database (`postgresql.blks_hit`/`blks_read`), losing the per-query dimension |
  | 3 | need `sqlqueryreceiver` — Top Queries, Query Latency, Fileblock IO (nothing exposes `blk_read_time`/`blk_write_time`) |

  The metric names here were **deliberately not renamed** to receiver names. They are a
  faithful `sqlqueryreceiver` shape and that receiver has no canonical names to rename *to*.
  What was missing was stating the model where a reader would see it, plus two cautions:
  `sqlqueryreceiver` metrics are **alpha**, so "achievable" is not "production-ready"; and in a
  real config alias the database column to **`db.namespace`** rather than `database`, so one
  dashboard control can filter both this family and any `postgresqlreceiver` metrics. Full
  per-integration table: `../skill/references/integration-to-receiver.md`.
- **`seriesType: bar` on an `lnsXY` with a date_histogram is a *time-series* bar.** It maps to
  ClickStack `stacked_bar`; `bar` is categorical and the tile fails schema validation for want
  of a `groupBy`. Only `bar_horizontal` means a categorical bar. This was a genuine bug in
  `inventory-panels.py`'s chart mapping, found by the tile refusing to save, and fixed.
- **Kibana's exclusion filter is exact and case-sensitive.** Both query-level panels carry
  `not query.text : ("BEGIN;" or "begin" or "commit" or …)`. On a keyword field that removes
  `SELECT * FROM pg_stat_statements` but **not** this corpus's `BEGIN`/`COMMIT`, because the
  list spells them `BEGIN;` and `commit`. The migrated tiles reproduce that exactly — a
  faithful port of a filter that only works if your application emits those precise spellings.
- **`query_text` as a dimension is a cardinality risk.** 16 distinct queries here; a real
  `pg_stat_statements` holds thousands, and the measured builder-tile limit is between 11 and
  22 series before the time axis truncates. Both query-level tiles are `sql` with explicit
  `LIMIT`s for that reason as much as for the counter.

### mysql — logs and metrics

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Logs MySQL] Overview` | 6 | `[Logs MySQL] Overview` | 7 |
| `[Metrics MySQL] Database Overview` | 16 | `[Metrics MySQL] Database Overview` | 17 |
| `[Metrics MySQL] Replica Status` | 4 | `[Metrics MySQL] Replica Status` | 5 |

Performed **2026-09-16**, verified with `./stack/clickstack/verify-mysql.sh` — **36/36**, plus
a bucket-for-bucket diff of **all 42 tile series** against Elasticsearch over an absolute 23h
window. Nothing blocked; 26 panels → 26 tiles + 3 provenance notes.

Three corrections the user caught by eye after the first numeric pass was green, all of them
*shape* errors that bucket-for-bucket diffing cannot see either:

| what was wrong | why | fix |
|---|---|---|
| `Source overview` returned **1 row** | the Kibana panel is a saved **search** — a document table, one row per scrape, newest first. The tile `GROUP BY`-ed the identity tuple, which is constant, collapsing 2,760 rows into one | row-per-scrape `ORDER BY TimeUnix DESC LIMIT 500`, with `file_info` rebuilt as `concat(binary_log_file, ' ', position)` |
| 4 tiles were `line` | **every** XY panel on `Database Overview` is `seriesType: bar_stacked` — a *time-series* stacked bar — and `inventory-panels.py` correctly said `stacked_bar` for all 15. I overrode it by hand on Connections, Thread Activity and the two Buffer Pool ratios | all 15 are `stacked_bar`; `verify-mysql.sh` now asserts the whole display-type set |
| `SQL thread delay` plotted nothing | not a tile bug — `thread.sql.delay.sec` was a constant **0** in all 2,880 generated docs, so both stacks agreed perfectly and both charts were empty. A panel that cannot render is a hole in the corpus | the replica is now a **delayed replica**: `SOURCE_DELAY=30` between 02:00 and 05:00 (360 scrapes), and `seconds_behind_source` carries the delay plus its real lag, so the ceiling moved 7 → 37 |

The last one is the one worth generalising: **a tile that matches the source perfectly can
still be worthless if the underlying field is constant.** Verification compares two platforms;
it never asks whether the panel has anything to show.

Data: `generator/generate-mysql.py` writes four files — a slow log, an error log, and the
`status` and `replica_status` series. A CMS on `mysql-primary-01` with a replica, databases
`cms` and `sessions`, deliberately not the shop nginx and apache serve. Statements are
generated once and both the log and the counters are read off them, so they cannot disagree:

| invariant | value |
|---|---|
| `final questions` == `final sum(command.*)` == statements generated | **120,000** |
| slow-log records == statements slower than `long_query_time` (1.0 s) | **2,513** |
| buffer-pool miss rate, `pool.reads / read.requests` | **0.0500 %** |

Four things this migration established that the other three did not.

#### 1. The first multi-line log format

A slow query is five lines and the integration's grok wants all five in one `message`. Read
line by line you get five documents per query, four of them empty of any query — **and
nothing errors**, because each of those lines is a valid log line. The symptom is a row count
five times too high (12,565 instead of 2,513). Both loaders split on the `# Time: ` header:
`read_records()` in `load-to-datastream.py`, `multiline.line_start_pattern` in
`otel-collector-mysql.yaml` — on the slowlog receiver only, which is why the error log is a
second receiver rather than another `include:` entry.

`SET timestamp=<epoch>` is the authoritative timestamp, **not** the `# Time:` header. Measured
against `_simulate`: make the two disagree and `@timestamp` follows `SET`; remove `SET` and
`@timestamp` is never set; remove `# Time:` and nothing changes. Being whole seconds, it also
makes both stacks compute a byte-identical shift — apache's property, for a different reason.

#### 2. Sum vs Gauge is decided by the PANEL, not by Elastic's metric type

This is the most transferable finding here. HyperDX de-cumulates every Sum, so the question
for each field is not "is it a counter?" but "does the source panel read its absolute value or
its difference?" Five fields the integration types `counter` therefore ship as **gauges**:

| field | Elastic type | panel does | shipped as |
|---|---|---|---|
| `status.max_used_connections` | counter | `max()` | gauge — a high-water mark, not a rate |
| `status.innodb.buffer_pool.pool.reads` | counter | `max()` | gauge — half of a lifetime ratio |
| `status.innodb.buffer_pool.read.requests` | **gauge** | `max()` | gauge — the other half |
| `replica_status.source.log_position.read` | counter | `last_value()` | gauge — a binlog offset |
| `replica_status.source.log_position.exec` | counter | `last_value()` | gauge — ditto |

and one that looks like a gauge ships as a **Sum**: `cache.ssl.size` is a constant 128 whose
panel `differences()` it, so Kibana plots a flat zero. As a gauge it would have plotted 128 and
disagreed with the source without failing anything.

#### 3. A gauge's samples cannot be aggregated inside a bucket by a builder tile

Measured, and not documented anywhere before this migration: on a metric source HyperDX
collapses a gauge to **one sample per bucket — the last** — *before* `aggFn` is applied. For a
single series in a bucket whose samples give max=14, avg=10.8, last=10:

```
aggFn = max | min | avg | sum | last_value   ->   10, 10, 10, 10, 10
```

All five agree, and all five are wrong for four of Kibana's panels. `aggFn` aggregates across
*series* (and, in a `number`/`table` tile, across buckets), never across the samples within a
bucket. So `average(mysql.status.open.files)` and `max(mysql.status.threads.connected)` are
**not builder shapes**: Open Tables/Files/Streams, Thread Activity, Buffer Pool Pages and
Connected Threads are all SQL tiles, and `verify-mysql.sh` asserts they stay that way.

This is narrower than the Sum rule but bites the same way — `query_tiles` returns `ok` with a
plausible row count either way. It also means the earlier note that "de-cumulation is
Sum-only, so gauges are safe" is true *only* of de-cumulation.

#### 4. One attribute that moves with the value destroys series identity

`source.file_info` is `"<binlog file> <byte position>"`, so attaching it to each replica data
point gave **2,879 distinct series for one replica** instead of 1. Nothing errors; the series
simply fragment, and since a gauge is then aggregated across series after being collapsed by
last-value, every tile read an arbitrary point per bucket. It showed up as `last_value()`
panels disagreeing with Kibana by a few thousand bytes — a plausible-looking wrong number.
The identity attributes that *are* constant (host, port, server id/uuid, binlog file, replication
user) stay; `file_info` is reconstructed in the Source overview tile's SQL. `verify-mysql.sh`
asserts `uniqExact(Attributes) = 1` per replica metric.

#### The wrong field, and a check that could not see it

**Reported by comparing the two dashboards.** `Top processes by CPU usage` didn't match.

The panel aggregates **`process.cpu.pct`**. The tile read
**`system.process.cpu.total.norm.pct`**. metricbeat normalises the second by core count and
not the first, so the tile was wrong by exactly the host's core count — 4x on `docs-web-01`,
8x on the edge nodes, 16x on `mysql-primary-01`:

| process | avg(process.cpu.pct) | avg(…norm.pct) | ratio |
|---|---:|---:|---:|
| mysqld | 0.817631 | 0.075525 | 10.8x |
| nginx | 0.261700 | 0.032706 | 8.0x |
| httpd | 0.132165 | 0.033044 | 4.0x |

(The ratios are blended because a process spans hosts with different core counts.)

**It verified green, and the reason is worth more than the bug.** The harness derived its
Elasticsearch expectation from *the field the tile read* — so it compared the tile against
itself. That passes for any field the tile happens to pick. The expectation has to come from
the source panel's aggregation list, which `inventory-panels.py` already prints. Two fields
differing only by `norm` are the easiest pair in observability to swap, and nothing about the
result looks wrong: same shape, same ranking, same units, plausible magnitude.

Fixed: the ClickStack loader now ships `system.process.cpu.pct` (named for the source field
rather than the misleading `pct_of_host` I first used), the tile reads it, and
`verify-system.sh` asserts the tile names the panel's field and *not* the normalised one.

#### `last_value` was ambiguous in four panels, on both platforms

Chasing the above surfaced a second problem in the same tables. `last_value(f)` grouped by one
field, over data carrying more, is not a well-defined number: Kibana's `top_metrics` sorts on
the time field only, so among documents sharing the newest timestamp it returns one
**arbitrarily**. `nginx` runs on three hosts and had three values at the last scrape
(0.119 / 0.140 / 0.152) — Kibana returned web-edge-02's, ClickHouse's `argMax` returned
web-edge-03's. Neither is reproducible.

All four affected tables now take the value **at** the newest scrape, averaged over the
collapsed dimension — deterministic, and the fleet reading their titles imply. The average
columns match Elasticsearch exactly; the last columns are a stated deviation.

**A placeholder also shipped.** `Top Hosts by CPU` had `any(0) AS "_"` where its second
column belonged — I built the tile around one of the panel's two `last_value` columns and left
a stub for the other. It now returns both CPU and memory.

#### `seriesLimit` emptied a chart again, and nothing could catch it

**Reported by looking at the dashboard, after every automated check was green.**
`Syslog events by hostname` rendered blank. `seriesLimit: 5` ranks series over
**unconditional** volume, ignoring the select-item `where` that scopes the tile — and the top
`host.hostname` bucket across `otel_logs` is the **empty string with 1,399,267 rows**, because
none of the other four services carries that attribute. The five real hosts were crowded out.

This is the **second** time this trap has fired here (nginx logs, 2026-09-16 was the first),
and the skill already said outright not to set it when a select-item `where` provides the
scope. I set it on seven tiles anyway.

What makes it worth a structural rule rather than care is that **no verification path
reproduces it**:

| check | result |
|---|---|
| `clickstack_query_tiles` | `status: ok`, `hasData: true` |
| full-distribution diff vs Elastic | 13/13 pass |
| re-issuing the tile through `clickstack_table`/`timeseries` | perfect — **the builder tools have no `seriesLimit` parameter**, so the method that catches everything else cannot express the setting that breaks the tile |

All seven tiles had it removed; five of them were exactly equivalent without it (the real
cardinality is ≤5), and two — *New users over time* and *New groups over time* — now show all
12 series where Kibana's `size=5` shows five. Kibana's five are arbitrary there anyway: every
user has exactly one event, so the top-5 is a tie broken at random.

`hdx-objects.py` gained a `serieslimit:<dashboard>` verb and `verify-system.sh` asserts
every dashboard reports nothing, which is the only defence available.

**A read-back trap found while building that guard:** a `pie`/`bar` tile's `limit` is persisted
in the dashboard document under the name **`seriesLimit`**. MCP's `get_dashboard` hands it back
as `limit`; the HyperDX REST API does not. So an unscoped audit flags every pie and bar as a
violation — the guard scopes by `displayType`, since only `line`/`stacked_bar` carry the
dangerous meaning. Same class as the `where` → `aggCondition` mismatch: what you save is not
what you read back.

#### Also worth recording

- **A builder `table` tile takes no row limit.** Only `line`/`stacked_bar` have `seriesLimit`
  and only `pie`/`bar` have `limit`. The Kibana `Top slowest queries` datatable is
  `terms(query) size=5`, so there is no builder that can cap it — it is a SQL tile with
  `LIMIT 5`.
- **`clickstack_query_tiles` is a smoke test, not verification.** It returns `status`,
  `hasData` and `rowCount` — no values. Every numeric claim above came from running the tiles'
  stored `sqlTemplate`s and re-issuing the builder tiles through `clickstack_timeseries`.
- **`clickstack_get_dashboard` takes `id`, not `dashboardId`.** Passing the wrong key does not
  error — the tool falls back to listing every dashboard, which reads downstream as "this
  dashboard has no tiles".
- **Two edge buckets differ on `increase` tiles, and both are platform behaviour.** `increase`
  reads the counter from before `startTime`, so the leading bucket has a value where Kibana's
  `differences()` nulls it; and the builder emits one bucket *at* `endTime` fed by data past
  the window. Every bucket in between matches exactly.
- **Both platforms round seconds→nanoseconds differently.** `Query_time` is logged in seconds
  and both stacks convert to ns in float64: Elastic disagrees with the file on 11 of 2,513
  records, ClickStack on 68, always by exactly ±1 µs on values of tens of seconds. Visible only
  as a 0.001 ms difference on a millisecond-scale panel.

### system — logs and metrics (Linux)

| dashboard | panels | ClickStack object | tiles |
|---|---:|---|---:|
| `[Logs System] Sudo commands` | 4 | same name | 5 |
| `[Logs System] Syslog dashboard` | 4 | same name | 5 |
| `[Logs System] New users and groups` | 7 | same name | 8 |
| `[Logs System] SSH login attempts` | 6 | same name | 7 |
| `[Metrics System] Host overview` | 29 | same name | 30 |
| `[Metrics System] Overview` | 10 | same name | 11 |

Performed **2026-09-17**, verified with `./stack/clickstack/verify-system.sh` — **44/44**,
plus full-distribution diffs of the four log dashboards (13/13) and bucket-for-bucket diffs of
the two metrics dashboards (20/20). The **widest** integration here: 10 data streams, more
than the other four combined.

Data: two new generators. `generate-system-logs.py` writes a syslog and an auth log (40,000 +
16,224 lines); `generate-system-metrics.py` writes **eight** metric streams (108,000 records,
396,000 OTel points) for the same five machines the rest of the corpus describes — the three
nginx edge nodes, an apache docs node and the mysql primary. So `system` monitors the fleet
the other four integrations serve rather than being a disconnected fifth dataset.

| invariant | value |
|---|---|
| `cpu.total.norm.pct` == sum of the six component percentages == `1 - idle` | exact, every scrape |
| `fsstat.total_size.used` == sum of per-mount used bytes | **1,536,275,774,361,037** |
| ssh `Accepted` / `Failed` / `Invalid` | 2,561 / 7,487 / 3,952 |
| `useradd` lines == `groupadd` lines | 12 == 12 |

#### Two pipeline behaviours that are opposite, inside one integration

Measured against `_simulate`, and the reason the loader has to do two things at once:

- **`logs-system.syslog` re-derives `@timestamp` from the message text**, discarding the value
  on the document.
- **`logs-system.auth` keeps the document's `@timestamp`** — its date processors are gated on
  `ctx['@timestamp'] == null` — and only falls back to the text otherwise.

So `shift_syslog_line` rewrites the timestamp *inside the line* **and** returns an ISO for the
document. Getting only one right breaks one stream while the other looks perfect. Note also
that syslog format carries **no year**: every reader has to supply one, and Elastic Agent,
Filebeat and the OTel stanza parser all default to the current year — which is why a syslog
archive backfilled across a New Year boundary is misdated by every tool involved.

#### `look_back_time` cannot be a constant

The first load of the metrics rejected **4,930 of 7,200** cpu documents and the same
proportion of the other seven streams. Cause: TSDB's write index refuses any timestamp older
than `look_back_time` before *now*, the loader hardcoded `30h`, and by 2026-09-17 this static
corpus began **46 hours** in the past. A value that was correct on 2026-09-16 silently
rejected two thirds of the data a day later. It is now derived from the data's own age
(`oldest + margin`, floored at 30h), which is the only form that keeps working as the corpus
ages. Both metric loaders also gained `--part`, so one stream can be regenerated and reloaded
without touching the other seven.

#### 37 of the 50 data tiles are SQL — and the split by signal type is total

50 data tiles for 50 data panels (plus 4 migrated nav panels, 4 section headers and 6
provenance notes). The SQL share is not spread evenly; it is almost perfectly segregated:

| | data tiles | SQL | builder |
|---|---:|---:|---:|
| the four **logs** dashboards | 17 | 4 | **13** |
| the two **metrics** dashboards | 33 | **33** | 0 |

**Every single metrics data tile is raw SQL, and 13 of 17 logs tiles are builder tiles.** That
is the sharpest confirmation yet of the pattern every migration here has shown: log dashboards
translate into the builder, metric dashboards do not. Five reasons, and a metrics panel
usually hits more than one at once:

| reason | tiles |
|---|---|
| a gauge's samples cannot be aggregated inside a bucket by a builder tile | the CPU/memory/load/filesystem charts and tables |
| `counter_rate()` over a cumulative Sum | Rate of disk IO, Network traffic (bytes), Network traffic (packets) |
| **the field has no OTel equivalent** and is derived on the target | `system.cpu.total.norm.pct` (hostmetrics has no `total` state — it is the sum of the six non-idle states) and `system.fsstat.total_size.*` (a Beats rollup — the sum of the per-mount filesystem numbers) |
| `terms(field) size=N` on a datatable, which a builder `table` cannot cap | Top sudo commands, New users, New groups, SSH failed users |
| the two degraded heatmaps | Top hosts by CPU / memory over time |

#### Three deviations forced by the source

1. **`reducedTimeRange='30s'`** on six network-throughput numbers asks for a 30-second window.
   This corpus scrapes every 60s, so that window holds at most one sample and `max - min` is
   **0 — in Kibana too**. The tiles use the newest two scrapes and divide by the real gap.
   With metricbeat's default 10s network period the two agree exactly.
2. **`last_value()` with no breakdown returns one arbitrary host** of the five — it is a
   `top_hits` of size 1, and which document wins is undefined. The number tiles take the
   newest scrape and average across the fleet, which is reproducible and is what a fleet gauge
   means.
3. **`static_value` reference lines are dropped.** Every gauge-style panel carries one as a
   second series; ClickStack's `number` has no reference line.

#### Also worth recording

- **`geo_country_code` answers `ZZ`, not empty, for a row with no IP.** The column is
  materialized from `LogAttributes['remote_addr']`, an empty string casts to `0.0.0.0`, and
  DB-IP genuinely maps that to `ZZ`. 42,224 system rows carry no IP at all, so the SSH country
  tile must exclude `('', 'ZZ')` or they dominate the chart. Free geo for the SSH panels came
  from emitting the source IP under the existing `remote_addr` key — the same trick the apache
  collector uses.
- **`scaled_float` silently destroys small values.**
  `system.process.cpu.total.norm.pct` is `scaled_float(scaling_factor=1000)`, so Elastic stores
  three decimals: `cron` averaging 9.3e-05 is stored as **exactly 0.0** there while ClickHouse
  keeps the Float64. The "Top processes by CPU" comparison therefore allows half a quantum
  (5e-4) in absolute terms. Irreducible source precision, not a migration error.
- **A generator bug the verifier caught**: the six CPU components were drawn independently and
  could sum past 100%, so 111 of 7,200 scrapes reported an impossible CPU and `total != 1 -
  idle`. They are now scaled back proportionally, with the rounding residual taken off the
  largest component — the identity and the physical bound both hold exactly.

## Parsed only

Read with `inventory-panels.py`, never written to ClickStack. Shapes, per integration:

- **system (Windows)** — 4 Windows Security dashboards + `[System] Windows Overview`; they
  read Windows event codes this corpus has no analogue for, so they need a third generator
  rather than a translation pass
- **kubernetes** — all metrics; the widest field surface of the estate (166 fields)

Largest single dashboards: `[Metrics System] Host overview` (29 panels),
`[System Windows Security] User Management Events` (27), `[Metrics Kubernetes] Scheduler` (22).

<details>
<summary>All 20 parsed dashboards</summary>

**kubernetes** — API server (5) · Cluster Overview (16) · Controller Manager (17) · Cronjobs (3) ·
DaemonSets (6) · Deployments (6) · Jobs (6) · Nodes (10) · PV/PVC (3) · Pods (9) · Proxy (14) ·
Scheduler (22) · Services (2) · StatefulSets (7) · Volumes (4)

**system (Windows only — the six Linux dashboards are migrated)** —
`[System Windows Security] Failed and Blocked Accounts` (12) · `… Group Management Events` (21) ·
`… User Logons` (13) · `… User Management Events` (27) · `[System] Windows Overview` (7)

</details>

## Why the rest stop at parsing

Three reasons, and only the first is about effort.

**1. There is no data for them.** `otel_logs` holds 1,455,491 rows — 1,011,404 nginx,
264,662 apache, 120,421 postgresql, 56,224 system and 2,780 mysql — plus 843,006 OTel metric
points. ClickStack has no Windows event data and no `kubernetes.*` data at all. Tiles translated today
would point at sources that do not exist: they would render empty and verify against nothing. Migrating any of these starts with ingesting its data,
not with translating panels — which is exactly what apache's migration turned out to be, with
the generator and the second loader path accounting for most of the work and the seven tiles
for very little of it.

**2. Most of them are metrics dashboards, which is a different translation path** — though no
longer an unproven one. `[Metrics Nginx] Overview` was migrated on 2026-09-16 and is the
reference for it; see [nginx — metrics](#nginx--metrics). What that exercise established is
that the difference is deeper than `metricName`/`metricType` on each select item: ClickStack
de-cumulates a cumulative Sum *before* aggregating, so a Kibana panel charting a raw counter
has no builder equivalent at all. Of the 210 still-parsed panels the majority are metrics
panels, and `inventory-panels.py --triage` now flags that case per panel.

**3. The panel inventory was the point.** These were installed to test
`inventory-panels.py` against panel shapes nginx does not contain. It worked: 334 panels
classified with zero unknowns, and the exercise found three real parser bugs —

- by-value legacy visualizations live at `embeddableConfig.savedVis`, not
  `attributes.visState` (**51 of 334 panels**, invisible to a `visState`-only reader)
- TSVB is a family, not a chart type: all **11** TSVB panels here are markdown-mode text
  panels carrying a vestigial `count` series, and would have migrated as line charts of a
  meaningless number
- `lnsTagcloud` was unmapped

## Unmigratable panels

**5 of 347 panels across all seven integrations**, and now **two** distinct causes rather
than one. Both are rendering-layer gaps: the data migrated perfectly and the target has
nowhere to draw it.

| integration | dashboard | panel | cause |
|---|---|---|---|
| nginx | `[Logs Nginx] Overview` | `Nginx logs` | no map tile type — migrated as a country `bar` |
| apache | `[Logs Apache] Access and error logs` | `Unique IPs map` | no map tile type — migrated as a country `bar` |
| system | `[Logs System] SSH login attempts` | `SSH failed login attempts source locations` | no map tile type — migrated as a country `bar` |
| system | `[Metrics System] Overview` | `Top hosts by CPU usage over time` | **no categorical heatmap** — migrated as a line chart grouped by host |
| system | `[Metrics System] Overview` | `Top hosts by memory usage over time` | **no categorical heatmap** — migrated as a line chart grouped by host |

**The second cause was found on 2026-09-17 and is new.** ClickStack *has* a `heatmap`
displayType, so `inventory-panels.py` called both panels "ready" — but it is a
**value-distribution** heatmap: exactly one series, a numeric `valueExpression` bucketed
against time, and **no `groupBy` at all** (read off the live `save_dashboard` schema). It does
what a trace-latency heatmap does. Kibana's two panels put `terms(host.name)` on the y-axis,
which has no equivalent. `--triage` now flags any heatmap carrying a `terms()` breakdown.

Both migrated maps took the same degradation, decided independently and landing in the same
place — which is a reasonable sign that a country `bar` is simply what a map becomes here.

Everything else has a target chart type, though some degrade in appearance (tag cloud → table,
gauge → number, area → line). That the only hard loss across 347 panels is the map — the
conclusion `MIGRATION.md` reached from nginx alone — now holds at 30× the sample.

## Reproduce this inventory

```bash
SKILL=../skill      # vendored in this repo; see skill/SKILL.md

# what is installed
curl -s -u elastic:changeme "http://localhost:5601/api/fleet/epm/packages?prerelease=false" \
  -H 'kbn-xsrf: true' > /tmp/pkgs.json
python3 -c 'import json; d=json.load(open("/tmp/pkgs.json")); print(sorted(i["name"] for i in d["items"] if i.get("status")=="installed"))'

# install another integration (dashboards only; no data required to read them)
curl -s -u elastic:changeme -X POST "http://localhost:5601/api/fleet/epm/packages/redis" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' -d '{"force":true}'

# list, export, inventory
export KIBANA_URL=http://localhost:5601 KIBANA_USER=elastic KIBANA_PASS=changeme
"$SKILL"/scripts/export-dashboards.sh --all-spaces
"$SKILL"/scripts/export-dashboards.sh <id>... > d.ndjson
python3 "$SKILL"/scripts/inventory-panels.py d.ndjson            # markdown table
python3 "$SKILL"/scripts/inventory-panels.py d.ndjson --fields    # fields to map
```

The counts above are for the **default** space, which is all this stack uses. On a real
deployment use `--all-spaces`: saved objects are space-scoped, and an unscoped call returns
an empty list for a Kibana whose dashboards live elsewhere — which reads as "nothing to
migrate" rather than as a scoping mistake. Validated here by copying a dashboard into a
second space and confirming the unscoped listing could not see it.

Installing an integration is cheap and reversible — it adds saved objects and index templates,
no data. It is the honest way to check what a migration would involve before committing to it.

## A whole feature class was missing: dashboard controls

Found 2026-09-17 when the `[Metrics PostgreSQL] Database Overview` `database` dropdown showed
nothing where Kibana offers `analytics`, `postgres`, `shop`.

A Kibana dashboard can carry a **control bar** — `controlGroupInput`, a row of field-bound
dropdowns that filter every panel. It lives outside `panelsJSON`, so a panel inventory never
mentions it, and `inventory-panels.py` explicitly skipped it as "not data". **21 of the 37
dashboards here have one, and 7 of the 17 migrated dashboards had lost it.**

It maps one-to-one onto ClickStack's dashboard-level `filters`, so this was never a
degradation — just a dropped feature:

A Kibana control resolves its options against the **data view it is bound to**, and every one
of these is bound to the broad `logs-*` or `metrics-*` pattern rather than to the
integration's own index pattern. So the option list is *not* scoped to the dashboard, on
either platform — which is what makes the two columns comparable at all:

| dashboard | Kibana control | migrated to | Kibana | ClickStack |
|---|---|---|---:|---:|
| `[Metrics PostgreSQL] Database Overview` | `postgresql.database.name` | `Attributes['database']` | 3 | 3 |
| `[Metrics Nginx] Overview` | `host.hostname` | `ResourceAttributes['host.name']` (sum) | 8 | 8 ‡ |
| `[Metrics Apache] Overview` | `host.hostname` | `ResourceAttributes['host.name']` (sum) | 8 | 8 ‡ |
| `[Metrics MySQL] Replica Status` | `service.address` | `ResourceAttributes['host.name']` (gauge) | 8 | 8 ‡ |
| `[Logs Nginx] Overview` and `… Access and error logs` | `host.hostname` | `LogAttributes['hostname']` | 5 | 5 |
| `[Logs Apache] Access and error logs` | `host.hostname` | `LogAttributes['hostname']` | 5 | 5 |

An earlier version of this table reported 3/3/2/1/3/0 and claimed the nginx and apache logs
controls were "empty in Kibana". **Both claims were wrong**, and wrong in the same way: the
counts had been derived from the integration's own data (how many hosts emit nginx metrics)
instead of from what the dropdown actually resolves (every host in `metrics-*`). Resolving
all six on both platforms is one query and it contradicted the table.

‡ **The three metric counts agree and the three sets do not** — the kind of match that a
count-based check passes and a set-based check fails. A filter reads exactly one metric table,
chosen by `sourceMetricType`, and the dataset is split across them: `otel_metrics_sum` has no
`mysql-replica-01` (replica status is all gauges) and `otel_metrics_gauge` has no
`pg-primary-01`. Each therefore resolves **seven** real hosts where Kibana's `metrics-*`
resolves eight, and each is padded back to eight by the spurious `d40a066c0be9` described
below. Two errors cancelling in the total is the argument for diffing sets, not lengths.

Three further consequences, none of them a translation error:

- **`service.address` is a different *kind* of value.** Kibana's MySQL Replica control offers
  `mysql-replica-01:3306`, `http://web-edge-01/nginx_status`, `pg-primary-01:5432` — endpoint
  addresses across all four integrations. There is no such attribute on the ClickStack side,
  so the control was mapped to `host.name`: same cardinality, different labels. The nearest
  honest equivalent, and the one row where the option *labels* differ in kind rather than the
  membership differing.
- **Both platforms ship a control that blanks its own dashboard.** `host.hostname` is set on
  `system.syslog` and `system.auth` **only** — all five values come from the system
  integration, and nginx/apache/postgres/mysql log documents have no host field at all. So
  picking any option on the Kibana nginx-logs control returns zero documents. The migration
  reproduces the control faithfully, including that.
- **Except ClickStack's is partly usable, which Kibana's is not.** `access_json` carries
  `hostname` for all 499,964 rows (`web-edge-01..03`), because the OTel receiver reads it from
  the JSON payload where Elastic's grok never captured it. So three of the five options do
  filter nginx logs here. A divergence in ClickStack's favour, recorded rather than removed.

### The three metric dropdowns carry one host that exists on neither side

Found 2026-09-17 on a retest, by listing what each dropdown actually resolves to instead of
only asserting that it resolves to *something*. Elastic's `metrics-*` offers eight hosts;
ClickStack offers those same eight **plus `d40a066c0be9`** — on `[Metrics Nginx] Overview`,
`[Metrics Apache] Overview` and `[Metrics MySQL] Replica Status`.

It is the ClickStack container's own id. The all-in-one image self-monitors: HyperDX's OpAMP
remote config injects a `prometheus` receiver into the **metrics** pipeline, scraping the
bundled collector's `:8888` and ClickHouse's endpoint, so `otel_metrics_{gauge,sum}` holds
3,510 `otelcol_*` / `scrape_*` / `promhttp_*` / `up` rows tagged with the container hostname
as `host.name`. Elastic has no counterpart: Beats' own telemetry goes to a monitoring cluster,
not into `metrics-*`.

Three things follow, and only the first is a defect:

- **The dropdown is populated from the whole source table, and the filter schema has no way to
  scope it.** A `QUERY_EXPRESSION` filter is `{name, expression, sourceId, sourceMetricType}` —
  there is no predicate field, so the value list is `SELECT DISTINCT <expression>` over every
  row of the table. Nothing in the migration can narrow it.
- **Cross-integration hosts in those dropdowns are *faithful*, not a bug.** `pg-primary-01` on
  the nginx dashboard looks wrong and is correct: Kibana's controls bind to the broad
  `metrics-*` / `logs-*` data views, not to the integration's own index pattern, so Kibana
  offers all eight hosts too. This was one query away from being "fixed" into a divergence.
- **The log-source dropdowns are exact.** `logs-*` yields the same five hosts on both
  platforms, because no self-telemetry lands in `otel_logs`.

Not worth working around. Every tile and every verifier already scopes by `MetricName`
(checked: zero SQL tiles touch `otel_metrics_*` without a `MetricName` predicate, and no tile
references a metric name outside the six dataset prefixes), so the rows are inert everywhere
except the dropdown. Deleting them is pointless — the scrape re-adds them every 30 s.

The general lesson is the same one the `sourceMetricType` bug taught, one level further in:
**asserting a dropdown is non-empty is not asserting it is right.** The earlier fix made three
dropdowns go from zero values to some values, and "some" read as done.

### The actual bug was one missing field

Three of those filters already existed from the original migrations. They rendered empty
because they lacked **`sourceMetricType`**, which picks the metric table the dropdown reads
from. The schema documents it as *"required only when `sourceId` is a Metric source"* — so it
is **not** in the top-level `required` list, and a metric-source filter without it validates,
saves, and silently shows nothing.

`scripts/audit-tiles.py` now checks both, mutation-tested: dropping a control fires, and
removing `sourceMetricType` fires. Detecting "is this sourceId a metric source" needs **both**
signals — a builder tile's `select[].metricType` *and* `otel_metrics_` in a SQL tile's
template — because metric dashboards here are overwhelmingly SQL (9 of 10 on the postgres
one), and the builder-only check found nothing and never fired.

## Closing the value gap: 200 tile series, bucket for bucket

Until 2026-09-17 `verify-tiles-vs-elastic.py` covered **mysql only** — 42 series. Everything
else was verified by totals and distributions, which is a weaker claim than it sounds: a
series can be the right shape, agree on its total, and be wrong in every bucket. Extending it
to the other four integrations added **158 series** and found three defects in migrations that
were already green.

| integration | series | logs / metrics | what extending it found |
|---|---:|---|---|
| mysql | 42 | 0 / 42 | nothing — it was written here |
| nginx | 23 | 12 / 11 | `Heartbeat / Up` under-counted hosts |
| apache | 54 | 11 / 43 | `Scoreboard` truncated at its own row cap |
| postgres | 38 | 7 / 31 | nothing wrong; two source-precision limits identified |
| system | 43 | 11 / 32 | nothing wrong; coverage widened over the throwaway harness |
| **total** | **200** | **41 / 159** | |

The mysql logs dashboard contributes no series: its six tiles are `search` and `terms` panels,
asserted by `verify-mysql.sh` instead.

**`system` was ported last, on 2026-09-17, and porting it widened the coverage.** The
harnesses the system migration had actually been verified with only ever existed outside the
repo, and they checked 33 comparisons where the ported version checks 43 series: the
throwaway code checked only ONE side of each bidirectional counter (`read bytes/s` but not
the negated `write bytes/s`; `In (bytes)` but not `Out (bytes)`) and covered only one of the
two degraded heatmaps. Both gaps were in the series a person had already eyeballed — which is
the pattern this whole document keeps rediscovering. All five of its mutation tests fire,
including the historical wrong-field bug: swapping `process.cpu.pct` for the core-normalised
field turns a check red, where the original harness passed it by deriving its expectation
from whatever the tile read.

The harness was split into `../verify/tilediff.py` (machinery),
`../verify/expect_{mysql,nginx,apache,postgres,system}.py` (expectations) and
`../verify/verify-tiles-vs-elastic.py` (entry point, takes integration names) — and then
moved out of this directory entirely, because it is generic: every endpoint and credential
comes from the environment via `../verify/conf.py`, so the same harness verifies a customer's
own migration. See the [repository README](../README.md).

### Three findings

- **`count_distinct` on a metric source under-counts.** nginx `Heartbeat / Up` returned **2**
  in 2 of 277 buckets where ClickHouse plainly held three hosts × ten points × ten distinct
  values. Same family as the gauge collapse — an `aggFn` on a metric source does not run over
  the raw rows — and the same fix the other seven tiles on that dashboard already had: SQL.
  The general rule tightens to: **on a metric source, only `last_value()` and counter
  `differences()` are faithful builder shapes.**
- **A tile can truncate itself.** See the `LIMIT 5000` note above.
- **`clickstack_timeseries` caps its rows and exposes no limit or pagination parameter.** A
  grouped nginx tile came back with **107 of ~1,100 rows**, which looks exactly like an empty
  chart. Grouped builder tiles are therefore *compiled* to SQL from the tile's own
  `aggFn`/`where`/`groupBy` — still derived from the live tile, so a hand-written
  "equivalent" cannot launder a misunderstanding into agreement.

### Two source-precision limits, measured rather than assumed

Both are Elasticsearch storage decisions, not migration errors, and each yields a **derived**
tolerance rather than a fitted one:

| what | measurement | consequence |
|---|---|---|
| log timestamps | **all 499,964 `nginx.access` documents have millisecond == 0** | the integration parses the combined log's second-resolution `time_local`; the target keeps the millisecond. Events within a second of a bucket edge land in different 5-minute buckets, so 227 of 276 buckets differ while the totals are identical (499,964 either way) |
| `scaled_float` / `float` | `apache.status.cpu.*` is `scaled_float(1000)`; `postgresql.statement.query.time.total.ms` is `float` | aggregations read doc_values, so a sample is on a 1/1000 grid and a counter reaching 3.2e6 is on a 0.25 grid. Raw values of 0.0003 and 0.0007 are indistinguishable from 0.000 and 0.001 on the Elastic side |

For log series the harness therefore asserts **conservation** instead of equality: no bucket
moves by more than one second's worth of events, and the signed deltas sum to ~0. Both bounds
are queried per series — an nginx `alert` series with 12 events all window long is allowed to
move by 1, not by the stream-wide 66. That still fails on every real bug (a double-counted
stream shifts every bucket ~100%, a wrong field changes the magnitude, a dropped group
vanishes, a scale error breaks conservation) and tolerates only re-bucketing.

> **A tolerance should come from a mapping or a query, never from the size of the failure.**
> Every number above was read off `_mapping` or computed from the data. One that was not:
> `compare()` briefly used `max(tol, tol × |expected|)`, which silently turns a tolerance of
> 1 into a 100% relative tolerance — a check that passes anything. Absolute now.

## Where the source platform is wrong

Verification normally means "match Elastic". Four numbers deliberately do not, because
Elastic's own parsing is at fault. Each is asserted in `verify-apache.sh` or
`verify-system.sh` so that nobody "fixes" ClickStack into agreeing:

| what | Elastic | ClickStack | cause |
|---|---|---|---|
| IPv6 client address | final hextet only, filed under `source.domain` | whole address | the integration's grok cannot match a bare IPv6 address in a combined log. 7,881 rows; 60 distinct addresses collapse onto 59 hextets, so Kibana reports 5,716 unique IPs to ClickStack's 5,718 |
| request line containing `\` | `url.original` dropped entirely | URL intact | 503 requests silently absent from Kibana's URL panel; 190 distinct URLs there, 191 here. Status, IP and UA survive on both sides |
| `Firefox/141.0` version | `141.0.` | `141.0` | Elastic appends a separator for a version capture group that *matched the empty string*; a ClickHouse back-reference cannot tell that apart from a group that did not match, and reproducing it would mean reproducing the bug. Same 15,801 rows |
| SSH `invalid user` name | `" admin"` — **leading space** | `"admin"` | the `system.auth` grok captures the username with the separator still attached on the `Failed password for invalid user X` variant. Kibana therefore reports **27** distinct failed usernames where there are 14, splitting every bot target in two: `admin` 880 **and** `" admin"` 759. ClickStack reads 1,639 for `admin`. The panel this ruins is `SSH users of failed login attempts`, whose whole purpose is a list of attacked usernames |

None of these were visible from the panel inventory. They surfaced only from diffing whole
distributions against a live source — the same method that found the three parser bugs above.
The SSH one is the most instructive: the counts matched perfectly in aggregate (14,000 sshd
lines either way), and only a **full-distribution** diff showed one platform splitting a
dimension the other did not.

## If you migrate one next

Four integrations are complete and `system` is done for Linux. What remains is genuinely
harder than anything migrated so far, and neither remaining candidate is a bigger version of
what has been done:

| candidate | dashboards | panels | the actual obstacle |
|---|---:|---:|---|
| **system (Windows Security)** | 5 | 80 | needs a **third** generator: Windows event logs keyed on event codes (4624/4625/4720/4732…), which this corpus has no analogue for. The translation itself looks cheap — mostly `terms` breakdowns and datatables — so this is purely a data problem, and the smallest remaining one |
| **kubernetes** | 15 | 130 | **leave alone.** 166 fields, and 43 panels use ad-hoc data views with Painless `runtimeFieldMap` — expressions evaluated per query that exist in no mapping on either side, so each must be re-implemented as a ClickHouse expression before a single tile can be verified. Several data views are also cross-cluster (`*:metrics-*`), so the target may not hold the data at all |

The pattern across all five completed integrations has not changed: **the work is the data, not
the translation.** The `system` migration is the clearest case — two generators and ten data
streams took the bulk of the effort, and the 65 tiles came out of it comparatively quickly.
Windows is the same shape of problem again, minus the metrics.
