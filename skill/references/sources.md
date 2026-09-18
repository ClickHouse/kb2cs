# Data views → ClickStack sources

A Kibana panel says *which index pattern* it reads. A ClickStack tile says *which source*.
Every tile carries exactly one `sourceId`, so until this mapping exists no tile can be
written — and unlike the field mapping, it cannot be deferred or guessed at from the panel
definition.

```bash
python3 scripts/inventory-panels.py dashboards.ndjson --sources   # source side
python3 scripts/introspect-clickstack.py --sources                # target side
```

## Matching rule

In order, because the first one can rule out the other two:

1. **Signal kind.** A `logs-*` view belongs on a `log` source, `metrics-*` on a `metric`
   source. Not cosmetic: a metric select item needs `metricName`, `metricType` and `isDelta`,
   which a log tile has no place for. A metric source also has **no single table** — it fans
   out to one per metric type (`otel_metrics_gauge`, `_sum`, `_histogram`, `_summary`), and
   `metricType` is what picks it. Getting the kind wrong means re-authoring the tile, not
   editing it.
2. **The fields.** Everything the panels read must exist on the candidate source.
   `--sources` prints the field list per data view; `clickstack_describe_source` prints the
   target's columns, including materialized ones. Diff them before committing.
3. **The time field.** The data view's `timeFieldName` should line up with the source's
   timestamp column (`Timestamp` on logs and traces, `TimeUnix` on metrics in the reference
   deployment). A mismatch is usually a sign that step 1 was wrong.

**The relationship is many-to-many.** One data view maps to several sources when the target
split that data across tables; several data views map to one source when the target
consolidated them. Think per *(data view, target table)*, not per name — and note that two
dashboards reading the same data view can still land on different sources.

## Where a panel records its data view

Kibana uses six different places, and a reader that checks only the obvious one under-reports
badly. Measured against the 334 stock reference panels:

| # | where | who uses it |
|---|---|---|
| 1 | `embeddableConfig.attributes.references[]` | by-value Lens |
| 2 | the referenced object's own `references[]` | by-reference panels |
| 3 | `kibanaSavedObjectMeta.searchSourceJSON.index` | legacy visualizations, saved searches |
| 4 | `embeddableConfig.savedVis.data.searchSource.index` | by-value legacy visualizations |
| 5 | `layerListJSON[].sourceDescriptor.indexPatternId` | maps |
| 6 | `state.adHocDataViews` | Lens, **43 of 334 panels** |

`indexPatternId` inside a Lens `formBased` layer is commonly `null` even when the panel
plainly reads an index — the reference array is authoritative, the datasource state is not.

## Three cases that change the work

### Ad-hoc data views

Lens can define a data view *inside the panel* (`state.adHocDataViews`) instead of pointing at
a saved object. These are not a corner case: they were **every** panel whose data view looked
unresolvable in the reference estate. Treat them exactly like saved views for mapping
purposes — they still need a target source — but note that nothing in Kibana's saved-object
list will tell you they exist.

### Runtime fields

An ad-hoc or saved data view can carry a `runtimeFieldMap`: **Painless scripts evaluated at
query time**. 12 of the 43 ad-hoc panels in the reference estate had one, for example:

```painless
if (doc['kubernetes.pod.status.phase'].value == "failed") { emit(1) }
```

Nothing on the target runs Painless, so each becomes a ClickHouse expression written by hand.
Two reasons this is worth its own step:

- It is **query-time** enrichment, so it is invisible in the index mappings and in any
  field-existence check against the target. The field simply is not there to find.
- The example above is a conditional emit — a counter. In ClickHouse that is usually not a
  column at all but a `countIf(...)` in the select item, which means the *tile* changes shape,
  not just an expression inside it.

`--triage` lists them per panel so they are costed rather than discovered mid-translation.

### Cross-cluster patterns

A data view title like `metrics-*,*:metrics-*` is cross-cluster search: the `<cluster>:` part
reads indices in *remote* clusters. The panel therefore aggregates data that is not in the
Elasticsearch you are pointed at, and may not be in the target at all. Resolve the scope
question — does the target ingest those clusters, into one table or several? — before mapping
it, because the answer decides whether this is one tile or several, or none.

## What the mapping is for

Write it down as an artifact, alongside the field mapping: **data view · target source id ·
kind · note**. The note column carries the awkwardness — "ad-hoc, defined in 3 panels",
"2 runtime fields re-expressed as countIf", "cross-cluster, remote data not ingested". A
later reader needs the reasons more than the ids.

## Can the target keep the SOURCE's schema?

A question that decides scope, and worth answering before anyone maps a field: if ECS-shaped
rows are already in ClickHouse, must the dashboards be rewritten onto the OTel model, or can a
source be pointed at ECS as it stands?

It matters in two situations. One is **existing ECS history** somebody wants queryable without
re-ingesting. The other is bigger: a customer who wants to **keep Filebeat/Metricbeat or
Elastic Agent** for a proof of concept, and validate ClickStack before touching their agents.
The [documented route](https://clickhouse.com/docs/use-cases/observability/clickstack/migration/elastic/migrating-agents)
for that is Beats → Vector → OTLP, which converts ECS to OTel in VRL — so on the sanctioned
path the dashboards move to OTel names anyway. The answer below is what you can do *instead*.

**Measured on ClickStack 2.35.0, 2026-09-18**, against a deliberately hostile case: ECS as
*flat typed columns*, no attribute `Map` anywhere, which is how ECS actually lands.
`verify/verify-ecs-source.py` in this repo re-asserts all of it in 29 checks against a
generated fixture, so a future ClickStack that behaves differently turns it red rather than
quietly invalidating this page.

| | verdict |
|---|---|
| Logs on a **log** source | **Yes.** A source stores expressions, so ECS columns map onto its slots |
| Metrics on a **metric** source | **No.** It requires the narrow OTel metric tables; a wide ECS document is an unpivot away |
| Metrics on a **log** source | **Yes** — and this is the useful answer. Builder *and* SQL tiles work, but see the NULL trap below |

### Logs: yes, and the reason is that a source stores EXPRESSIONS

`clickstack_save_source` takes `timestampValueExpression`, `bodyExpression`,
`serviceNameExpression`, `severityTextExpression`, `eventAttributesExpression` and the rest as
**SQL expressions, not column names**. So the mapping happens in the source definition:

```
timestampValueExpression  : `@timestamp`
bodyExpression            : message
serviceNameExpression     : `service.name`
severityTextExpression    : `log.level`
eventAttributesExpression : map('source.ip', `source.ip`,
                                'url.original', `url.original`,
                                'http.response.status_code', toString(`http.response.status_code`),
                                'user_agent.name', `user_agent.name`)
```

That last one is the interesting part: flat ECS columns can be *synthesised into* an attribute
map by expression. Everything downstream worked — `describe_source` introspected it, `search`
returned rows, `table` and `timeseries` aggregated, and a dashboard rendered with data.

> **The limitation, which is easy to assume away.** The source's semantic expressions do **not**
> rewrite query identifiers. Only the timestamp expression reaches the generated SQL. A tile
> referring to `Attributes['user_agent.name']`, `SeverityText`, `ServiceName`, `Body` or
> `Timestamp` fails with *"Unknown expression identifier"* — all five rejected. Tiles must
> reference the **source table's own column names**:
>
> ```
> groupBy: `user_agent.name`        -- works
> groupBy: Attributes['user_agent.name']   -- Unknown expression or function identifier `Attributes`
> ```
>
> Simpler in one way, but you lose the attribute-map abstraction and ClickStack's semantic
> identifiers inside queries, and anything downstream keyed on them.

### Metrics on a metric source: no, and the obstacle is shape rather than naming

- `save_source` with `kind: metric` and no `metricTables` → rejected: **`metricTables: Required`**.
- `metricTables: {gauge: <wide ECS metrics table>}` → **accepted**, then every query failed:
  *"Unknown expression or function identifier `ScopeAttributes`"*.

The metric query layer assumes the OTel metrics columns — `MetricName`, `Value`, `TimeUnix`,
`ScopeAttributes` and friends — spread across one table per metric kind. ECS metrics are the
opposite shape: **one wide document with many numeric fields** (`system.cpu.user.norm.pct`,
`system.cpu.system.norm.pct`, …). Getting from there to here is an **unpivot, not a rename**.

### Metrics on a LOG source: yes, and this is what a Beats proof of concept should use

Register the wide ECS metrics table as `kind: log` and the whole tile vocabulary works. Against
36,000 real Metricbeat documents (`system.cpu`, `.memory`, `.load`, `.network`) landed
unchanged, **1,005 series matched Elasticsearch** across `line`, `stacked_bar`, `table`,
`number`, `bar`, `pie`, `search` and `sql` tiles — including multi-dimensional `groupBy`,
`quantile` levels, `numberFormat`, and Lucene filters resolving **dotted column names**
(`event.dataset:system.memory`) with no `Map` involved.

What you give up is the metric-source machinery: `metricType`/`metricName`, de-cumulation, and
`aggFn: "increase"`. A counter therefore has **no builder shape** and needs a SQL tile:

```sql
SELECT ts, host, sum(delta) AS `Bytes in` FROM (
  SELECT $__timeInterval_ms(`@timestamp`) AS ts, `host.name` AS host,
         `system.network.name` AS iface,
         max(`system.network.in.bytes`) - min(`system.network.in.bytes`) AS delta
  FROM $__sourceTable
  WHERE $__timeFilter_ms(`@timestamp`) AND `event.dataset` = 'system.network' AND $__filters
  GROUP BY ts, host, iface
) GROUP BY ts, host ORDER BY ts, host LIMIT 5000
```

Per-interface before summing, because a counter is per interface; 120 of 120 buckets matched
Elastic's equivalent exactly.

> ### The trap: NULL is not absent, it is a zero that counts
>
> Beats writes **one document per metricset**, so a table holding several metricsets is mostly
> NULL — and a numeric builder aggregation compiles to
>
> ```sql
> AVG(toFloat64OrDefault(toString(`system.cpu.total.norm.pct`)))
> ```
>
> `toString(NULL)` is NULL and `toFloat64OrDefault` returns its default for anything it cannot
> parse. So a NULL becomes a zero **in the denominator**:
>
> | aggregation | on a wide multi-metricset table | why |
> |---|---|---|
> | `avg` | **wrong** — divided by however many rows share the group | zeros enter the denominator |
> | `min` | **wrong** — returns 0 | the injected zero is the minimum |
> | `last_value` | **wrong** — usually 0 | the newest row belongs to another metricset |
> | `count` | **wrong** | it counts rows, and other metricsets' documents are rows |
> | `quantile` | **wrong, and plausible** — it silently reports a lower percentile | the zeros occupy the low ranks |
> | `max`, `sum` | correct | zero is their identity |
> | `count_distinct` | correct | it is the one numeric aggregation NOT put through the cast |
>
> **Every one of them renders `status=ok, hasData=true`.** The dilution factor is the number of
> *rows* sharing the group, **not** the number of metricsets — a `system.network` metricset
> emits one document per interface, so four metricsets diluted a reference average five-fold.
> Derive it as `count(*) / countIf(field IS NOT NULL)`.
>
> Note also what does *not* help: returning NULL from the value expression
> (`if(`event.dataset` = 'system.cpu', `system.cpu.total.norm.pct`, NULL)`) is coerced just the
> same. The coercion happens outside the expression, so it cannot be written around.

**Two fixes, in order of preference.**

1. **One view per metricset, each registered as its own source.** No predicate discipline, and
   every aggregation is faithful including `last_value` and `count`. A `CREATE VIEW` is
   accepted as a source table, so this costs no second copy of the data, and it maps
   one-to-one onto Kibana panels, which are already scoped to a single data stream.
   `scripts/ecs-to-clickhouse.py` emits the table, the views and the `save_source` arguments.
2. **Carry the metricset predicate on every tile.** With `event.dataset = 'system.memory'` on
   each select item, all seven aggregations matched Elasticsearch exactly — avg, min, max, p95,
   cardinality and row count. This is the metrics corollary of the rule that logs already have:
   for logs, omitting the dataset predicate *double-counts*; for metrics it silently *divides*.

### If the metric arrives as a String, which the Vector route produces

The documented VRL conversion **flattens every field to a string**. Measured consequence: a
numeric metric in a `String` column still aggregates, because the cast parses it — but any
value that does not parse becomes a **zero that counts**, exactly like a NULL. A tile over
string-typed metrics needs a predicate excluding the non-numeric values, or its average is
pulled toward zero by however many there are. Type loss is not only a cosmetic complaint.

### Two smaller notes from the same measurements

- **Column discovery is cached by table name.** Recreate a table with an extra column and
  `describe_source` keeps reporting the previous column list — which reads exactly like the
  target dropping a column. A tile can still *read* the new column, so the cache misleads
  discovery, not querying. It matters during a proof of concept, when the landing schema is
  still being iterated on; give the table a new name, or expect to be lied to.
- The delete tools take **`id`**, where `patch_dashboard` takes `dashboardId` and the query
  tools take `sourceId`. A wrong key fails validation rather than doing something harmful, but
  it costs a round trip.
- ClickStack cannot read Elasticsearch at all, so every version of this presupposes the rows
  are already in ClickHouse. That migration is the real cost, not the schema.
