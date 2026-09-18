---
name: kibana-to-clickstack
description: >
  Migrate Kibana dashboards, visualizations and saved searches to ClickStack (HyperDX)
  dashboards and tiles. Use when asked to move, port, convert or recreate Kibana/Elastic/ELK
  dashboards or panels on ClickStack, HyperDX or ClickHouse observability, or to map ECS
  fields onto a ClickStack OTel schema. Covers exporting saved objects, translating
  aggregations to tile specs, and verifying migrated tiles against the source numbers and
  against the source's rendered charts.
---

# Migrating Kibana dashboards to ClickStack

A dashboard migration is three problems, not one, and they fail in different ways:

1. **Schema** — ECS typed fields vs. a `Map(LowCardinality(String), String)`. Mechanical, solvable.
2. **Visualization vocabulary** — the target has fewer chart types than the source. Not solvable; must be declared up front.
3. **Index-time enrichment** — geo, user-agent, and any ingest-pipeline output does not travel with the data. Recreatable, but as a different mechanism.

Work in that order. Most wasted effort comes from translating a panel that was never going to render (2) or whose source field does not exist on the target (3).

## Preflight

**Know the tool asymmetry before planning.** It decides where each artifact comes from:

| | tools | reads/writes dashboards? |
|---|---|---|
| `elasticsearch` MCP | `list_indices`, `get_mappings`, `search`, `esql`, `get_shards` | **No.** Data only |
| `clickstack` MCP | ~30 incl. `save_dashboard`, `patch_dashboard`, `query_tile(s)`, `describe_source`, `table`, `timeseries`, `sql` | **Yes**, full CRUD |

So panel definitions come from the **Kibana Saved Objects API over HTTP**, never from MCP. There is no Kibana MCP tool.

Checks worth doing before step 1, in one batch:

- `claude mcp list` — the ClickStack server must read **Connected**, not "Pending approval". MCP servers attach at session start, so approving one cannot help the running session; a restart is required. If ClickStack was recreated with a fresh volume, its personal API key changed and the old token fails with *"requires re-authorization (token expired)"* — regenerate the MCP config, then restart.
- **Settle auth and spaces first — both are silent failure modes.**

  ```bash
  export KIBANA_URL=https://<deployment>.kb.<region>.cloud.es.io
  export KIBANA_API_KEY=<the `encoded` field from POST /_security/api_key>
  curl -s -H "Authorization: ApiKey $KIBANA_API_KEY" "$KIBANA_URL/api/status" | head -c 200
  "$SKILL"/scripts/export-dashboards.sh --list-spaces
  ```

  *Auth:* basic auth (`-u user:pass`) works self-managed but is frequently unavailable on
  Elastic Cloud and absent on Serverless, and is displaced wherever SAML/OIDC SSO is in use.
  An API key works everywhere, so default to one. Pass it by environment variable — an
  argument is visible in `ps` and in shell history.

  *Spaces:* saved objects are space-scoped and so is every API path (`/s/<space>/api/...`).
  An unscoped call against a deployment whose dashboards live in other spaces returns an
  empty list, which is indistinguishable from "there are no dashboards" — do not read that
  as nothing to migrate. Run `--all-spaces` to take the real inventory.
- If the source is Elasticsearch 8.x, **do not plan to aggregate through `@elastic/mcp-server-elasticsearch`** (≤0.3.1). It sends `compatible-with=9` accept headers and 8.x rejects them with `media_type_header_exception`. Use `curl` for aggregations; keep the MCP server for `get_mappings`.

## Procedure

### 1. Export and inventory the source panels

Script paths below are relative to **this skill's directory**, not the working directory:

```bash
# installed as a personal skill (see the repo README for how to install it):
SKILL=~/.claude/skills/kibana-to-clickstack
# ...or run it straight out of a clone of this repo:
SKILL=./skill

# credentials and URL come from the environment (see Preflight)
export KIBANA_URL=... KIBANA_API_KEY=...

# with no ids, LISTS dashboards; --all-spaces sweeps every space, which is the real inventory
"$SKILL"/scripts/export-dashboards.sh --all-spaces
"$SKILL"/scripts/export-dashboards.sh --space <space> <id>... > dashboards.ndjson

# basic auth and the older positional form still work, for a self-managed stack
"$SKILL"/scripts/export-dashboards.sh --url http://localhost:5601 --user elastic --pass changeme

python3 "$SKILL"/scripts/inventory-panels.py dashboards.ndjson           # markdown table
python3 "$SKILL"/scripts/inventory-panels.py dashboards.ndjson --json    # machine-readable
python3 "$SKILL"/scripts/inventory-panels.py dashboards.ndjson --fields  # fields to map
```

Two parsing facts that cost time if rediscovered by hand: panels are usually stored **by value** inside `attributes.panelsJSON` (a JSON *string*), not as referenced saved objects; and for Lens panels the field you want is `sourceField` inside `datasourceStates.formBased.layers.*.columns`. `inventory-panels.py` handles both, plus legacy `visState` aggs, TSVB, maps and saved searches — read `references/kibana-export.md` if a panel type comes back `unknown`.

`--fields` emits the complete set of source fields the dashboards depend on — that list, not the index mappings, is what you diff against the target in step 2. Mappings declare far more fields than any dashboard uses.

Show the panel table to the user and settle the `NOT MIGRATABLE` / `UNKNOWN` rows before translating anything (see step 3).

**Do not stop at panels.** A dashboard's **control bar** (`controlGroupInput`) is a row of field-bound dropdowns that filters every panel, it lives outside `panelsJSON`, and it maps one-to-one onto ClickStack's dashboard-level `filters`. **21 of 37 dashboards in the reference estate carry one and 7 of 17 migrated dashboards silently lost it.** `inventory-panels.py` prints a `Dashboard controls` line; `references/kibana-export.md` has the shape, the `sourceMetricType` trap that makes a migrated one render empty, and — the follow-on that caught the same dashboards twice — why you must diff each dropdown's **option set** against the source rather than assert it is non-empty. Note before judging a dropdown "too broad": a Kibana control resolves against the **data view it is bound to**, normally the broad `logs-*`/`metrics-*` pattern, so an unscoped option list on the target is usually faithful.

To check the parser after editing it: `python3 scripts/tests/make-fixture.py > /tmp/f.ndjson && python3 scripts/inventory-panels.py /tmp/f.ndjson` — the fixture exercises every panel shape the script claims to handle.

### 2. Map data views to sources, inventory the target, then triage

**Every tile carries exactly one `sourceId`, so this mapping gates all translation.** Do it before writing a query: a dashboard whose panels read several data views may not be one dashboard on the target.

```bash
python3 "$SKILL"/scripts/inventory-panels.py dashboards.ndjson --sources   # source side
python3 "$SKILL"/scripts/introspect-clickstack.py --sources                # target side
```

Match on three things, in order: **signal kind** (a `logs-*` view belongs on a `log` source, `metrics-*` on a `metric` source — the tile keys genuinely differ), **the fields** the panels read (they must exist on that source), and **the time field** (it becomes the source's timestamp column). One data view can map to several sources and several to one: the mapping is per *(data view, target table)*, not per name. Awkward cases: `references/sources.md`.

Three things `--sources` surfaces that reading panel JSON by hand does not:

- **Ad-hoc data views** (`state.adHocDataViews`) are defined inside the panel, not as saved objects. 43 of the 334 reference panels use one, and they were every Lens panel whose data view otherwise looked unresolvable.
- **Runtime fields** are Painless, evaluated per query. Nothing on the target runs Painless, so each becomes a ClickHouse expression written by hand — query-time enrichment, easy to miss because the panel looks like it reads an ordinary field.
- **Cross-cluster patterns** (`metrics-*,*:metrics-*`) read indices that are not in this Elasticsearch at all. Settle whether the target holds that data before mapping it.

Then inventory the target's keys and diff them, rather than assuming the mapping table:

```sql
-- target: every key actually present
SELECT arrayJoin(mapKeys(LogAttributes)) AS k, count() FROM otel_logs GROUP BY k ORDER BY 2 DESC
```

`clickstack_describe_source` is the other half: with a `Map` schema it is the only way to learn which keys exist, and it surfaces materialized columns (e.g. `geo_*`, `ua_*`) as real top-level columns — so the builder tools can reach them and you do not need a `clickstack_sql` fallback.

**Then triage, and pick what to migrate first:**

```bash
python3 "$SKILL"/scripts/inventory-panels.py dashboards.ndjson --triage
```

It splits every panel into *ready* (a direct translation exists), *decide* (translatable, but something is lost or must be re-expressed — it names which) and *blocked*, and nominates a first dashboard: decision-free, and exercising as many distinct tile types as it can. `ready` means nothing is known to stand in the way — **not** that the tile will be correct. That is still step 6.

### 3. Declare the losses before building anything

Say this at the start of the migration report, not when you reach the panel.

**Two chart shapes cannot be migrated.** ClickStack's `displayType` vocabulary is `line`, `stacked_bar`, `table`, `number`, `pie`, `bar`, `heatmap`, `search`, `event_patterns`, `markdown`, `sql`.

- **Maps.** There is nowhere to put a lat/lon pair, so a geo map degrades to a bar/table on country code. This holds even when the geo *data* migrated perfectly — the gap is in the rendering layer.
- **Heatmaps with a categorical axis.** The name matches, which is the trap: ClickStack's `heatmap` takes exactly one series, a numeric `valueExpression`, and **no `groupBy`**. It is a value-distribution heatmap (the trace-latency shape). A Kibana heatmap of `terms(host.name)` x time x `average(...)` degrades to a line chart grouped by that field.

> **Check the destination's chart types before promising a dashboard, not just its schema.**

For index-time enrichment (geo, user agent, anything an ingest pipeline computed), the honest framing is narrower than "it cannot be recovered": what does not travel is the *computation*, and re-expressing it on ClickHouse costs a dictionary, not a pipeline. See `references/enrichment.md`.

### 4. Translate

Build the field map first (`references/field-mapping.md` — ECS → `LogAttributes` patterns, required casts, and the expressions for fields Elastic derived at index time), then translate panel by panel.

Prefer `clickstack_table` and `clickstack_timeseries` over `clickstack_sql`: the builder tools are more reliable and produce chart-ready results. Reach for `sql` tiles only when a builder cannot express the shape.

**Reproduce the source; do not improve on it.** When the target can express something *better* than the source panel did, that is a suggestion to make, not a migration to ship. On a real migration a rate panel was translated with `increase` (the fleet total across hosts) instead of Kibana's `differences(of max())` (one host's rate), on the grounds that the fleet total is the number anyone actually wants. It is — and it was still wrong: a single-node burst that Kibana renders as a 1.8x spike is diluted to noise once three nodes are summed, and the user spotted the difference on the chart immediately. Migrate faithfully, verify against the source, then offer the improvement as a separate tile. A "deliberate deviation" you declared in a provenance note is still a dashboard that does not match.

**On a metric source, check what `aggFn` actually operates on.** ClickStack de-cumulates a cumulative Sum *before* aggregating, so every `aggFn` acts on the per-series per-bucket **increase**, never on the counter value. A Kibana `max(some.counter)` panel therefore has **no builder equivalent** and needs a `sql` tile; and `differences(of max(V))` is not `increase` once there is more than one series. Full measurements in `references/clickstack-tiles.md`.

**`seriesLimit` bypasses the select-item `where` — so do not set it at all on a shared table.** On `line`/`stacked_bar` it ranks the top N series over *unconditional* volume, so a tile can query fine and render an empty chart. This has fired **twice** here; the second time the top bucket was the empty string with 1.4M rows from four other services that lack the field. **No verification path catches it:** the builder tools have no `seriesLimit` parameter, so re-issuing the tile through them cannot reproduce it; `query_tiles` said `hasData: true`; a full-distribution diff passed. The only defence is structural: assert the key is absent on every `line`/`stacked_bar` tile. Note also that a **pie/bar `limit` is persisted as `seriesLimit`**, so scope any such audit by `displayType`. Details in `references/clickstack-tiles.md`.

**The trap that silently doubles every number:** builder tiles have **no tile-level `where`**. For `line`, `stacked_bar`, `table`, `pie`, `bar` and `number`, the filter goes on **each `select` item** (it compiles to `countIf(…)`). Only `search` and `event_patterns` tiles accept `config.where`. Full tile schema and the `where` → `aggCondition` persistence mismatch: `references/clickstack-tiles.md`.

**Carry the source's dataset predicate onto every tile.** A Kibana panel scoped to one data stream (`data_stream.dataset: nginx.access`) becomes an explicit predicate on the target (e.g. `LogAttributes['log.stream'] = 'access_json'`). If the target ingests the same events by more than one route, omitting it double-counts everything.

**Filters live at two levels and a panel that looks unfiltered usually isn't.** The dashboard object carries its own filters, which apply to every panel. On the stock `[Logs Nginx] Overview`, three of seven panels carry no panel-level filter and inherit `data_stream.dataset: nginx.error, nginx.access` from the dashboard. Because that inherited filter is typically an OR over several datasets, you cannot paste it onto each tile — decide the scope per panel. `inventory-panels.py` prints dashboard-level filters in a header above the table so they stay visible.

### 5. Create

`clickstack_save_dashboard` with no `id` creates; then use `clickstack_patch_dashboard` for tile-by-tile fixes rather than resubmitting the whole object. Tag the result (e.g. `migrated-from-kibana`) so `clickstack_search_dashboards` can find the set later. Saved searches go through `clickstack_save_saved_search` with the mapped column list.

Add one `markdown` tile per dashboard recording provenance: source dashboard id, migration date, and any panel that degraded. The dashboard should explain itself without this file.

### 6. Verify — in four passes, and none of them is redundant

`clickstack_query_tiles` runs every tile in one call. Compare each against a number obtained from the **source platform**, not against expectation.

Two rules that decide whether the numeric pass means anything:

- **Diff the whole distribution, not the top-N you wrote down.** Matching three values you happened to record is not the same as matching the panel. Pull the full `terms` aggregation from the source and diff bucket-for-bucket — it costs one query and it is the only way to know. On the reference migration this is exactly what exposed two wrong answers that already looked finished.
- **Derive every tolerance; never fit one to the failure.** A bucket diff disagrees for
  source-side reasons that are not migration errors: `scaled_float`/`float` fields are
  quantised in doc_values, and one platform may store whole seconds where the other keeps
  milliseconds. Read the bound off `_mapping` or compute it from the data. For the timestamp
  case assert the *shape* of the disagreement — no bucket moves by more than one second's
  worth of events, and the deltas conserve — rather than equality.
- **Verify the verifier.** A suite reports what it executed, not what you intended, and
  the difference is always green: a dropped helper makes every call a "command not
  found" that prints nothing and counts nothing. Assert the check COUNT as well as the
  failure count, mutation-test each check, and when you refactor the harness diff its
  OUTPUT against the previous run rather than its exit code.
- **A tile can truncate itself.** A chart query's own row cap is legitimate syntax, so no
  structural check sees it, and every bucket it does return is correct, so no value check
  sees it either. Run each time-series tile capped and uncapped and compare row counts.
- **Compare totals and distributions, never a wall-clock window.** The two platforms are almost certainly not on the same clock, and a sub-second-precision difference in the source logs puts events on opposite sides of a bucket edge. Window-dependent comparisons produce failures that no query can fix. This has produced **two false bug reports** in the reference project — both times a static corpus seen through "Last 24 hours", where the count drifts every minute. When two UIs disagree, ask what range each is showing *before* querying anything.

#### 6a. Audit every tile against its panel, structurally — before anything else

```bash
python3 "$SKILL"/scripts/audit-tiles.py source.ndjson migrated.json --field-map map.json
```

Seven checks, each from a real failure and each mutation-tested: a **dropped dashboard control**, a **metric-source filter missing `sourceMetricType`** (which renders an empty dropdown), a **dropped metric** (the panel displays two values, the tile one), a **placeholder** left in stored SQL, **`seriesLimit`** on a line/stacked_bar, **chart type drift** from the panel's `seriesType`, and a **field-mapping mismatch**. That last one needs the step-4 field map, because a metric tile references an OTel metric name rather than the source field — no heuristic bridges `process.cpu.pct` to `system.process.cpu.utilization`, and the audit lists such pairings for review rather than guessing. Exit status gates the migration. Details and its limits: `references/verification.md`.

This exists because a person comparing charts caught **ten** bugs on the reference migrations that a green suite did not, and most of them were wrong *structure*, which is mechanically checkable. It finds in a second what 6c finds in ten minutes.

#### 6b. Then diff every series bucket for bucket

Totals are not enough, and neither is a spot check. Diff each series per bucket against the source, comparing the **key sets** as well as the values.

This is the pass that catches a series which is the right shape and quietly wrong. In the reference project it found **8 wrong series across two migrations that had already passed their full suites and been eyeballed** — builder gauge tiles off by about 1% (3.63 against 3.67), which no chart and no whole-window average can show. Run the tile's **stored** `sqlTemplate` for `sql` tiles, and re-issue the tile's own `select`/`groupBy`/`where` through `clickstack_timeseries` for builder tiles; a hand-written "equivalent" re-encodes whatever misunderstanding produced the tile. Normalise the bucket keys first — Elastic renders `15:00` and ClickHouse `15:00:00`, and joining the raw strings gives an empty intersection that reads as "everything is broken". Full recipe in `references/verification.md`.

#### 6c. Finally, open both dashboards side by side and compare them tile by tile

**Do not skip this, and do not treat it as a formality.** On the reference migrations **six** tile bugs were caught by a person looking at charts — and the last three were found *after* bucket-for-bucket diffing was already green, which is the strongest argument there is for keeping this pass. `query_tiles` reported `status: ok` with a plausible row count for every one.

The first three were **valid query, wrong series**:

| what was wrong | what the chart showed |
|---|---|
| a tile subtracted two cross-host maxima instead of differencing per host | 1 spike where the source had 4 |
| a rate tile used `increase` (fleet total) where the source used `differences(of max())` (one host) | a 1.8× single-node spike diluted into noise |
| a tile aggregated across a `terms` breakdown the source panel splits by | 5 series instead of 10 |

Aggregate totals matched in every case. That is why totals alone cannot catch this class.

The other three were **right values, wrong shape** — invisible to any comparison of numbers, because every number was correct:

| what was wrong | what the chart showed |
|---|---|
| a saved search translated with a `GROUP BY` over columns that never change | 1 row where the source listed 2,760 |
| 4 tiles built as `line` where every source panel was `seriesType: bar_stacked` | bars on one side, lines on the other |
| a panel whose source field was constant 0 | both charts empty, both platforms agreeing perfectly on nothing |

The last one is worth internalising: **a tile can match the source in every bucket and still be worthless.** Verification compares two platforms; it never asks whether the panel has anything to show. `references/verification.md` has the one-line query for it.

Four things to compare per tile, in this order — they are ordered by how often they catch something:

1. **Series count and legend.** Same number of series, same names? A source panel's `terms` breakdown is a **dimension**; if the legend shows host names on one side and metric names on the other, a dimension was dropped.
2. **Shape.** Do spikes and dips fall in the same buckets, with the same relative prominence? A spike that is present but flatter usually means series were summed that the source kept apart.
3. **Density.** Is every series continuous across the window, or fragmentary? Gaps mean series truncation, not missing data.
4. **Magnitude.** Same order of magnitude per series? A tile reading 3× the source is often a fleet total standing in for a single series.

Mechanise what you can, so the visual pass is a backstop rather than the only defence. Each failure above has a cheap automated counterpart, all described in `references/verification.md`:

- run the tile's **stored** `sqlTemplate`, not a hand-written equivalent;
- **derive the expected value from the SOURCE PANEL's field, never from the field the tile reads** — otherwise the check is a tautology and a wrong-field tile passes. This cost a day: a tile read `system.process.cpu.total.norm.pct` where the panel aggregates `process.cpu.pct`, which is the same quantity *not* normalised by core count, so it was wrong by 4x-16x and every check was green;
- assert `uniqExact(series)` **and** `uniqExact(ts)`, not just row count;
- scan every source panel for `terms()` on a dimension field and assert the tile references it;
- assert the tile's `displayType` set against the source's `seriesType` set, so a hand-picked chart type cannot drift from the panel it came from;
- flag any metric with `uniqExact(Value) = 1` — a tile that can only draw a flat line.

Details, including what residual disagreement is expected and what is a real bug: `references/verification.md`.

### 7. Record what did not survive

Every migration has a residue. Write it down with the *reason*, classified:

| class | example | recoverable? |
|---|---|---|
| rendering-layer gap | map panel → country bar; multi-ring donut → flat pie | no |
| enrichment not in target | `user_agent.name` | yes, via dictionary |
| aggregation shape differs | nested `terms` datatable → global top-N table | no |
| third-party dataset differs | GeoLite2 vs DB-IP country | no — migrate the *database* to fix |
| precision differs at source | second vs millisecond timestamps | no |
| **source parses it wrong** | IPv6 client address truncated to its last hextet by the source's own grok | n/a — the target is already right |
| **collection gap** | the signal is not in the target collector's *default* set — an optional metric that is off, or one no standard receiver exposes | yes, by changing collector configuration, not the tile |

Two rows mislead people. **Third-party dataset differs** is the one read as a bug: geo is the enrichment where "same data, same query" still does not mean "same answer", because the answer depends on a dataset that belongs to neither platform.

**Collection gap** is the one that is not a dashboard problem at all: the panel's signal is not arriving, so no tile can be written. Its resolution is a collector change, not a tile change — which is why the collector configuration belongs in the migration's scope.

> **Before declaring one, read the receiver's `metadata.yaml`.** In the reference project this class was claimed twice and was wrong both times. An apache panel was recorded as unmigratable because "the standard apachereceiver emits no async-connection metric" — it emits `apache.connections.async` and enables it *by default*; the panel had been dropped on an assumption never checked against the target, and the data was already in the corpus. A postgres panel was then recorded the same way because `postgresqlreceiver` has no per-statement metrics — true, but `sqlqueryreceiver` scrapes `pg_stat_statements` directly, so the answer was a second receiver rather than a lost panel.
>
> What survives as a genuine gap is narrower and more useful: **the metric exists but is not enabled by default** (`system.cpu.utilization` is optional; the default is cumulative `system.cpu.time`), or **it needs a receiver nobody configured**. Both are answered by collector config, and neither is a reason to abandon a panel.

**Source parses it wrong** is the row that inverts the exercise, so say so explicitly when it happens. "Verify against the source platform" quietly assumes the source is correct, and sometimes it is not: one migration turned up three defects in Elastic's own apache parsing — IPv6 client addresses truncated to their final hextet and filed under `source.domain`, `url.original` dropped whenever the request line contained a backslash, and a trailing dot on `user_agent.version` from a capture group that matched the empty string. None raised `_grokparsefailure`, and none were visible from reading panel definitions; only diffing whole distributions surfaced them.

When it happens, do not "fix" the target into agreeing. Assert the disagreement in the verification script, with the reason, so nobody reintroduces the source's bug in the name of matching it.

## References

| file | read it when |
|---|---|
| `scripts/audit-tiles.py` | after creating tiles, before the visual pass — structural diff of every tile against its panel |
| `references/sources.md` | mapping data views to sources; ad-hoc views, runtime fields, cross-cluster |
| `references/field-mapping.md` | translating any field; ECS → `LogAttributes`, casts, derived expressions |
| `references/clickstack-tiles.md` | writing a tile; schema, `displayType` table, filter placement |
| `references/kibana-export.md` | a panel type is unrecognized, or you need the export shape |
| `references/enrichment.md` | a panel needs geo, user-agent, or other ingest-pipeline output |
| `references/verification.md` | verifying tiles, or explaining a mismatch |

## Validation status — read this before trusting a number in here

This procedure was built from, and run against, a **narrow set of environments**. That is the
main risk in using it, so it is stated rather than buried: where a claim was measured, it is
reliable; where it was reasoned, it may be wrong in your deployment.

| area | status |
|---|---|
| Panel parsing (`inventory-panels.py`) | **Measured.** 347 real panels across the nginx, apache, system, mysql and kubernetes integrations — Lens (formBased + textBased), legacy `visState`, TSVB, timelion, maps, by-reference and by-value panels. Zero unclassified. Filter rendering was fixed 2026-09-17: a **`combined`** filter's `meta.relation` was being dropped, so an OR read as an AND — which inverts a panel's meaning and made one working stock panel look unmigratable |
| Data-view resolution and `--sources` | **Measured** on the same 334 panels: all six reference shapes, including 43 ad-hoc data views, 12 with Painless runtime fields, and a cross-cluster pattern. The only panels left with no data view are the 40 prose panels, which correctly read nothing |
| `--triage` | **Measured** on the same estate (37 dashboards, 347 panels): **128 ready / 216 decide / 3 blocked**. The heatmap rule was added 2026-09-17 after ClickStack's heatmap turned out to have no categorical axis — it had been calling two unmigratable panels "ready". It was 140/189/3 until the mysql migration showed the metric rules were too lenient — a panel doing `average()`/`max()` on a metric's *value* has no builder path whether the field is a Sum or a Gauge, and a `table` tile takes no row limit, so `terms(f) size=N` on a datatable needs SQL. 22 panels moved from ready to decide, which is the more honest split |
| The visual pass (step 6c) | **Measured, by failing six times.** Six tile bugs passed a fully green numeric suite and were caught only by comparing charts — and the last three survived bucket-for-bucket diffing too, because they were shape errors whose every value was correct (a saved search collapsed to one row, four chart types hand-picked away from the source, and a panel whose field was constant). The protocol in `references/verification.md` is written from those six |
| **Bucket-for-bucket diffing** | **Measured, and it found what the visual pass could not.** The mysql migration diffed all 42 tile series against Elasticsearch per bucket, and the same method then exposed **8 wrong series in two earlier migrations that had passed 22/22 and looked right on screen** — gauge tiles off by 3.63 vs 3.67. Totals, whole-window averages and charts all miss this. Diff buckets |
| Tile schema, `displayType` vocabulary, filter placement, `aggFn`, `quantile` levels | **Measured**, read off a live `save_dashboard` schema — but on **ClickStack 2.35.0-beta only**. Re-run `introspect-clickstack.py`; it exists for exactly this |
| The `seriesLimit` trap, the `where`→`aggCondition` read-back mismatch, the `whereLanguage` default | **Measured**, each by isolating the one key that caused it |
| Five end-to-end integrations (nginx, apache, postgresql, mysql, system) | **Measured** — 17 dashboards, logs *and* metrics for each, verified against the source's own numbers. mysql added the first **multi-line** log format (a line-oriented reader produces 5x the rows and no error); `system` added the widest shape yet — **10 data streams**, two of which derive `@timestamp` in *opposite* ways inside one integration |
| Auth (API key + basic) and Kibana **Spaces** | **Measured** against Kibana 8.15: an API key created via `POST /_security/api_key`, a dashboard copied into a second space, and pagination forced to 10/page to exercise the loop |
| ECS → OTel **semconv** field mapping | **Reasoned, not measured.** Derived from the semconv/ECS alignment. Every row needs checking against `mapKeys(LogAttributes)` on the actual target |
| **Metrics** dashboards | **Measured across five dashboards** (nginx, apache, postgresql, mysql ×2), 2026-09-16 — OTLP ingest, the Sum/Gauge split, and 22 `sql` tiles. It has now corrected **two** wrong claims in this skill: cumulative Sums de-cumulate before `aggFn` (so a raw-counter panel is not a builder tile), and — from mysql — a **gauge is collapsed to one sample per bucket before `aggFn`**, so `average()`/`max()` over a gauge's samples is not a builder tile either. Still untried: histogram and summary metric types, delta temporality, `isDelta` on gauges |
| **Traces / APM** dashboards | **Not covered at all.** No guidance, and the `quantile` restriction to 0.5/0.9/0.95/0.99 is a real obstacle for APM latency panels |
| Kibana versions other than **8.15** | **Untested.** `inventory-panels.py` does read 7.x shapes — `datasourceStates.indexpattern` Lens layers and `input_control_vis` control panels — but no 7.x export has been exercised end to end, and 9.x is unverified |
| Multiple ClickStack **sources** | **Partly measured.** Migrations have targeted one logs source and one metric source, and a metric source fans out to `otel_metrics_{gauge,sum,histogram,summary}` with `metricType` selecting the table — a single tile joining two of those tables needs raw SQL. Untested: a dashboard whose panels span several *logs* sources |
| Elastic **Cloud / Serverless** as the source | **Untested end to end.** Auth and spaces are handled, but nothing else has been exercised there |

Two consequences worth acting on:

- **Migrate one dashboard end to end before batching.** It is the only way to find out which
  of the untested rows above applies to you — and, as the `seriesLimit` trap showed, the only
  way to catch a tile that queries successfully and renders nothing.
- **Do not present a rendered tile as verified, and do not present a green check suite as
  verified either.** Step 6 exists because every wrong tile in the reference migrations
  rendered perfectly; **step 6c exists because six of them also passed every numeric check
  I had written, three of those even after per-bucket diffing.** Both passes, every time.
