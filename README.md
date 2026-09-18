# kb2cs — migrating Kibana dashboards to ClickStack

Tooling and a written procedure for moving **Kibana (Elastic) dashboards to ClickStack /
HyperDX**, plus the part most migrations skip: **proving the migrated tiles show the same
numbers as the originals.**

A dashboard migration is three problems that fail in different ways, and the order matters:

1. **Schema** — ECS typed fields against a `Map(LowCardinality(String), String)`. Mechanical.
   Usually you translate the dashboards onto the OTel model, because if ingestion moves to the
   OTel collector then ECS names never arrive. If instead you need *existing* ECS rows in
   ClickHouse queryable without re-ingesting: **logs can be, metrics cannot** — measured, in
   [`skill/references/sources.md`](skill/references/sources.md).
2. **Visualization vocabulary** — the target has fewer chart types than the source. *Not*
   solvable; must be declared before you start translating.
3. **Index-time enrichment** — geo, user-agent, anything an ingest pipeline computed does not
   travel with the data. Recreatable, but as a different mechanism (a dictionary, not a
   pipeline).

Most wasted effort in a migration goes into translating a panel that was never going to
render, or one whose source field does not exist on the target.

---

## Using this on your own migration

`skill/` is a **Claude Code skill**, not a checklist. You do not walk the seven steps by
hand — you install it, export the credentials, and ask. Claude runs the scripts, calls the
ClickStack MCP tools to create the dashboards, and verifies its own output.

### Set it up once

```bash
# 1. install the skill
git clone https://github.com/ClickHouse/kb2cs && cd kb2cs
cp -r skill ~/.claude/skills/kibana-to-clickstack     # or keep it project-local

# 2. credentials and endpoints (see the full list below)
export KIBANA_URL=... KIBANA_API_KEY=...
export ES_URL=... ES_USER=... ES_PASSWORD=...
export CLICKSTACK_MCP_URL=... CLICKSTACK_API_KEY=...
export CLICKHOUSE_URL=... CLICKHOUSE_PASSWORD=...

# 3. give Claude Code the ClickStack MCP server, which is what CREATES the dashboards
cp .mcp.json.example .mcp.json     # then put your URL and key in it
```

Then **restart Claude Code** — MCP servers attach at session start, so approving one cannot
help a session already running. Check it reads `Connected`, not `Pending approval`:

```bash
claude mcp list
```

### Then ask

> *Migrate my Kibana dashboards to ClickStack. Start with an inventory and show me what
> won't survive before you build anything.*

Claude will export the saved objects, inventory every panel, introspect your ClickStack
source to see which attribute keys and materialized columns actually exist, tell you which
panels cannot be migrated and why, translate the rest, create the dashboards, and then run
the structural audit over what it built.

### What it will stop and ask you about

Two things are deliberately **not** automated, because automating them would make them
worthless:

- **The losses.** A map panel has no target chart type; a categorical heatmap has no target
  at all. Which degraded form is acceptable — a country bar, a table, nothing — is a product
  decision, so the skill surfaces the list and waits rather than picking for you.
- **The value verification (the diff passes).** Diffing tiles against Elasticsearch needs the
  *Elasticsearch* side written by hand, one file per integration. That is the whole point: an
  expectation derived from the tile agrees with the tile whatever it says. `verify/expect_*.py`
  are five worked examples to copy — `expect_nginx.py` is the smallest and between them they
  cover every tile shape (`wide`, `long`, `scalar`, `terms`, `builder`, `grouped`).

Everything else — export, inventory, target introspection, translation, creation, the
structural audit, the row-cap check — Claude does unattended.

### The environment it reads

| variable | used by | notes |
|---|---|---|
| `KIBANA_URL`, `KIBANA_API_KEY` | `skill/scripts/export-dashboards.sh` | API key is the `encoded` field from `POST /_security/api_key`. **Required on Elastic Serverless**, and the only option where SSO has displaced basic auth. `KIBANA_USER`/`KIBANA_PASS` work self-managed |
| `KIBANA_SPACE` | same | or pass `--all-spaces`; most non-trivial deployments use spaces, and querying the default one alone reads as "this deployment has no dashboards" |
| `ES_URL`, `ES_USER`, `ES_PASSWORD` | `verify/` | the source numbers for value verification |
| `CLICKSTACK_MCP_URL`, `CLICKSTACK_API_KEY` | `skill/`, `verify/` | HyperDX. `CLICKSTACK_PERSONAL_API_KEY` is also accepted |
| `CLICKHOUSE_URL`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE` | `verify/` | the only route that works for ClickHouse Cloud or any remote cluster |
| `CLICKSTACK_CONTAINER`, `CLICKSTACK_COMPOSE_DIR` | `verify/` | *instead of* `CLICKHOUSE_URL`, when ClickHouse is a local container — the bundled all-in-one image needs a password on 8123 that its own client does not |

Pass credentials through the environment, never as arguments: an argument is visible in `ps`
and lands in shell history. **[`.env.example`](.env.example)** lists every variable with notes;
copy it to `.env` (gitignored) and `set -a; . ./.env; set +a`. Nothing reads `.env` itself —
the scripts read the environment, which is deliberate: a config file the tools read on their
own is a config file that eventually gets committed.

### Which versions this works against

| | version | status |
|---|---|---|
| **Elasticsearch** | **8.15.0** | tested — the reference stack pins it |
| **Kibana** | **8.15.0** | tested, including API-key auth and Spaces |
| **ClickStack / HyperDX** | **2.35.0** | tested; pinned in the reference stack. The image's health endpoint reports `2.35.0`; `skill/references/` calls the same release `2.35.0-beta` |
| Elasticsearch / Kibana 8.x generally | | **expected to work**, not exercised. The export path is the Saved Objects API, whose shapes are stable across 8.x |
| Kibana 7.x | | **partly handled, untested.** The panel parser already reads 7.x Lens layers (`datasourceStates.indexpattern` as well as 8.x `formBased`) and 7.x `input_control_vis` control panels, but no 7.x export has been run end to end |
| Kibana / Elasticsearch 9.x | | **unverified** |
| Elastic Cloud, Elastic Serverless | | auth and Spaces are handled; **not exercised end to end** |

Three version-specific things that will bite, all of them checkable in a minute:

- **Do not aggregate through `@elastic/mcp-server-elasticsearch` (≤ 0.3.1) against 8.x.** It
  sends `compatible-with=9` accept headers and 8.x rejects them with
  `media_type_header_exception`. Keep that MCP server for `get_mappings` if you like; use
  `curl` or `verify/` for aggregations. Nothing in this repo depends on it.
- **On Serverless, basic auth is gone** — an API key is the only option, and it is also the
  only one that survives SAML/OIDC SSO. `KIBANA_API_KEY` wants the `encoded` field from
  `POST /_security/api_key`, not `id` or `api_key`.
- **Query the default Space alone and a deployment looks empty.** Most non-trivial
  deployments use Spaces; pass `--all-spaces` to take an honest inventory.

> **The target version is the likelier problem, not the source.** ClickStack is beta and its
> tile schema moves between releases, which is exactly why
> `skill/scripts/introspect-clickstack.py` reads the schema off your *running* server instead
> of trusting notes from another version. Run it first; everything in
> `skill/references/clickstack-tiles.md` was measured that way against **2.35.0**, which the
> reference stack now pins for that reason — `latest` would quietly invalidate every number
> in `INTEGRATIONS.md` for anyone cloning later.

---

## Doing it by hand, or checking Claude's work

The same procedure, as commands. Worth reading even if you never run them — it is what the
skill is doing, and the verify step is where migrations are usually declared finished too
early.

### 1. Export and inventory the source panels

Panel definitions come from the **Kibana Saved Objects API**, never from an MCP server —
there is no Kibana MCP tool, and the Elasticsearch one has no access to saved objects.

```bash
# with no ids, LISTS what it can see (the ids are not guessable)
./skill/scripts/export-dashboards.sh --all-spaces
./skill/scripts/export-dashboards.sh --space obs-team <id>... > dashboards.ndjson

python3 skill/scripts/inventory-panels.py dashboards.ndjson            # markdown table
python3 skill/scripts/inventory-panels.py dashboards.ndjson --fields   # fields to map
python3 skill/scripts/inventory-panels.py dashboards.ndjson --json
```

`--fields` is the list you diff against the target — **not** the index mappings, which
declare far more fields than any dashboard uses.

Two parsing facts that cost a day if you rediscover them: panels are usually stored *by
value* inside `attributes.panelsJSON` (a JSON **string**), and for Lens panels the field you
want is `sourceField` inside `datasourceStates.formBased.layers.*.columns`. The script
handles those plus legacy `visState` aggs, TSVB, maps and saved searches.

### 2. Derive the collector config from the panels you are keeping

Only if ingestion is moving to the OTel collector — which is the usual path. Do it **before**
inventorying the target, because until the collector is configured there is nothing in the
target to inventory. Skipping it is how a migration finds out mid-flight that a panel has no
data.

```bash
python3 skill/scripts/inventory-panels.py dashboards.ndjson --fields > fields.txt
python3 skill/scripts/plan-collector.py fields.txt          # the plan
python3 skill/scripts/plan-collector.py fields.txt --yaml    # just the receivers: block
```

Every field the panels depend on gets a verdict — **default** (nothing to do), **optional**
(exists but off by default, the commonest case), **reshaped** (several fields collapse into one
metric plus a dimension, so the panel is a rewrite), **absent** (needs another receiver),
**derive** (compute it in the tile), or **dimension** (it becomes an attribute).

The trap worth knowing before you plan: **every hostmetrics `*.utilization` metric is optional
and the default is the absolute counter.** Elastic hands you percentages; OTel makes
percentages opt-in.

Background and caveats:
[`skill/references/integration-to-receiver.md`](skill/references/integration-to-receiver.md).
Five worked configs written against this plan:
`reference-stack/ingest/clickstack/otel-collector-*.yaml`.

### 3. Inventory the target before writing a single query

```bash
python3 skill/scripts/introspect-clickstack.py --sources
```

With a `Map` schema this is the only way to learn which keys exist, and it surfaces
materialized columns (`geo_*`, `ua_*`) as real top-level columns — which decides whether a
builder tile can reach them or you need SQL.

### 4. Settle the losses with stakeholders *now*

See [What does not survive](#what-does-not-survive). Saying it at the start is a design
decision; saying it when you reach the panel is an excuse.

### 5. Translate, then create

`skill/references/field-mapping.md` for ECS → `LogAttributes` patterns and the expressions
for fields Elastic derived at index time; `skill/references/clickstack-tiles.md` for the tile
schema and the trap that silently doubles every number (**builder tiles have no tile-level
`where`** — the filter goes on each `select` item).

### 6. Verify — a tile that renders is not a tile that is correct

This is the part this repo exists for. Four passes, in increasing cost:

```bash
# 6a. structural audit: does each tile still match the panel it came from?
python3 skill/scripts/audit-tiles.py dashboards.ndjson migrated.json --field-map fields.json

# 6b. do the dropdowns resolve to the same option SET as the source's controls?
#     (edit the SPEC at the top of the file to name your dashboards and their controls)
python3 verify/verify-controls.py
python3 verify/verify-controls.py --mutate   # prove each check can actually fail

# 6c. bucket-for-bucket value diff against Elasticsearch
python3 verify/verify-tiles-vs-elastic.py

# 6d. a human opens the two dashboards side by side
```

**The two diff passes need one file each pointed at your dashboards.** That is the only hand-written
part, and it is deliberate: an expectation derived automatically from the tile would agree
with the tile whatever it says. For the value diff copy `verify/expect_nginx.py` — it is the smallest of
the four and covers every tile shape between them (`wide`, `long`, `scalar`, `terms`,
`builder`, `grouped`); you write the Elasticsearch side, `verify/tilediff.py` does the rest.
For the control diff, edit `SPEC` and `EXTRA` at the top of `verify/verify-controls.py`. Run either with
`--list` / `--mutate` to see the shape and to confirm the checks can fail.

### 7. Record the residue

Every migration has one. Write it down **with the reason, classified** — rendering-layer gap,
enrichment absent, third-party dataset differs, source precision differs. Classifying it is
what lets the next reader tell a limitation from a bug.

Full procedure, with every trap and its reason: **[`skill/SKILL.md`](skill/SKILL.md)**.

---

## What the verification catches, and why each check exists

Every check below was added because something got through the previous ones. On the reference
migration a person comparing charts by eye found **thirteen** defects that a fully green suite
had passed.

| check | catches | added after |
|---|---|---|
| `query_tiles` | a tile that errors or returns nothing | — |
| structural audit (`audit-tiles.py`) | dropped metric, chart-type drift, placeholder SQL, `seriesLimit` on a time series, normalisation mismatch (`.norm.` on one side), dropped dashboard control, missing `sourceMetricType` | 7 mutation-tested checks, each from a real failure |
| dropdown option sets (`verify-controls.py`) | a control that resolves to the wrong values — including when the option **count** matches | a dropdown gained the target's own container id and still counted 8 against Kibana's 8 |
| bucket-for-bucket diff (`verify-tiles-vs-elastic.py`) | a series that is the right shape and wrong in every bucket | 8 gauge series off by ~1% on two already-signed-off dashboards |
| row caps (same harness) | a tile truncated by **its own `LIMIT`** — every bucket it returns is correct, it just stops early | a 22-series chart silently lost its last 48 buckets |

Three findings worth knowing before you build tiles on a metric source, because they are
platform behaviour and not bugs you can fix in a query:

- **A gauge is collapsed to one sample per bucket (the last) before `aggFn` runs.** So a
  Kibana panel that averages a gauge's samples inside a bucket is **not** a builder shape —
  `max`, `min`, `avg`, `sum` and `last_value` all returned the same number where the truth
  was `max=14, avg=10.8`. Use SQL.
- **`count_distinct` on a metric source under-counts.** It returned 2 where the data plainly
  held 3 hosts × 10 points × 10 distinct values. Use SQL.
- Net: **on a metric source, only `last_value()` and counter `differences()` are faithful
  builder shapes.** Everything else needs a `sql` tile.

And two measurement rules that keep a verification suite honest:

- **Diff the whole distribution, not the top-N you wrote down.** Matching three values you
  happened to record is not matching the panel. On the reference migration a full `terms`
  diff is exactly what exposed two wrong answers that already looked finished.
- **Derive every tolerance from a mapping or a query — never from the size of the failure.**
  `scaled_float` and `float` fields are quantised in doc_values; one platform may store whole
  seconds where the other keeps milliseconds. Read the bound off `_mapping`. And never write
  `max(tol, tol × |expected|)`: that turns a tolerance of 1 into a 100 % relative tolerance.

---

## What does not survive

Declare these before translating anything.

| class | example | recoverable? |
|---|---|---|
| **rendering-layer gap** | a **map** panel. ClickStack's chart vocabulary is `line`, `stacked_bar`, `table`, `number`, `pie`, `bar`, `heatmap`, `search`, `event_patterns`, `markdown`, `sql` — there is nowhere to put a lat/lon pair, so a geo map degrades to a bar or table on country code. This holds even when the geo *data* migrated perfectly | no |
| **rendering-layer gap** | a **categorical heatmap**. ClickStack's `heatmap` is value-distribution only: one series, no `groupBy`. A host × time × value heatmap has no target and degrades to a multi-series line | no |
| **enrichment absent** | `user_agent.name`, `source.geo.*` — computed by an ingest pipeline, so not in the data | yes, via a ClickHouse dictionary — see `skill/references/enrichment.md` |
| **third-party dataset differs** | GeoLite2 against DB-IP: same query, same data, different country. Differs by ~43 % on some countries | no — migrate the *database* to fix |
| **source precision differs** | second-resolution timestamps against millisecond; `scaled_float(1000)` destroying values below 5e-4 | no |
| **collection gap** | the signal is not in the collector's *default* set — an optional metric left off (`system.cpu.utilization`), or one needing a receiver nobody configured (`sqlqueryreceiver` for `pg_stat_statements`) | yes, by changing collector configuration — not the tile |

The third and fourth rows are the ones people misread as bugs.

---

## Status: what has actually been migrated and verified

Measured against both running stacks on 2026-09-17, not asserted. **5 integrations, 17
dashboards, 126 data tiles.**

| integration | dashboards | tiles | renders | verifier | value-diff vs Elastic | status |
|---|---:|---:|---:|---|---:|---|
| **nginx** (logs) | 2 | 10 | 10/10 | `verify-nginx.sh` 42/42 | 12 series | complete |
| **nginx** (metrics) | 1 | 8 | 8/8 | `verify-nginx.sh` 42/42 | 11 series | complete |
| **apache** (logs) | 1 | 7 | 7/7 | `verify-apache.sh` 46/46 | 11 series | complete |
| **apache** (metrics) | 1 | 11 | 11/11 | `verify-apache.sh` 46/46 | 46 series | complete |
| **postgresql** (logs) | 2 | 6 | 6/6 | `verify-postgres.sh` 21/21 | 7 series | complete |
| **postgresql** (metrics) | 1 | 9 | 9/9 | `verify-postgres.sh` 21/21 | 31 series | complete |
| **mysql** (logs) | 1 | 6 | 6/6 | `verify-mysql.sh` 36/36 | — ² | complete |
| **mysql** (metrics) | 2 | 20 | 20/20 | `verify-mysql.sh` 36/36 | 42 series | complete |
| **system** (logs, Linux) | 4 | 17 | 17/17 | `verify-system.sh` 44/44 | 11 series | complete ¹ |
| **system** (metrics, Linux) | 2 | 33 | 33/33 | `verify-system.sh` 44/44 | 32 series | complete ¹ |
| system (Windows Security) | 5 | — | — | — | — | **read only** ⁴ |
| kubernetes | 15 | — | — | — | — | **read only** ⁵ |
| synthetics | 0 | — | — | — | — | ships no dashboards |
| **total** | **17 / 37 read** | **127** | **127/127** | **229 checks, 0 failures** | **203 series** | |

¹ Has a declared loss: a map panel (apache, system logs), two categorical heatmaps (system
metrics). See
[`reference-stack/INTEGRATIONS.md`](reference-stack/INTEGRATIONS.md).
² Six `search`/`terms` tiles — no time series to diff; covered by `verify-mysql.sh`.
³ Ported 2026-09-17 from the harnesses the system migration was originally verified with,
which had only ever existed outside the repo. Porting widened the coverage from 33
comparisons to 43 series: the throwaway version checked only one side of each bidirectional
counter and skipped one of the two degraded heatmaps. All five mutation tests fire, including
the historical wrong-field bug (`process.cpu.pct` against the core-normalised field, which
once passed a green check by deriving its expectation from the tile).
⁴ Needs a Windows event-log corpus (4624/4625/4720/4732…) this dataset has no analogue for.
The translation looks cheap; it is purely a data problem.
⁵ 166 fields, and 43 panels use ad-hoc data views with Painless `runtimeFieldMap` —
expressions evaluated per query that exist in no mapping on either side. Several data views
are also cross-cluster, so the target may not hold the data at all.

Across five integrations the pattern did not change: **the work is the data, not the
translation.**

---

## The reference migration (optional, but it is how all of the above was found)

[`reference-stack/`](reference-stack/) is a self-contained pair of stacks — Elasticsearch
8.15.0 + Kibana with real Elastic integration packages installed, and ClickStack 2.35.0, all
three pinned — over a
**deterministic synthetic corpus**: nginx, apache, postgresql, mysql and system logs and
metrics, ~1.5 M events with stated invariants.

It exists so that "the migration is correct" is a measurable claim rather than an opinion: the
same events are in both stacks, so every tile has a source number to be diffed against.

```bash
cd reference-stack
python3 generator/generate.py && python3 generator/validate.py
shasum -a 256 -c checksums.txt          # 24 files, byte-identical every run
```

The corpus is **not committed** — 704 MB, and one file is 434 MB against GitHub's 100 MB
limit. The generators are seeded, so the command above reproduces it exactly and
`checksums.txt` proves it. Then follow
[`reference-stack/RUNBOOK.md`](reference-stack/RUNBOOK.md) to bring both stacks up and load
them.

| document | read it for |
|---|---|
| [`reference-stack/RUNBOOK.md`](reference-stack/RUNBOOK.md) | bringing both stacks up, loading, re-anchoring the corpus to now |
| [`reference-stack/INTEGRATIONS.md`](reference-stack/INTEGRATIONS.md) | what was migrated per integration, every finding, and where the source platform is wrong |
| [`reference-stack/MIGRATION.md`](reference-stack/MIGRATION.md) | the original nginx migration, walked through end to end |
| [`reference-stack/EXERCISES.md`](reference-stack/EXERCISES.md) | exercises against the corpus |

---

## Layout

```
skill/            the migration procedure and its scripts — generic, no dataset assumptions
  SKILL.md          the seven-step procedure
  references/       integration→receiver coverage, field mapping, tile schema, Kibana
                    export shapes, enrichment, verification
  scripts/          export-dashboards.sh, inventory-panels.py, introspect-clickstack.py,
                    audit-tiles.py, plan-collector.py (+ receiver-map.json)
  scripts/tests/    run-all.sh — three suites, no stack required
verify/           the verification harness — environment-driven, points anywhere
  conf.py           every endpoint and credential, from the environment
  tilediff.py       the bucket-for-bucket machinery
  expect_*.py       per-integration Elasticsearch expectations (copy one as a template)
  verify-tiles-vs-elastic.py, verify-controls.py
reference-stack/  the two stacks, the generators and the corpus documentation
```

## A note on credentials

Everything checked in is a **throwaway local value** for the reference stack —
`elastic:changeme`, `TrainingP4ss!`, `localhost` endpoints. They are defaults for a disposable
Docker Compose environment and are meant to be overridden by the environment variables above.
`.mcp.json` is **not** committed because it holds a HyperDX personal API key minted by the
local container; `.mcp.json.example` is the template, and
`reference-stack/stack/clickstack/write-mcp-config.sh` regenerates the real one.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
