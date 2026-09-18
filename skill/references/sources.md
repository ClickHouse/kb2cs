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

## Can the target keep the SOURCE's schema? Logs yes, metrics no

A question that decides scope, and worth answering before anyone maps a field: if the customer
already has ECS-shaped rows in ClickHouse, must the dashboards be rewritten onto the OTel
model, or can a source be pointed at ECS as it stands?

**Measured on ClickStack 2.35.0, 2026-09-18**, against a deliberately hostile case — ECS as
*flat typed columns*, no attribute `Map` anywhere, which is how ECS actually lands:

```
`@timestamp`  `message`  `service.name`  `log.level`  `trace.id`
`source.ip`  `url.original`  `http.response.status_code`  `user_agent.name`  `event.duration`
```

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
map by expression. The source was accepted and everything downstream worked —
`describe_source` introspected it, `search` returned rows, `table` and `timeseries` aggregated
correctly, a numeric ECS field averaged, and a two-tile dashboard rendered with data.

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

### Metrics: no, and the obstacle is shape rather than naming

- `save_source` with `kind: metric` and no `metricTables` → rejected: **`metricTables: Required`**.
- `metricTables: {gauge: <wide ECS metrics table>}` → **accepted**, then every query failed:
  *"Unknown expression or function identifier `ScopeAttributes`"*.

The metric query layer assumes the OTel metrics columns — `MetricName`, `Value`, `TimeUnix`,
`ScopeAttributes` and friends — spread across one table per metric kind. ECS metrics are the
opposite shape: **one wide document with many numeric fields** (`system.cpu.user.norm.pct`,
`system.cpu.system.norm.pct`, …). Getting from there to here is an **unpivot, not a rename**,
so there is nothing to be gained by keeping ECS names for metrics — you are reshaping the data
either way.

### What to do with this

If ingestion is moving to the OTel collector, the question is moot: ECS-shaped rows never
arrive. It matters in exactly one situation — **existing ECS history that someone wants
queryable in ClickStack without re-ingesting**. There, logs are reachable through a source
definition, and metrics are not without reshaping. Decide that before promising a dashboard
over historical data.

Two smaller notes from the same test, both easy to trip on:

- The delete tools take **`id`**, where `patch_dashboard` takes `dashboardId` and the query
  tools take `sourceId`. The key name is not consistent across the API; a wrong one fails
  validation rather than doing something harmful, but it costs a round trip.
- ClickStack cannot read Elasticsearch at all, so any version of this presupposes the rows are
  already in ClickHouse. That migration is the real cost, not the schema.
