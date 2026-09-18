# Verifying a migrated dashboard

> **A tile that renders is not a tile that is correct.**

Every wrong tile in the reference migration rendered perfectly. Verification is the only part
of this procedure that distinguishes a finished migration from one that looks finished.

## What to compare

Pick invariants that are **window-independent** — totals, distinct counts, whole
distributions:

| question | source | target |
|---|---|---|
| total events in the dataset | `_count` on the index | `count()` with the scoping predicate |
| a status/level breakdown | `terms` aggregation | `GROUP BY` the mapped expression |
| distinct entities | `cardinality` agg | `uniqExact()` (not `uniq()`, when comparing exactly) |
| a summed measure | `sum` agg | `sum(toUInt64(...))` |

`uniq()` is approximate. If a source `cardinality` agg and a target `uniq()` disagree by a
fraction of a percent, you have compared two different estimators, not found a data problem.

## Rule 1: diff the whole distribution, not the top-N

Matching the three values you happened to write down is not the same as matching the panel.
Pull the full aggregation from the source and diff bucket-for-bucket:

```bash
curl -s -u "$U:$P" "$ES/$INDEX/_search" -H 'Content-Type: application/json' -d '
{"size":0,"aggs":{"b":{"terms":{"field":"user_agent.name","size":50}}}}'
```

On the reference migration the first attempt matched the three recorded values and looked
complete. Diffing the full distribution exposed two genuine errors — a bucket that should
have been split in two, and an `Other` bucket that was a specific enumerated set rather than
a remainder. Both were invisible at the top of the distribution.

It costs one query per panel and it is the only way to know.

**Elasticsearch 8.x note:** `@elastic/mcp-server-elasticsearch` (≤0.3.1) sends
`compatible-with=9` accept headers, which 8.x rejects with `media_type_header_exception`. It
cannot run these aggregations. Use `curl`.

## Rule 2: never compare a wall-clock window

The two platforms are almost certainly not on the same clock — each loader computes its own
time shift when it runs, so the datasets sit however far apart the two loads were. On the
reference run that was 40 minutes, which made the same half-hour bucket read 15,278 on one
platform and 14,310 on the other. Nothing was missing.

Compare totals and distributions aligned to each dataset's own start, and report clock skew as
an advisory, not a failure.

## Rule 3: know which residual is irreducible

If the two platforms parse **different source files**, sub-second precision may differ. A
second-precision log cannot tell you which side of a bucket edge a `12:29:59.400` event
belongs to. Measured with the clocks aligned: **0–3 rows per 30-minute bucket, ≤0.02%**.

No query can correct this. Do not chase it, and do not "fix" it by re-pointing ingestion at a
different file unless that is independently the right call. State the bound and move on.

## Mechanize it

`clickstack_query_tiles` runs every tile of a dashboard in one call — use it rather than
querying tiles one at a time, so the check is cheap enough to repeat after every patch.

Then write the checks to a script so they survive the session. A good migration check suite
asserts, in order:

1. **Existence** — every expected dashboard and saved search exists, with the expected tile count.
2. **Scoping** — every queryable tile carries the dataset predicate (see the snippet in
   `clickstack-tiles.md`; remember `where` persists as `aggCondition`). Skip `markdown` tiles.
3. **Invariants** — the totals and distributions above still match the source numbers.
4. **Stored expressions** — execute the expressions **read back out of the saved tiles**, not
   the ones you meant to save, and diff those against the source. This is the check that
   catches a migration that looks right and is not.

   **This matters most for `sql` tiles, and a hand-written "equivalent" defeats it.** On a
   real migration a Drops Rate tile passed a hand-written check of the same *intent* while
   the stored query computed something else, and `query_tiles` reported it `ok` with 49 rows
   throughout — the query was valid, the series was wrong. It was the user looking at the
   rendered chart who noticed: four spikes in Kibana, one in ClickStack. Read the
   `sqlTemplate` out of the dashboard, expand the macros, run *that*.

5. **Sentinels vs. structure** — a window function's "no previous row" case must be
   detected **structurally**, with `row_number() OVER (ORDER BY ts) > 1`, never by testing the
   lagged *value*. `lagInFrame(x)` returns the column's default (0 for a number) out of frame,
   so `WHERE prev != 0` reads as "has a predecessor" only while the metric never legitimately
   holds 0.

   That assumption held for one dataset and broke on the next. Counters starting above four
   million made `prev = 0` a safe proxy; postgres's `conflicts` and `deadlocks` sit at zero
   all day, so the guard dropped exactly the bucket where the counter first moved — which *is*
   the spike. 48 buckets became 31 and every spike vanished, while the query stayed valid and
   `query_tiles` stayed green.

6. **Read each series' own aggregate from the formula.** A Lens panel can mix them: the
   reference Conflict/Deadlock panel uses `differences(average(deadlocks))` for one series and
   `differences(max(conflicts))` for the other. Applying one aggregate to both looks right and
   silently flattens the series that needed the other.

7. **Per-dimension arithmetic** — if a tile subtracts or divides two aggregates, check it
   does so **within** each series, not across them. `max(a) - max(b)` over several hosts is
   not `max(a - b)`: when one host dominates both inputs, the result silently collapses to
   that host. That was the Drops Rate bug precisely — `max(accepted) - max(handled)` across
   three nodes returned only the highest-numbered node's drops. Group by the dimension
   first, then aggregate.
8. **Breakdown dimensions** — a Kibana `terms` breakdown is a **dimension, not decoration**.
   If the source panel splits by `host.hostname` and the tile aggregates across hosts, the
   tile is not wrong in its totals — it has silently lost the per-node divergence the panel
   exists to show. The legend is the tell: the user sees host names on one platform and
   metric names on the other.

   Check it mechanically rather than per panel. For every source panel, collect the `terms`
   fields that are dimensions rather than the panel's own metric split, and assert the
   migrated tile's `groupBy`/`sqlTemplate` mentions each one:

   ```python
   DIMS = ("host.hostname", "host.name", "agent.id", "service.address")
   # panel has terms(DIM)  ->  tile's groupBy or sqlTemplate must reference it
   ```

   On the reference estate that flagged 3 panels out of 332, all in one metrics dashboard,
   and one of the three had dropped it.

9. **Advisories** — clock skew, and any known third-party-dataset disagreement (geo),
   reported without failing the run.

Exit non-zero and name the failing object. A check suite that prints a wall of green without
naming what it compared is not evidence.

## Step 6a: audit each tile against its panel, structurally

Before opening a browser, run `scripts/audit-tiles.py`. It compares every migrated tile to the
panel it came from and reports differences in *structure* — which is most of what the visual
pass actually catches, and it finds them in a second rather than ten minutes.

```bash
# installed as a personal skill (see the repo README for how to install it):
SKILL=~/.claude/skills/kibana-to-clickstack
# ...or run it straight out of a clone of this repo:
SKILL=./skill
python3 "$SKILL"/scripts/audit-tiles.py source-dashboards.ndjson migrated.json \
    --field-map field-map.json          # the step-5 mapping; see below
```

`migrated.json` is whatever `clickstack_get_dashboard` returns. Exit status is 1 if anything
is flagged, so it can gate a migration.

Five checks, each taken from a real failure on the reference migrations, and each
**mutation-tested** — the bug is re-introduced into a known-good dashboard and the check must
fire, with an unmodified control that must stay silent:

| check | the failure it came from |
|---|---|
| dropped metric | `Memory usage vs total` displays used bytes **and** total; the tile showed one |
| placeholder | `any(0) AS "_"` shipped where a second column belonged |
| `seriesLimit` on line/stacked_bar | a chart rendered blank; no value check can see it |
| chart type drift | four tiles built as `line` where the panel was `seriesType: bar_stacked` |
| field mapping | the tile read the **core-normalised** CPU field where the panel reads the raw one — wrong by 4x-16x, every check green |

### The field map is what makes the last one possible

A metric tile references an **OTel metric name**, not the source field name — renaming is the
point of the migration. So no string heuristic can bridge `process.cpu.pct` to
`system.process.cpu.utilization`, and the audit will not guess. Give it the mapping you built
in step 5:

```json
{"process.cpu.pct": "system.process.cpu.pct",
 "system.cpu.user.norm.pct": "system.cpu.utilization"}
```

With it, every panel field must map to something its tile actually references. Without it,
unmatched fields are listed as a **review** — printed with the identifiers the tile does
reference, so a human confirms the pairing — and are *not* counted as findings. Writing the
map down is the point: the pair `X.pct` / `X.norm.pct` differs only by core count, and
choosing wrong looks identical to choosing right.

### Pairing a tile to its panel is where this check goes wrong

Everything the audit reports is relative to the panel it thinks a tile came from, so a bad
pairing does not weaken the check — it **manufactures findings**. Three rules, each learned
from a false positive on the reference estate:

1. **Match within the tile's own source dashboard.** Panel titles repeat across integrations:
   `Connections` exists on both a MySQL and an Apache dashboard, `CPU Usage` on two system
   dashboards. Searching the whole export paired an Apache tile to the MySQL panel and then
   flagged a chart-type difference that was purely the mispairing. Scope the search, and use
   the field-overlap fallback rather than widening it.
2. **Assign one panel per tile.** Two tiles can reduce to the same title — a migrated
   `SSH login attempts` chart and an added `SSH login attempts (search)` raw-rows view. Matched
   independently, both claimed the panel, and the search tile was flagged "panel is a stacked
   bar but tile is a search". Assign greedily in two passes, exact titles first, so the result
   does not depend on tile order.
3. **Tolerate annotations on either side, but only when unambiguous.** A migrated tile gains
   `(SQL: why it is not a builder tile)`; a stock Kibana panel carries `[Metrics Apache]` or
   `(converted)`. Stripping a trailing parenthetical or bracket recovers those pairings — but
   `Network traffic (bytes)` and `Network traffic (packets)` are *two different panels*, and
   stripping both sides paired the bytes tile to the packets panel. Strip only as a fallback
   after an exact match fails, and accept it only when exactly one panel matches. Ambiguity
   means no pairing, which is the honest answer.

Together these took the reference estate from 56 tiles paired to 96, and removed three
findings that were all mispairings rather than defects. **Before trusting a new pairing rule,
list every non-exact pairing it makes and read them** — the count going up is not evidence the
pairings are right.

### What it cannot do

- **Untitled panels.** Many stock panels have a UUID for a title, so title matching leaves
  them unmatched. A field-overlap fallback claims a pairing only with two shared fields and a
  clear margin over the runner-up; below that the tile is listed as un-audited rather than
  guessed at. An inferred pairing never drives a comparison — a guessed panel manufacturing a
  defect is worse than no check, and it did exactly that on the first draft, three times out
  of three.
- **Wrong values from the right field.** That is the bucket-for-bucket diff's job, above.
- **A field that is constant**, or a source panel that is itself ambiguous. Those are data
  questions, further down this file.

## The visual pass, and why it is not optional

Everything above is the numeric pass. It is necessary and it is **not sufficient**: on the
reference migrations **eight** tile bugs passed a fully green check suite and were found by a
person comparing charts. `query_tiles` returned `status: ok` and a plausible `rowCount` for
every one, and the aggregate totals matched.

The first five were *valid query, wrong series* — the numbers were wrong in a way totals hid.

| bug | numeric check said | the chart said |
|---|---|---|
| `max(a) - max(b)` across hosts instead of `max(a - b)` per host | ok, 49 rows | 1 spike where the source had 4 |
| `increase` (Σ per-series) where the source used `differences(of max())` (Δ of max) | ok, totals consistent | a 1.8x single-node spike flattened into noise |
| aggregated across a `terms` breakdown the source splits by | ok, averages matched Elastic to 4 d.p. | 5 series instead of 10; legend showed metric names where the source showed hosts |
| used a value sentinel (`prev != 0`) to mean "no previous bucket" | ok, 31 rows | one spike in the source, none on the target |
| used `max()` where the source formula used `average()` on one of two series | ok, plausible series | the series was flat where the source had spikes |

Note the third: the numbers were *right to four decimal places*. What was wrong was the
**shape of the result set**, and no assertion about values can see that.

The other three were *right values, wrong shape*, and these survived **bucket-for-bucket
diffing as well** — every value in every bucket was correct:

| bug | every numeric check said | the chart said |
|---|---|---|
| a saved search translated with `GROUP BY` over columns that never change | ok, 1 row, values correct | 1 row where the source listed 2,760 |
| four tiles built as `line` where the source panels were `seriesType: bar_stacked` | ok, 42/42 series matched per bucket | bars on one side, lines on the other |
| a panel whose source field was a constant 0 | ok, 276/276 buckets identical | both charts empty — the two platforms agreeing perfectly on nothing |

So the passes are not redundant and they are not ordered by strength: totals catch gross
errors, per-bucket diffing catches a series that is the right shape and quietly wrong, and
only a human comparing charts catches a tile whose every number is right and whose *form* is
not the source's.

### The protocol

Open the source dashboard and the migrated one side by side, same absolute time range, and
walk the tiles in order. Per tile, in this sequence:

1. **Series count and legend.** Same count? Same names? This is the highest-yield check
   because a dropped dimension is invisible in every aggregate. If one legend lists hosts and
   the other lists metric names, a `terms` breakdown was aggregated away.
2. **Shape.** Spikes and dips in the same buckets, with the same relative prominence. "Present
   but flatter" is the signature of summing series the source kept separate.
3. **Density.** Every series continuous across the window? Fragmentary series mean truncation
   (see the multi-dimension `groupBy` limit in `clickstack-tiles.md`), not absent data.
4. **Magnitude.** Same order of magnitude per series. An exact 2x or 3x on a multi-host
   dataset is almost always a fleet total standing in for one series.

Record the comparison, not just its outcome: "11 tiles compared against the source at
Last 24 h; legends, spike positions and series continuity match; Drops Rate differs in the
first bucket only, expected" is evidence. "Looks good" is not.

### Mechanising it

The visual pass should be the backstop, not the only defence. Each failure above has a cheap
automated counterpart:

- **Run the tile's stored `sqlTemplate`**, expand the macros, execute *that*. A hand-written
  "equivalent" re-encodes the same misunderstanding that produced the tile.
- **Assert the result-set shape, not just values:** `count()`, `uniqExact(series)` **and**
  `uniqExact(ts)`. The series count alone looks healthy while the buckets are gone.
- **Scan for dropped dimensions.** For every source panel, collect `terms()` fields that are
  breakdown dimensions and assert the tile's `groupBy`/`sqlTemplate` mentions each:

  ```python
  DIMS = ("host.hostname", "host.name", "agent.id", "service.address")
  # panel has terms(DIM)  ->  tile must reference DIM
  ```

  Cheap to run across a whole estate: on 332 reference panels it flagged 3, and one of the
  three had indeed dropped it.

Even with all three in place, view one dashboard end to end before declaring the migration
done. The class of bug these catch is the class we already know about.

## The pass that beats both: diff every series bucket for bucket

The visual pass is not sufficient either, and there is a measured case. Migrating mysql, all
42 tile series were diffed against Elasticsearch **per bucket** over one absolute window. The
same harness then run against two *earlier* migrations — both signed off, both green, both
eyeballed — found **8 wrong series**:

```
nginx  Active connections      141/144 buckets disagreed with Elastic
nginx  Waiting                 141/144
nginx  Writing                  74/144
nginx  Reading                  23/144
apache Total connections        92/144
apache Average server load     144/144 on each of load.1 / load.5 / load.15
```

Cause: a gauge is collapsed to one sample per bucket before `aggFn` runs, so a builder tile
cannot compute Kibana's `average()` over a bucket's samples (see `clickstack-tiles.md`). The
numbers were wrong by **3.63 against 3.67** — indistinguishable on a chart, invisible to a
whole-window average, and both verifiers were asserting whole-window averages.

So the ordering is: totals catch gross errors, the visual pass catches wrong *shapes*, and only
a per-bucket diff catches a series that is the right shape and quietly wrong. It costs one
query per series per platform:

```python
# {bucket -> value} from each side, then compare the KEY SETS as well as the values
e = {b["key_as_string"][:16]: b["v"]["value"] for b in es_date_histogram(...)}
c = {row["__hdx_time_bucket"][:16]: row["v"] for row in clickstack_timeseries(...)}
assert set(e) == set(c)                     # same buckets
assert all(abs(e[k] - c[k]) < 1e-6 for k in e)   # same values
```

Three things that make this harness work rather than produce noise:

- **Normalise the bucket keys first.** Elastic's `key_as_string` renders `15:00`, ClickHouse's
  `DateTime` renders `15:00:00`. Joining the raw strings gives an empty intersection, which
  reads as "everything is broken" rather than "the harness is". Truncate both to minutes.
- **Use one absolute window, defined once**, for every comparison on both platforms.
- **Trim the two edge buckets on `increase` tiles** — the builder produces a leading value
  Kibana nulls, and a trailing bucket past `endTime`. Both are platform behaviour.

> **It has since been extended to 203 tile series across five integrations**, and each
> extension found something the previous suite had passed: a `count_distinct` under-counting on
> a metric source, and a tile silently truncated by its own row cap. Extending the diff to a
> new integration is cheap — the machinery is shared and only the source-side expectations are
> per-integration — and it has never yet been extended without finding something.

## Count records, not lines, when the source format is multi-line

A row-count assertion is the cheapest check there is, and it silently means the wrong thing if
one logical record spans several lines. MySQL's slow log is five lines per query:

```
# Time: ...          <- the record separator
# User@Host: ...
# Query_time: ...
SET timestamp=...;
SELECT ...;
```

A line-oriented reader emits five documents per query, four carrying no query at all, **and
nothing errors** — each of those lines is a valid log line. The symptom is a row count exactly
5x too high (12,565 rows where the source has 2,513 records), which looks like a duplicated
load rather than a parsing error.

So assert two things, not one:

```sql
-- the record count, against the SOURCE's record count (not `wc -l`)
SELECT count() FROM otel_logs WHERE ...;
-- and that no separator line leaked into a parsed field
SELECT countIf(position(LogAttributes['query'], '# Time:') > 0
             OR position(LogAttributes['query'], 'SET timestamp') > 0) FROM otel_logs WHERE ...;
```

The second is what distinguishes a working reader from a half-working one. On the ingest side
this is `multiline.line_start_pattern` for a filelog receiver; whatever produces the rows, the
verification is the same.

## Check the panel has something to show

Both passes above compare two platforms. Neither asks whether the panel is *worth* rendering,
and a constant field makes the two agree perfectly on nothing at all.

Measured: the mysql `SQL thread delay` panel charts `mysql.replica_status.thread.sql.delay.sec`,
which was **0 in all 2,880 documents**. Both stacks returned 276/276 identical buckets, every
check passed, and both charts were empty — the migration was correct and the panel was useless.
`SQL_Delay` is only non-zero on a deliberately delayed replica, so zero was realistic; it was
still a hole.

So for each migrated tile, ask the cheap question:

```sql
SELECT MetricName, uniqExact(Value) AS distinct_values, min(Value), max(Value)
FROM otel_metrics_gauge GROUP BY MetricName HAVING distinct_values = 1
```

`distinct_values = 1` means the tile can only ever draw a flat line. That is either
(a) genuinely how the source system behaves — say so in the residue list, so nobody re-migrates
it looking for the bug; or (b), if you control the corpus, a gap to fill. A step function makes
a far better cross-platform comparison than a flat line, because a mis-scoped tile shows up
instantly against it.

## Derive the expectation from the PANEL's field, never from the tile's

The most embarrassing hole in this whole procedure, and it verified green for a full day.

`Top processes by CPU usage` aggregates **`process.cpu.pct`**. The migrated tile read
`system.process.cpu.total.norm.pct`. Those are different fields: metricbeat normalises the
second by core count and not the first, so the tile was wrong by a factor of **4x, 8x or 16x**
depending on which hosts ran the process.

Every check passed, because the harness built its Elasticsearch expectation from
*the field the tile read*. Comparing a tile to itself always agrees:

```python
# WRONG -- this is a tautology dressed as a test
tile_field = read_metric_from(tile)
expected   = es_avg(tile_field)          # asks Elastic about the tile's own choice
assert expected == tile_value            # passes for ANY field the tile picked
```

```python
# RIGHT -- the field comes from the source panel's aggregation list
panel_field = "process.cpu.pct"          # from inventory-panels.py --fields, per panel
expected    = es_avg(panel_field)
assert expected == tile_value
```

`inventory-panels.py` already prints each panel's aggregations with their fields; that list is
the input to the expectation, exactly as it is the input to the field mapping. A cheap
structural companion is to assert the tile's stored SQL *names* the panel's field:

```bash
q "sqltemplate:<dash>:<tile>" | grep -q "process.cpu.pct'"   # and NOT the normalised one
```

> Two fields whose names differ only by `norm` are the easiest pair in observability to swap,
> and nothing about the result looks wrong: same shape, same ranking, same units, plausible
> magnitude. Only the source panel tells you which one it meant.

## `last_value` is ambiguous when it collapses a dimension

`last_value(f)` grouped by *one* field, over data that carries *more*, is not a well-defined
number. Kibana implements it as `top_metrics` sorted by the time field only — so when several
documents share the newest timestamp it returns one of them **arbitrarily**.

Measured: `Top processes by CPU usage` groups by `process.name`, but `nginx` runs on three
hosts. At the newest scrape there are three values (0.119 / 0.140 / 0.152). Kibana returned
web-edge-02's; ClickHouse's `argMax` returned web-edge-03's. Neither is reproducible, and
re-running either can change the answer.

So do not try to match it. Pick a deterministic reading and say so on the dashboard — the
value **at** the newest timestamp, aggregated over the dimension the panel collapses:

```sql
avgIf(Value, TimeUnix = (SELECT max(TimeUnix) FROM ... WHERE <same filters>)) AS "Last"
```

The same applies to a `last_value` **number** tile with no breakdown at all: it returns one
arbitrary series out of however many exist. Check the cardinality of what the panel groups by
against the cardinality of the data before assuming the source has a single answer.

## Two source-side precisions that look like migration bugs

Both were measured on the `system` integration and both produce a *small* disagreement that
survives every structural check, so they are worth knowing before you chase one.

**`scaled_float` silently destroys small values.** Elastic stores a `scaled_float` as an
integer multiple of `1/scaling_factor`. `system.process.cpu.total.norm.pct` has
`scaling_factor: 1000`, so it keeps three decimals: a process averaging `9.3e-05` is stored as
**exactly 0.0**, while ClickHouse keeps the Float64. A "top processes by CPU" table then shows
`0.000` on one side and a real number on the other, for the quietest rows only.

```bash
# find the factor before comparing anything
curl -s -u user:pass -XPOST "$ES/_index_template/_simulate_index/metrics-<ds>-default" \
  | python3 -c 'import json,sys; ...  # look for "scaling_factor"'
```

Compare such fields with an **absolute** tolerance of half a quantum, not a relative one. A
relative tolerance fails hardest exactly where the quantisation bites.

**`reducedTimeRange` can ask for a window the data cannot fill.** A Lens formula like
`(max(f, reducedTimeRange='30s') - min(f, reducedTimeRange='30s')) / 30` computes over the
last 30 seconds of the range. If the source collects every 60s, that window holds **one**
sample and the panel reads **0 — in Kibana too**. Check the collection interval against any
`reducedTimeRange` before deciding the target tile is wrong; the honest translation is the
newest available interval, with the deviation stated.

## Derive every tolerance; never fit one to the failure

A bucket-for-bucket diff will disagree for reasons that are not migration errors, and the
difference between a real check and a rubber stamp is where the tolerance comes from. Read it
off the source's `_mapping` or compute it from the data — never widen it until the test
passes.

Three source-side causes, all worth checking before concluding a tile is wrong:

| cause | how to confirm it | the derived bound |
|---|---|---|
| **Timestamp resolution** — the source stores whole seconds where the target keeps milliseconds (or vice versa) | aggregate the sub-second component; if every document reports 0, the source truncated | events within one second of a bucket edge move between buckets. Bound = the most events sharing any single second, **queried per series** |
| **`scaled_float`** | `scaling_factor` in the mapping | samples sit on a `1/scaling_factor` grid, so half a quantum bounds the disagreement between averaging quantised and exact samples. Values below half a quantum are indistinguishable from zero |
| **`float` / `half_float`** | the field type in `_field_caps` | `_source` keeps full precision but **aggregations read doc_values**, which are single (or half) precision. A cumulative counter at magnitude *m* sits on a `2^(exponent(m)-24)` grid, and differencing two such values doubles the error |

For the timestamp case, exact equality is the wrong assertion entirely. Assert the SHAPE of
the disagreement instead:

- no bucket differs by more than one second's worth of events;
- the signed deltas **conserve** — they sum to ~0, meaning events moved between buckets
  rather than appearing or vanishing;
- absent means **zero**, not "skip": a sparse series whose only event in a bucket crosses the
  boundary leaves that bucket present on one platform and missing on the other, which is the
  same effect showing up as bucket presence rather than as a value.

Those three still fail on every real bug — a double-counted stream shifts every bucket by
~100%, a wrong field changes the magnitude, a dropped group vanishes, a scale error breaks
conservation — and tolerate only re-bucketing.

> **Write tolerances as ABSOLUTE.** `max(tol, tol × |expected|)` looks defensive and turns a
> tolerance of 1 into a 100% relative tolerance: a check that passes anything.

## Verify the verifier: a check that does not run looks exactly like one that passed

A suite reports what it *executed*, not what you *intended*. Those differ silently, and the
failure is always in the same direction — green.

The concrete shapes this takes, each seen on the reference migration:

- **A missing helper.** Refactoring two check scripts into one dropped a helper function that
  one half defined in its preamble. Every call to it became "command not found": no output,
  no counter increment, exit status ignored. Eighteen checks vanished while their section
  headers still printed and the summary still said *"24 checks passed"* in green. What caught
  it was diffing the merged run's output lines against the two original runs'; nothing in the
  run itself was anomalous.
- **A silent query error.** A helper that returns a sentinel on failure rather than raising
  turns "this query is broken" into "this tile has no data", which reads as a finding about
  the data instead of a bug in the harness.
- **A tautological expectation.** An expectation derived from the artifact under test agrees
  with it whatever it says. This is the single most expensive failure mode here: a tile
  reading the wrong field passed a green check for a day because the check had computed its
  expectation from the field *the tile* named.

Three habits that cost minutes and are the only reliable defences:

1. **Assert the check COUNT, not just the failure count.** A suite that knows it should run N
   checks catches its own silence; one that only counts failures cannot.
2. **Mutation-test every check.** Re-introduce the bug the check exists for and confirm it
   goes red, with an unmodified control that must stay silent. A check that cannot be made to
   fail is not a check, and this is cheap enough to do for all of them.
3. **When you refactor the harness, diff its OUTPUT against the previous run**, not its exit
   code. Same for a rename: a per-artifact variable may mean something different in each half
   you merged.

## A tile can truncate itself

A chart query carries its own row cap. That cap is legitimate syntax, so no structural audit
flags it, and every bucket the tile *does* return is correct, so no value check flags it
either — the series is simply short at one end. It bites when `series × buckets` exceeds the
cap, which means it can pass at the granularity a verifier checks and fail at the granularity
a dashboard uses.

Check it by running each time-series tile's query twice, capped and uncapped, and comparing
row counts. Scope that to queries with a time column: on a top-N table or bar the cap *is* the
panel's size, and flagging those produces false positives that train everyone to ignore the
check. Confirm the panel's own `size` matches before calling a table's limit a bug.

Related, and worth knowing before you trust a grouped result: **the API that runs a chart may
cap its rows with no limit or pagination parameter to raise.** A truncated grouped response is
indistinguishable from a nearly-empty chart. Where that happens, compile the tile's own
aggregation, filter and group-by into a direct query rather than hand-writing an "equivalent"
— a hand-written one re-encodes whatever misunderstanding produced the tile and then agrees
with it.

## Report the residue

Close with an explicit list of what did not survive, each with a class and a reason:
rendering-layer gap (no target chart type), enrichment absent (recreatable via dictionary),
third-party dataset differs (migrate the database to fix), source precision differs
(irreducible). Classifying it is what lets the reader tell a limitation from a bug.
