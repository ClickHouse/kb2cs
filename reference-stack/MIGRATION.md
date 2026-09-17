# Migrating the nginx dashboards from Elastic to ClickStack

A repeatable recipe for moving the two prebuilt Kibana nginx dashboards onto ClickStack,
driven by MCP servers. Everything below was measured against the two running stacks
(`stack/elastic`, `stack/clickstack`) holding the same 499,964 requests, so the numbers are
verification targets rather than illustrations.

**This recipe has been run end to end.** All ten panels were migrated and every tile
validated with `clickstack_query_tiles` against the Expected column below. See
[Executed result](#executed-result) for what came out of it — including two places where
running it disproved what this document originally predicted.

**Scope: nginx logs.** The apache logs dashboard has since been migrated by this same recipe
— see [Apache, by the same recipe](#apache-by-the-same-recipe). Three other integrations
(system, mysql, kubernetes) plus both metrics dashboards have had their panels read and
inventoried but *not* migrated — 31 dashboards, 315 panels, nothing written to the target.
[`INTEGRATIONS.md`](INTEGRATIONS.md) records which is which and why, and confirms at 30× the
sample that the map is the only panel class with no target chart type.

## Before you start

Both stacks up, both loaded, and both MCP servers connected:

```bash
cd stack/elastic     && docker compose up -d && ./verify.sh
cd ../clickstack     && docker compose up -d && ./load.sh && ./verify.sh && ./geoip.sh
cd ../..             && ./stack/clickstack/write-mcp-config.sh
# then restart Claude Code from the repo root and APPROVE both servers at the prompt
claude mcp list      # both must read "Connected", not "Pending approval"
```

The approval prompt is the step that is easy to miss. MCP servers attach at session start, so
approving them cannot affect a session that is already running.

## The tools you actually have

This is the first thing to understand, because it is asymmetric.

| | tools | can it read/write dashboards? |
|---|---|---|
| `elasticsearch` MCP | `list_indices`, `get_mappings`, `search`, `esql`, `get_shards` | **No.** Data only |
| `clickstack` MCP | 30 tools incl. `save_dashboard`, `patch_dashboard`, `query_tile(s)`, `describe_source`, `table`, `timeseries`, `sql` | **Yes**, full CRUD |

So the source definitions do **not** come from MCP. They come from the Kibana Saved Objects
API:

```bash
curl -s -u elastic:changeme -X POST "http://localhost:5601/api/saved_objects/_export" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{"objects":[{"type":"dashboard","id":"nginx-55a9e6e0-a29e-11e7-928f-5dbe6f6f5519"},
                  {"type":"dashboard","id":"nginx-046212a0-a2a1-11e7-928f-5dbe6f6f5519"}],
       "includeReferencesDeep":true}' > dashboards.ndjson
```

Two parsing notes that cost time: the panels are stored **by value** inside
`attributes.panelsJSON` (a JSON string), not as referenced saved objects, and the fields you
want are `sourceField` inside `datasourceStates.formBased.layers.*.columns`.

`@elastic/mcp-server-elasticsearch` is also deprecated in favour of the Agent Builder MCP
endpoint in Elastic 9.2+. On 8.15 it still works and is useful for `get_mappings` when you
need to confirm a field's type before translating it.

## Field mapping

Verified by inventorying both sides: `mapKeys(LogAttributes)` on `otel_logs`, and the
populated fields of a real `logs-nginx.access-default` document.

The shapes differ fundamentally. Elastic's integration produces **ECS, flattened into typed
fields at index time**. ClickStack's collector produces **raw nginx names inside a
`Map(LowCardinality(String), String)`**, so everything is a string and needs casting.

### Direct equivalents

| Kibana (ECS) | ClickStack | note |
|---|---|---|
| `@timestamp` | `Timestamp` | real column, `DateTime64(9)` |
| `http.response.status_code` | `LogAttributes['status']` | **string** — `toUInt16()` to compare or range |
| `url.original` | `LogAttributes['request_uri']` | includes the query string on both sides |
| `http.request.method` | `LogAttributes['request_method']` | |
| `http.response.body.bytes` | `LogAttributes['body_bytes_sent']` | `toUInt64()` before `sum()` |
| `http.request.referrer` | `LogAttributes['http_referer']` | `-` when absent, not null |
| `http.version` | `LogAttributes['server_protocol']` | `2.0` vs `HTTP/2.0` — strip the prefix |
| `source.address` / `source.ip` | `LogAttributes['remote_addr']` | |
| `user_agent.original` | `LogAttributes['http_user_agent']` | |
| `log.level` (error stream) | `LogAttributes['level']` | also `SeverityText`, but coarser |
| `message` (error stream) | `LogAttributes['msg']` | |
| `data_stream.dataset` | `LogAttributes['log.stream']` | `nginx.access` → `access_json` / `access_combined` |

### Needs deriving on ClickStack

Elastic computed these at index time; on ClickStack they are query-time expressions.

| Kibana (ECS) | ClickStack expression |
|---|---|
| `url.path` | `splitByChar('?', LogAttributes['request_uri'])[1]` |
| `url.extension` | `extract(LogAttributes['request_uri'], '\\.([a-z0-9]+)(\\?\|$)')` |
| `event.outcome` | `if(toUInt16(LogAttributes['status']) >= 500, 'failure', 'success')` |
| `user_agent.name` | `ua_browser` — a real column, via `ua.sh`. See [below](#why-operating-systems-and-browsers-are-not-on-this-list) |
| `user_agent.version` | `ua_browser_version` |
| `user_agent.os.name` | `ua_os` |
| `user_agent.os.version` | `ua_os_version` |
| `user_agent.device.name` | `ua_device` |

### Geo — provided by `geoip.sh`, as real columns

These are **not** in `LogAttributes`; they are materialized columns, so query them directly.

| Kibana (ECS) | ClickStack |
|---|---|
| `source.geo.country_iso_code` | `geo_country_code` |
| `source.geo.city_name` | `geo_city` |
| `source.geo.location.lat/lon` | `geo_latitude` / `geo_longitude` |
| `source.geo.country_name`, `region_name`, `continent_name` | **absent** — DB-IP Lite has no country/region names |
| `source.as.number`, `source.as.organization.name` | **absent** — no ASN dataset loaded |

### Present in ClickStack, absent from Elastic

Worth knowing because it reframes the migration: the target is **richer** than the source.
The Elastic stack ingests `access.log` (stock combined format), which carries no latency at
all. ClickStack also ingests `access.json.log`.

`request_time`, `request_time_ms`, `upstream_addr`, `upstream_status`,
`upstream_response_time`, `upstream_connect_time`, `upstream_header_time`, `request_id`,
`connection`, `connection_requests`, `gzip_ratio`, `bytes_sent`, `request_length`,
`ssl_protocol`, `ssl_cipher`, `hostname` (edge node).

So p95 latency, upstream error attribution and per-node breakdowns are dashboards you can
build on the target that the source could not express.

## The panels

Ten, across two dashboards — seven on Overview, three on Access and error logs, numbered
1–10 in the tables below. (An earlier draft said "nine" here while its own tables listed
ten; the tables were right.) The two `Dashboards` navigation panels are `links` panels, not
migratable, and not counted.

### [Logs Nginx] Overview

| # | Panel | Source spec | Target | Expected |
|---|---|---|---|---|
| 1 | Nginx logs | map over `source.geo.location` | ⚠ no map tile type on the target — migrated as a `bar` on `geo_country_code` | US 182,300 · CN 46,221 · JP 25,800 |
| 2 | Response codes over time | `date_histogram` + `filters` on status ranges | `clickstack_timeseries`, group by `intDiv(toUInt16(status),100)` | 200 376,256 · 304 92,544 · 404 18,987 · 403 2,801 |
| 3 | Errors over time | `date_histogram` + `terms` on `log.level` | error stream, `terms` on `LogAttributes['level']` | warn 5,438 · error 5,097 · info 622 · crit 172 · notice 135 · alert 12 |
| 4 | Top pages | `terms` on `url.original`, size 10 | `terms` on `LogAttributes['request_uri']` | css 32,030 · js 31,734 · logo.svg 29,568 |
| 5 | Data Volume | `sum(http.response.body.bytes)` over time | `sum(toUInt64(LogAttributes['body_bytes_sent']))` | **10.56 GB** total |
| 6 | Operating systems | donut, `terms` on `user_agent.os.name` + `.version` | `pie` on `ua_os`, a materialized column built by `ua.sh` — matches Elastic exactly | Windows 133,594 · iOS 103,549 · Android 80,648 |
| 7 | Browsers | donut, `terms` on `user_agent.name` + `.version` | `pie` on `ua_browser`, same source — all 20 buckets exact | Chrome 118,541 · Mobile Safari 88,076 · Chrome Mobile 71,194 |

### [Logs Nginx] Access and error logs

| # | Panel | Source spec | Target | Expected |
|---|---|---|---|---|
| 8 | Access logs over time | `date_histogram` on `@timestamp`, count | `clickstack_timeseries` on `Timestamp` | 499,964 over 24h, diurnal 4.7k–34.3k/hr |
| 9 | Nginx access logs | saved search, columns `url.original`, `http.request.method`, `http.response.status_code`, `http.response.body.bytes` | `clickstack_save_saved_search` with the mapped columns | — |
| 10 | Nginx error logs | saved search, columns `log.level`, `message` | same, on the error stream | 11,476 rows |

**Filter every panel.** The ClickStack equivalent of Kibana's dataset scoping is
`LogAttributes['log.stream'] = 'access_json'` — without it you double-count, because
`access_combined` holds the same 499,964 requests again.

Note *where* that scoping comes from on the source side, because it is not uniform: the
dashboard object carries `data_stream.dataset: nginx.error, nginx.access` and **three of the
seven Overview panels — the map, Operating systems and Browsers — carry no panel-level filter
at all**, inheriting it. Read only the panel filters and those three migrate unscoped. And
because the inherited filter is an OR over *both* datasets, it cannot be pasted onto a tile
as-is; the per-panel scope is a decision, not a copy.

## The one panel that cannot be migrated faithfully

Say this up front rather than discovering it at panel 1.

**The map.** This is the only genuine loss, and it is not a data problem — `geoip.sh` closes
the data gap with DB-IP and a ClickHouse dictionary (close to Elastic's MaxMind on the top
countries, though not uniformly; see [Executed result](#executed-result)). The problem is the
**rendering layer**:
ClickStack has no map tile type. The `displayType` vocabulary is exactly **ten** values —
`line`, `stacked_bar`, `table`, `number`, `pie`, `bar`, `heatmap`, `search`,
`event_patterns`, `markdown` — and there is nowhere to put a lat/lon pair. `geo_latitude` and
`geo_longitude` are queryable but not plottable, so the panel degrades to a country bar
chart.

`sql` is **not** in that list, though an earlier draft counted it as an eleventh. A raw SQL
tile is a separate tile branch keyed by `configType: "sql"`, carrying a `sqlTemplate` plus a
`displayType` restricted to the six chart types — so it *renders as* a chart rather than
being one. Read the live vocabulary off the server rather than off this file:

```bash
python3 ../skill/scripts/introspect-clickstack.py \
  --key-cmd ./stack/clickstack/personal-key.sh
```

Note also what changed operationally even where the data survived: Elastic's geo is frozen
into each document at index time, while the ClickStack version is a dictionary lookup you can
repoint and recompute with a mutation. Same numbers, different lifecycle.

### Why Operating systems and Browsers are *not* on this list

They look like they should be, and an earlier draft of this document listed them as
un-migratable. Running the migration disproved that — twice, in two different ways.

**The production answer: `ua.sh`.** ClickHouse's `regexp_tree` dictionary layout exists for
precisely this job, so the UA gap closes the same way the geo gap does — a third-party corpus
loaded into a dictionary and exposed as `MATERIALIZED` columns:

| | Elastic | ClickStack |
|---|---|---|
| geo | `geoip` processor + GeoLite2, at index time | `ip_trie` dictionary over DB-IP → `geo_*` columns (`geoip.sh`) |
| user agent | `user_agent` processor + uap-core, at index time | `regexp_tree` dictionary over uap-core → `ua_*` columns (`ua.sh`) |

The one trick that makes it exact: **`ua.sh` extracts the regex corpus from the running
Elasticsearch container's own `ingest-user-agent` jar** rather than downloading it from
upstream. uap-core is versioned and releases disagree about some agents, so using the source
platform's own copy makes the two agree *by construction*. Result: **all 20 browser buckets and
all 5 OS buckets match exactly**, and the tile `groupBy` drops from 1,620 characters to
`ua_browser`.

Note the three dictionaries, not one. A `regexp_tree` lookup returns the attributes of the
first node that matches, so browser and OS patterns in a single tree shadow each other;
uap-core treats its three parser lists as independent passes and so must the target.

**The other answer, kept here because it is the more interesting finding**, is that you did
not strictly *need* any of that:

Elastic's integration runs a `user_agent` processor at index time, producing
`user_agent.name`, `.version`, `.os.name`, `.os.version`. The ClickStack collector does no UA
parsing — only the raw string survives. That much is true. But this dataset contains only
**31 distinct user-agent strings**, so a query-time `multiIf` over `http_user_agent`
reproduces Elastic's ua-parser output *exactly*: **all 20 browser buckets and all 5 OS
buckets match Elastic on both label and count**, verified bucket-for-bucket against a
`terms` aggregation on `user_agent.name` / `user_agent.os.name`.

Getting to exact rather than merely close took four rules, and the last two are the ones you
would not guess:

- Test `Edg/` before `Chrome/` — every Edge UA also contains `Chrome/`.
- Test `Chrome/ AND Mobile Safari` before plain `Chrome/`, and `Mobile/15E148` before
  `Safari/605.1.15`, or mobile traffic lands in the desktop bucket.
- **The two `ShopMobile/…` native-app agents do not share a bucket.** ua-parser reads the
  platform token inside the parentheses and splits them: `ShopMobile/4.12.1 (iOS 18.6; …)`
  becomes `Mobile Safari UI/WKWebView` (15,473) while `ShopMobile/4.11.0 (Android 15; …)`
  becomes plain `Android` (9,454) — a browser bucket named after an OS. Lumping them into one
  `ShopMobile` slice is the intuitive move and it is wrong.
- **Elastic's `Other` is a specific set, not a remainder.** It is exactly `python-urllib3`,
  `zgrab`, `kube-probe`, and `ELB-HealthChecker` (23,464 combined). `python-requests`, by
  contrast, gets the proper name `Python Requests`. You have to enumerate which agents
  ua-parser recognises; you cannot infer it.

The OS side has its own version of that last trap: Elastic emits **no** `user_agent.os.name`
at all for bots and HTTP clients — the field is simply absent, so those 115,558 documents
appear in no OS bucket rather than in an `Other` one. The five named buckets (Windows
133,594 · iOS 103,549 · Android 80,648 · Mac OS X 54,539 · Linux 12,076) sum to 384,406, not
499,964. A ClickHouse `multiIf` has no "absent" — it must emit *something* — so the migrated
panel carries an explicit `Other` slice that Kibana's donut does not show. Same partition of
the data, one more visible slice.

So the honest generalisation is narrower than "index-time enrichment cannot be recovered":

> **Index-time enrichment does not travel with the data, but recreating it is a choice of
> mechanism, not a loss.** Over 31 user agents a hand-written `multiIf` is exact and cheap.
> Over the open web it is neither — but a `regexp_tree` dictionary over the same corpus the
> source platform used is exact *and* maintainable, and needs no re-ingest. What does not
> travel is the *computation*; on ClickHouse you re-express it at query time and it costs a
> dictionary, not a pipeline.

**Prefer `ua.sh` over the `multiIf` in practice**, for a reason that has nothing to do with
correctness: the hand-written expression lived in three places — both tiles and the
verification script — and a 32nd user agent means editing all three, with nothing to catch a
miss. The dictionary is defined once.

There is a second lesson in *how* this was established. The first attempt matched the three
values printed in the Expected column and looked finished; the ShopMobile split and the
composition of `Other` only surfaced when the full aggregation was pulled from Elasticsearch
and diffed bucket-for-bucket. **Matching the top-N you happened to write down is not the same
as matching the panel.** If the source platform is still running, aggregate it and diff the
whole distribution — it costs one query and it is the only way to know.

The map is the opposite shape of problem, and the more instructive one: the data migrated
perfectly and the panel still could not be reproduced, because the target's visualization
vocabulary is smaller than the source's. **Check the destination's chart types before
promising a dashboard, not just its schema.**

## Procedure

1. **Export** the two dashboards from the Kibana Saved Objects API (above).
2. **`clickstack_describe_source`** on the logs source *before* writing any query. Its own
   description says this prevents unknown-column errors, and with a `Map` schema it is the
   only way to know which keys exist. It **does** surface the `geo_*` materialized columns
   (`geo_country_code`, `geo_city`, `geo_latitude`, `geo_longitude`) as real top-level
   columns alongside the 42 `LogAttributes` keys, so the geo panel needs no `clickstack_sql`
   fallback — the builder tools reach it directly.
3. **Translate** each panel using the tables above. Prefer `clickstack_table` and
   `clickstack_timeseries` over `clickstack_sql`; the server's own guidance is that the
   builder tools are more reliable and produce chart-ready results.
4. **Create** with `clickstack_save_dashboard` (omit `id`), then `clickstack_patch_dashboard`
   for tile-by-tile fixes rather than resubmitting the whole object.
5. **Verify with `clickstack_query_tiles`** — run every tile in one call and compare against
   the Expected column. This is what makes the recipe testable rather than aspirational: a
   tile that renders is not a tile that is correct.
6. **Record** any panel that could not be reproduced, and why.
7. **Re-verify the artifacts** with `./stack/clickstack/verify-migration.sh` — 16 checks that
   the four objects exist, that every tile carries its stream predicate, and that the
   expressions *stored inside the tiles* still reproduce Elastic's numbers. To rebuild both
   stacks from zero and run all of this end to end, follow [`RUNBOOK.md`](RUNBOOK.md).

## Verification cheat sheet

Independent of the dashboards, these should agree across both platforms:

| Question | Elastic | ClickStack |
|---|---:|---:|
| Total access requests | 499,964 | 499,964 (`log.stream='access_json'`) |
| 5xx | 2,534 | 2,534 |
| Distinct client IPs | 12,659 | 12,659 |
| Bytes sent | 10.56 GB | 10.56 GB |
| Error-log entries | 11,476 | 11,476 |

If a migrated tile disagrees with its Expected value, check the stream filter first — a
missing `log.stream` predicate doubles almost everything.

**Every check above is deliberately window-independent.** Compare totals and distributions,
never a wall-clock time range, because the two stacks are almost certainly not on the same
clock: each loader shifts the dataset to end at "now" and computes that shift when it runs, so
they end up apart by however long elapsed between the two loads. On the reference run that was
40 minutes, which made the local `00:00` half-hour bucket read 15,278 in Kibana and 14,310 in
HyperDX — the same traffic, 40 minutes offset, nothing missing.

Aligned to each dataset's own start, the two agree bucket for bucket to within a handful of
rows. `verify-migration.sh` reports the skew as an advisory check; `RUNBOOK.md` explains how to
load both from a common shift.

**Expect a small residual even when perfectly aligned, and do not try to fix it.** Elastic
parses `access.log` (stock combined, whole seconds) while ClickStack ingests `access.json.log`
(`msec`, milliseconds), so an event at `12:29:59.400` sits on one side of a bucket edge for one
platform and the other side for the other. Measured with the stacks 0 s apart: **0–3 rows per
30-minute bucket, ≤0.02%**. No query can correct it — the second-precision file simply does not
carry the sub-second information. Removing it would mean pointing Filebeat at the JSON log,
which would discard the combined-vs-JSON parsing contrast this dataset is built to demonstrate.
Compare totals, not buckets.

## Executed result

Run on 2026-08-20 against the live stacks, data window
`2026-08-19T14:04:24Z` → `2026-08-20T14:04:32Z`.

| Artifact | |
|---|---|
| `[Logs Nginx] Overview (migrated)` | 7 tiles + a provenance note, `query_tiles` 7/7 ok |
| `[Logs Nginx] Access and error logs (migrated)` | 3 tiles + a provenance note, `query_tiles` 3/3 ok |
| `Nginx access logs (migrated)` | saved search, columns per panel 9 |
| `Nginx error logs (migrated)` | saved search, columns per panel 10 |

Both dashboards are tagged `nginx` + `migrated-from-kibana`, so
`clickstack_search_dashboards` finds them by tag.

Measured against the Expected column:

| Panel | Expected | Measured |
|---|---|---|
| Response codes | 2xx 376,837 · 3xx 95,196 · 4xx 25,397 · 5xx 2,534 | exact |
| Errors by level | warn 5,438 · error 5,097 · info 622 · crit 172 · notice 135 · alert 12 | exact, all six |
| Top pages | css 32,030 · js 31,734 · logo.svg 29,568 | exact |
| Data volume | 10.56 GB | 10,557,094,271 B |
| Access total · distinct IPs | 499,964 · 12,659 | exact |
| Operating systems | Windows 133,594 · iOS 103,549 · Android 80,648 | exact — and all 5 OS buckets match |
| Browsers | Chrome 118,541 · Mobile Safari 88,076 · Chrome Mobile 71,194 | exact — and all 20 browser buckets match |
| Geo | US 182,300 · CN 46,221 · JP 25,800 | 177,022 · 46,921 · 26,249 (−2.9% / +1.5% / +1.7%) |

The UA panels were not merely checked against the three values above — the full
`user_agent.name` and `user_agent.os.name` distributions were pulled from Elasticsearch and
diffed against the migrated tiles bucket by bucket. Reproduce that check with:

```bash
curl -s -u elastic:changeme "http://localhost:9200/logs-nginx.access-default/_search" \
  -H 'Content-Type: application/json' \
  -d '{"size":0,"aggs":{"b":{"terms":{"field":"user_agent.name","size":30}},
                        "o":{"terms":{"field":"user_agent.os.name","size":15}}}}'
```

Note that `@elastic/mcp-server-elasticsearch@0.3.1` cannot run this: it sends
`compatible-with=9` accept headers, which ES 8.15 rejects with `media_type_header_exception`.
Use `curl` for aggregations against the 8.x stack.

Geo is the only panel that does not match exactly, and it is expected not to: DB-IP Lite and
MaxMind disagree about which country some synthetic ranges belong to. Everything else agrees
to the digit, which is the point — the two platforms are computing the same answers from the
same bytes.

**Do not read the geo row as a ±3% tolerance.** The top three happen to agree closely; the
disagreement widens sharply below them, and an earlier draft of this document quoted
"−2.9%/+1.8% on the top countries" on the strength of those three alone:

| | Elastic (GeoLite2) | ClickStack (DB-IP Lite) | Δ |
|---|---:|---:|---:|
| US | 182,300 | 177,022 | −2.9% |
| CN | 46,221 | 46,921 | +1.5% |
| JP | 25,800 | 26,249 | +1.7% |
| GB | 21,155 | 21,334 | +0.8% |
| DE | 18,129 | 18,883 | +4.2% |
| KR | 16,111 | 16,114 | +0.0% |
| FR | 12,753 | 13,363 | +4.8% |
| **CA** | **6,955** | **9,923** | **+42.7%** |

Canada is the one to look at: DB-IP places roughly 3,000 more requests there than MaxMind
does. Nothing is broken — the client IPs are synthetic, and their ranges fall into blocks the
two databases classify differently, so neither answer is more correct than the other.

The migration lesson is that **geo is the one enrichment where "same data, same query" still
does not mean "same answer"**, because the answer depends on a third-party dataset that is not
part of either platform. If a geo panel needs to match across a migration, migrate the
*database*, not just the lookup.

Three implementation notes worth carrying forward:

- **Builder tiles have no tile-level `where`.** For `line`, `stacked_bar`, `table`, `pie`,
  `bar`, and `number`, the filter goes on *each* `select` item (it compiles to `countIf(…)`).
  Putting the stream predicate in the wrong place is the easiest way to silently double every
  access-log number. The schema says so outright, marking `where` on those six as
  `{"not": {}}` with the description *"Not supported on this tile type. Filter via each
  select item's `where`."* — so this is enforced, not merely conventional.
  `heatmap`, `search` and `event_patterns` do take a tile-level `config.where`; note that
  `heatmap` sits with those two rather than with the chart types it resembles.
- **`whereLanguage` defaults to `lucene`, not `sql`.** Every `where` above is a ClickHouse
  expression, so each one must set `whereLanguage: "sql"` explicitly. Omit it and the
  expression is parsed as a Lucene query — it does not throw, it just stops meaning what you
  wrote.
- **`quantile` only accepts levels 0.5, 0.9, 0.95 and 0.99.** Not a constraint this migration
  hit, but any source panel showing p75 or p99.9 needs a SQL tile rather than a builder tile.
- **Panel 2 is grouped by status family**, `concat(toString(intDiv(toUInt16(status),100)),'xx')`,
  matching Kibana's range filters and the README's published invariants. The Expected column
  above lists per-code counts; group by `LogAttributes['status']` directly if you want those.
- The blank `geo_country_code` bucket (16,955 rows, IPs the dictionary does not cover) is
  relabelled `unknown` via `if(geo_country_code = '', 'unknown', geo_country_code)` so it does
  not render as an unlabelled bar. `ZZ` (17,280) is a *different* thing and worth not
  conflating: it is exactly the two health-checkers — `ELB-HealthChecker/2.0` and
  `kube-probe/1.30`, 8,640 requests each from one source IP each, all on `/health` — which
  DB-IP resolves to the reserved-range placeholder. Blank means "no dictionary entry"; `ZZ`
  means "matched, and the answer is *not a country*".

---

## Apache, by the same recipe

`[Logs Apache] Access and error logs` was migrated on **2026-09-16** using this document as
the procedure, on the same ClickStack 2.35.0-beta. It is the first time the recipe was applied
to something it was not written from, so what it got right and wrong is worth recording.

7 source panels → 8 tiles (7 + a provenance note), 20/20 on
`stack/clickstack/verify-apache.sh`. Full coverage detail is in
[`INTEGRATIONS.md`](INTEGRATIONS.md#apache--logs); ingest steps are RUNBOOK step 5b.

**What transferred unchanged.** The field mapping above needed two additions
(`apache.error.module` → `LogAttributes['module']`, and the version columns) and nothing else:
apache's `combined` LogFormat is byte-compatible with nginx's, so `status`, `request_uri`,
`remote_addr`, `http_user_agent` and `body_bytes_sent` all land under the same keys. That was
deliberate on the ingest side — `otel-collector-apache.yaml` emits *nginx's* attribute names
so the existing `geo_*` and `ua_*` materialized columns enrich both services. The filter trap,
the `whereLanguage` default and the map degradation all played out exactly as written.

**Four things this document did not warn about.** Each cost time:

1. **`seriesLimit` makes a tile ignore its own select-item `where` when choosing series —
   and on a shared table that renders an empty chart.** This one took two attempts to
   diagnose, and the first diagnosis was wrong.

   The symptom was `toUInt16(LogAttributes['status'])` failing on the saved `stacked_bar`
   with *"Cannot parse UInt16 from String, because value is too short"* while prototyping
   fine through `clickstack_table`. That looks like "a saved tile's `groupBy` is evaluated
   over every row", and a null-safe cast (`toUInt16OrZero`) does silence it — but the tile
   then **queried successfully and drew nothing**, which is strictly worse than the error.

   The actual cause is `seriesLimit`. Proved with two tiles identical but for that one key,
   both given a bare `toUInt16`: without it the query returns 307 rows, with it the cast
   throws. So `seriesLimit` adds a top-N series ranking pass that does **not** apply the
   select-item filter. Two consequences:

   - the `groupBy` expression sees every row in the window, including rows with no such
     attribute — hence the cast error;
   - the top N series are ranked by **unconditional** volume. `otel_logs` here holds
     1,011,404 nginx rows against 264,662 apache ones, so the ranking is decided by nginx
     and the five series it picks are close to empty once the apache filter is applied.

   **Fix: do not set `seriesLimit` on a tile whose scope comes from a select-item `where`,
   which on a shared table is every tile.** Both migrated stacked_bar tiles now omit it, and
   group on the raw string (`LogAttributes['status']`) — no cast, so null-safety is automatic.
   The cost is honest and small: Kibana's `terms size=5` becomes all 7 statuses.

   Note what *no* automated check caught here. `query_tiles` reported `status: ok` and
   `hasData: true` for the broken tile at every time range, because the query really did
   return rows — they were just rows of the wrong series. Only looking at the rendered
   dashboard surfaced it.
2. **ClickStack's dashboard-level `filters` are interactive dropdowns, not applied
   predicates.** They take an `expression` naming a *column* to offer values for, so unlike a
   Kibana dashboard filter they cannot scope anything on their own. Any inherited
   dashboard-level filter has to be pushed onto every tile.
3. **A two-ring Kibana donut flattens.** Both apache donuts break down by name *and* version;
   ClickStack groups on both and renders one slice per combination. Data intact, shape lost —
   a degradation worth declaring up front alongside the map.
4. **A Kibana datatable with two nested `terms` buckets is not a flat table.** `Top URLs by
   response code` is top-5 URLs *within* each of the top 5 statuses; the migrated table is a
   global top-N over (status, URL) pairs. Same numbers per pair, different row set.

**And three places where matching the source would have been wrong.** Diffing whole
distributions — rather than the handful of numbers in an Expected column — turned up three
defects in Elasticsearch's own parsing: IPv6 client addresses truncated to their last hextet,
`url.original` dropped whenever the request line contains a backslash, and a stray trailing
dot on `user_agent.version`. All three are listed with counts in
[`INTEGRATIONS.md`](INTEGRATIONS.md#where-the-source-platform-is-wrong) and asserted in
`verify-apache.sh`, so the next person does not "fix" ClickStack into agreeing with them.

That is the strongest argument in this document for the distribution diff. Panel definitions
were the same on both platforms; the numbers were not, and only a bucket-by-bucket comparison
said so.
