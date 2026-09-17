# The Kibana export shape

What `inventory-panels.py` is walking, so you can extend it or read a panel by hand when it
reports `unknown`.

## The file

`POST /api/saved_objects/_export` returns **NDJSON**, one saved object per line, plus a final
summary line (`{"excludedObjects":[],"exportedCount":N,...}`) that is not an object. Each
object is `{"id","type","attributes","references":[...]}`.

Types you will see: `dashboard`, `lens`, `visualization`, `search`, `map`, `index-pattern`,
`tag`.

## Panels live inside a JSON string

`dashboard.attributes.panelsJSON` is a **string** containing the panel array. Parse it, then
each entry has:

| key | meaning |
|---|---|
| `type` | `lens`, `visualization`, `search`, `map`, `links` |
| `panelIndex` | stable panel id |
| `title` | panel title *override*; often absent, in which case the title lives on the embedded object |
| `gridData` | `{x,y,w,h}` layout — ClickStack tiles use the same idea, so it is worth carrying over |
| `embeddableConfig` | **by-value** panel definition, when there is one |
| `panelRefName` | present instead when the panel is **by reference** — resolve via `references[]` to another line in the file |

Two ways the same panel can be stored, and a single dashboard mixes them freely:

- **By value** — `embeddableConfig.attributes` holds the whole visualization. Nothing to resolve.
- **By reference** — `panelRefName: "panel_1"` matches `references[].name`, whose `id` is another object in the NDJSON.

Always handle both. In 7.x+ exports, by-value is the common case and the one people miss.

## Lens panels (`type: "lens"`)

The definition is at `embeddableConfig.attributes` (by value) or on the `lens` object's
`attributes` (by reference):

```
attributes.visualizationType   lnsXY | lnsPie | lnsDatatable | lnsMetric | lnsLegacyMetric
                               | lnsHeatmap | lnsTagcloud | lnsGauge
attributes.state.datasourceStates.formBased.layers.<layerId>.columns.<colId>
    operationType              count | sum | average | median | max | min | unique_count
                               | terms | date_histogram | filters | range | percentile
                               | last_value | formula | math | static_value
                               | differences | counter_rate | moving_average | cumulative_sum
    sourceField                the ECS field  ← the one you want
    label                      user-facing label
    params                     size / orderBy / interval / filters[] / percentile ...
attributes.state.datasourceStates.textBased        ← ES|QL / SQL Lens, query in .layers.*.query
attributes.state.filters[]                          panel-level filters
attributes.state.query                              {language:"kuery"|"lucene", query:"..."}
attributes.state.visualization                      shape/preferredSeriesType, axis assignment
```

`operationType` is the aggregation; `visualizationType` is the chart type. You need both —
the first maps to a ClickStack `select`/`groupBy`, the second to a `displayType`.

Gotchas:

- A `formula` column has **no `sourceField`**; its expression is in `params.formula` and
  references other fields by name. Translate the formula, not a field.
- **Referenced ("pipeline") operations have no `sourceField` either.** `differences`,
  `moving_average`, `cumulative_sum`, `counter_rate` and `normalize_by_unit` carry
  `references: [columnId]` pointing at the column they transform, and *both* columns appear
  in `columnOrder`. Read them as a pair — the op alone does not say which metric is being
  differenced. These do **not** map to a ClickStack aggregate; they need a window function
  (see `field-mapping.md`). Observed on the stock `[Metrics Nginx] Overview` dashboard, where
  4 of 8 panels are `differences(of max(...))`.
- `filters` columns hold an array of KQL clauses in `params.filters[].input.query` — this is
  how Kibana expresses "status ranges as series". On ClickStack this becomes either a
  `groupBy` expression (`intDiv(status,100)`) or one `select` item per clause.
- `date_histogram` with `params.interval: "auto"` has no fixed bucket size. Pick one
  explicitly on the target; do not try to reproduce "auto".

## Legacy visualizations (`type: "visualization"`) — two different shapes

**By reference:** `attributes.visState`, a JSON string (next section).

**By value:** `embeddableConfig.savedVis`, an **already-parsed object with a different
shape** — there is no `visState` key at all:

```
savedVis.type                 pie | markdown | metrics | table | ...
savedVis.title
savedVis.params               chart options; `markdown` text lives here
savedVis.data.aggs            the agg list (same shape as visState.aggs)
savedVis.data.searchSource    query + filter[], already parsed (not a string)
```

Reading only `visState` silently misses every by-value legacy panel. Measured across the
stock nginx, system, apache, mysql and kubernetes dashboards: **51 of 334 panels**, all of
them at `embeddableConfig.savedVis`, none with a `visState`.

## TSVB is a family, not a chart type

`type: "metrics"` means "this is TSVB". What it *renders* is `params.type`:

| `params.type` | target |
|---|---|
| `timeseries` (the default when the key is absent) | `line` |
| `metric`, `gauge` | `number` |
| `top_n` | `bar` |
| `table` | `table` |
| **`markdown`** | **`markdown` — a text panel with no data** |

Classifying TSVB by `savedVis.type` alone is a trap with teeth: a markdown-mode TSVB still
carries a default `series: [{metrics: [{type: "count"}]}]`, so it looks like a count chart.
All **11** TSVB panels in the stock system and kubernetes dashboards are markdown-mode — read
them as charts and you migrate 11 text panels as line charts of a meaningless count.

TSVB metrics live at `params.series[].metrics[].field`, never in `aggs`.

### The `visState` string (by-reference form)

```
{ "type": "pie"|"histogram"|"line"|"area"|"table"|"metric"|"goal"|"tagcloud"|"metrics"|"region_map"|"timelion",
  "aggs": [ { "type":"count"|"terms"|"date_histogram"|"sum"|"avg"|"cardinality"|"filters",
              "schema":"metric"|"segment"|"group"|"bucket",
              "params": {"field":"...", "size":10, "orderBy":"1", ...} } ],
  "params": { ... } }
```

`schema` tells you the role: `metric` → a `select` item, `segment`/`group`/`bucket` → `groupBy`.

One special case: `type: "timelion"` carries a `.es(...)` expression string. There is no
structured field list; read the expression. (`type: "metrics"` is TSVB — see above.)

The index it queries is in `attributes.kibanaSavedObjectMeta.searchSourceJSON` (also a string):
`index` (an index-pattern reference id), `query`, and `filter[]` where each filter has
`meta.key`, `meta.negate`, `meta.type` and a `query` clause.

## Filters come from two levels, and you need both

The **dashboard** object has its own `searchSourceJSON` filters, which apply to every panel on
it. A panel that looks unfiltered is often scoped by the dashboard.

This is not a corner case. On the stock `[Logs Nginx] Overview`, the dashboard carries
`data_stream.dataset: nginx.error, nginx.access` and three of its seven panels — the map, OS
breakdown and Browsers breakdown — carry **no panel-level filter at all**. Read only the panel
filters and you will migrate those three unscoped.

Two consequences for translation:

- A dashboard-level filter is frequently a **phrases (OR) filter over several datasets**, so it
  does not translate to one predicate you can paste onto every tile. Decide the scope
  per panel: the union is the *default* inherited scope, not the panel's intent.
- `inventory-panels.py` prints dashboard-level filters once in a header line above the panel
  table, precisely so they are not silently folded into each row.

## A `combined` filter carries its own AND/OR relation

Kibana can join several filter pills into one, and the join is stored on the wrapper rather
than on the members:

```json
{"meta": {"type": "combined", "relation": "OR",
          "params": [ {"meta": {"type": "exists", "key": "a"}, ...},
                      {"meta": {"type": "exists", "key": "b"}, ...} ]}}
```

`meta.params` is a **list of whole sub-filters**, each with its own `meta` — so a reader that
treats `params` as a scalar prints Python dicts, and a reader that flattens the members loses
the `relation`.

**Losing the relation inverts the panel.** On the stock `[Metrics System] Overview`,
`Top Hosts by CPU` combines two `exists` filters with **OR**:

| read as | rows returned |
|---|---|
| `exists(a) OR exists(b)` — correct | 50,400 |
| `exists(a) AND exists(b)` | **0** |

Zero, because the two fields live in different data streams (`system.process` and
`system.memory`) and never co-occur in a document. Flattened output made a working panel look
structurally unmigratable. `inventory-panels.py` renders combined filters recursively as
`(a:* OR b:*)`; three panels across the reference estate use one.

## The "field exists" idiom

Kibana panels scope by existence with `some.field:*` — the two stock saved searches use
`message:*` and `url.original:*` to separate the error stream from the access stream without
naming a dataset. On ClickStack a missing `Map` key returns the **empty string**, not NULL, so
this becomes `LogAttributes['msg'] != ''`, not `IS NOT NULL`.

## Saved searches (`type: "search"`)

`attributes.columns` is the displayed column list — these map straight onto a ClickStack
saved search's columns. `attributes.sort`, and `searchSourceJSON` as above. This is the
easiest panel class to migrate and worth doing first for an early win.

## Maps (`type: "map"`)

`attributes.layerListJSON` (string) → layers with `sourceDescriptor.geoField`. Inventory it so
the loss is explicit, but there is no target chart type. See SKILL.md step 3.

## The control bar (`controlGroupInput`) is not a panel, and it IS migratable

A dashboard can carry a row of dropdowns above the panels that filter all of them. It lives
outside `panelsJSON` entirely:

```json
"controlGroupInput": {
  "panelsJSON": "{\"<id>\":{\"order\":0,\"type\":\"optionsListControl\",
                   \"explicitInput\":{\"fieldName\":\"postgresql.database.name\",
                                      \"title\":\"database\",\"singleSelect\":true}}}"
}
```

**21 of the 37 dashboards in the reference estate have one**, and because it is not a panel a
panel inventory never mentions it — which is how it went unmigrated on 7 of 17 dashboards
until a user asked why a `database` dropdown was missing.

It maps **one-to-one** onto ClickStack's dashboard-level `filters` array, which renders as
exactly that: an interactive dropdown in the filter bar rather than an applied predicate. So
this is a translation, not a degradation, which makes dropping it silently worse than a
documented loss.

| Kibana control | ClickStack filter |
|---|---|
| `explicitInput.title` | `name` |
| `explicitInput.fieldName` | `expression` (the mapped target column) |
| — | `sourceId`, and **`sourceMetricType` if that source is a metric source** |

> **The trap: `sourceMetricType` is "required only when `sourceId` is a Metric source", so it
> is absent from the schema's top-level `required` list.** A metric-source filter without it
> validates, saves, and then renders an **empty dropdown** — which is what "I can't see the
> databases" turned out to be. `scripts/audit-tiles.py` checks both the presence of the
> filters and this field.

`inventory-panels.py` now prints a `Dashboard controls` line per dashboard.

### Then verify the OPTION SET, because "not empty" is not "right"

An option list on the target is `SELECT DISTINCT <expression>` over a whole table, so it shows
whatever else happens to be in that table. Two mechanisms put things there that the source
cannot have:

- **The target self-monitors.** The ClickStack all-in-one image ships a collector whose OpAMP
  config injects a `prometheus` receiver scraping its own telemetry endpoint into the metrics
  pipeline, so `otel_metrics_*` carries `otelcol_*` / `scrape_*` / `up` series tagged with the
  container's hostname. Any host-valued metric dropdown gains that hostname as an option. The
  source has no counterpart, because an agent's own telemetry goes to a monitoring cluster
  rather than into the data indices.
- **The metric-type split removes things.** A filter reads exactly ONE table — whichever
  `sourceMetricType` names — and a dataset is normally spread across `sum` and `gauge`. Any
  entity whose metrics are all of the other type is missing from the option list, legitimately.

Those two pull in opposite directions, and on the reference migration they cancelled exactly:
three dropdowns each lost one real host to the split and gained the container id, resolving
the same option COUNT as Kibana while the sets differed. **Two errors cancelling in the total
is the argument for diffing sets, not lengths.**

Three things to establish before calling a control migrated, in this order:

1. **The control exists** on every dashboard whose source has one.
2. **It resolves to something** — this is the `sourceMetricType` check.
3. **It resolves to the same set as the source's control**, value by value.

Getting (3) right needs one fact about Kibana that inverts the obvious expectation: **a
control resolves its options against the data view it is bound to, which is usually the broad
`logs-*` / `metrics-*` pattern rather than the integration's own index pattern.** So an
integration dashboard's host dropdown lists *every* host in `metrics-*`, including hosts that
emit none of that integration's metrics. An unscoped dropdown on the target is therefore
**faithful**, and "fixing" it into an integration-scoped list would *create* a divergence.
Check the bound data
view before you decide a dropdown is too broad — it is one API call, and on the reference
migration it was one call away from being got backwards: a host that obviously "did not
belong" on an integration's dashboard was offered by the source too.

The same check also reveals controls that are **broken identically on both platforms** —
bound to a field that the dashboard's own documents do not carry, so every option returns
nothing. That is the correct outcome: reproduce it, record it, and do not repair it into a
divergence.

A `QUERY_EXPRESSION` filter is `{name, expression, sourceId, sourceMetricType}` — there is no
predicate field, so the option list is always `SELECT DISTINCT <expression>` over the whole
table. When the target's own telemetry lands there, no migration-side change can exclude it;
assert the one known extra value explicitly so that a *second* one fails the check.

## `links` panels

Dashboard-navigation panels. Not data, not migratable, and should not be counted in the
panel total — otherwise the migration looks incomplete when it is finished.
