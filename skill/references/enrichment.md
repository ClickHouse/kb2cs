# Recreating index-time enrichment

Elastic integrations run ingest pipelines that compute fields at index time — `geoip`,
`user_agent`, `dissect`, `grok`, `script`. Those fields are frozen into each document. The
OTel collector does none of it, so only the **raw** string survives.

The framing that matters:

> **Index-time enrichment does not travel with the data, but recreating it is a choice of
> mechanism, not a loss.** What does not travel is the *computation*; on ClickHouse you
> re-express it at query time and it costs a **dictionary**, not a pipeline.

## First: find out what the pipeline actually did

Do not infer it from the field names — read the pipeline:

```bash
curl -s -u "$U:$P" "$ES/_ingest/pipeline" | python3 -c '
import json,sys
for name, p in json.load(sys.stdin).items():
    procs = [k for proc in p.get("processors",[]) for k in proc]
    print(name, "->", ", ".join(sorted(set(procs))))'
```

`geoip`, `user_agent`, `script` and `enrich` processors are the ones that produce fields no
query on the target can recover from the raw data. `grok`/`dissect`/`rename` are usually
recoverable, because the raw string is still there.

## The two mechanisms

| | Elastic | ClickStack |
|---|---|---|
| geo | `geoip` processor + GeoLite2, at index time | `ip_trie` dictionary → `geo_*` `MATERIALIZED` columns |
| user agent | `user_agent` processor + uap-core, at index time | `regexp_tree` dictionary → `ua_*` `MATERIALIZED` columns |

Both are defined once in DDL, are queryable as ordinary columns (so `describe_source` exposes
them and the builder tools reach them), and need **no re-ingest** — a `MATERIALIZED` column
can be backfilled with a mutation and the dictionary can be repointed later.

That last point is a genuine operational improvement worth telling the user about: Elastic's
geo is frozen into each document at index time, while the ClickStack version is a dictionary
lookup you can update and recompute. Same numbers, different lifecycle.

## Making the two sides agree *exactly*

The trick, and it is the whole game: **extract the reference corpus from the source
platform's own installation** rather than downloading it from upstream.

uap-core and the GeoIP databases are versioned, and releases disagree about specific agents
and IP ranges. Pulling the regex corpus out of the running Elasticsearch container's
`ingest-user-agent` jar makes the two platforms agree *by construction* rather than
approximately. On the reference migration this took the result from "close" to **all 20
browser buckets and all 5 OS buckets exact**.

Two structural gotchas:

- **Use three separate dictionaries, not one.** A `regexp_tree` lookup returns the attributes
  of the *first* matching node, so browser and OS patterns in a single tree shadow each other.
  uap-core treats its browser, OS and device parser lists as three independent passes, and so
  must the target.
- **Enumerate, do not guess, what the source recognized.** ua-parser's `Other` bucket is a
  *specific set* of agents, not a remainder — and it emits **no** OS field at all for bots and
  HTTP clients, so those documents appear in no bucket rather than an `Other` one. A
  ClickHouse `multiIf` has no "absent"; it must emit something. Expect the migrated panel to
  carry one extra visible slice for the same partition of the data, and say so.

## When a hand-written expression is the right answer

If the raw field has **few distinct values**, a query-time `multiIf` reproduces the pipeline
exactly and costs nothing to set up. On the reference dataset, 31 distinct user-agent strings
made a `multiIf` exact — every bucket matched Elastic on label and count.

**Prefer the dictionary anyway in production**, for a reason unrelated to correctness: the
hand-written expression lived in three places (two tiles and the verification script), and a
32nd distinct value means editing all three with nothing to catch a miss. The dictionary is
defined once. Use `multiIf` for a one-off proof or a demo; use a dictionary for anything that
will be maintained.

If you do write one, order the branches by specificity — the near-universal trap is that
substrings nest (every Edge UA also contains `Chrome/`; mobile UAs contain the desktop
token). Test the most specific pattern first, and derive the ordering from the source
parser's own rules rather than intuition.

## Geo will not match exactly, and that is not a bug

The answer depends on a third-party dataset belonging to neither platform. GeoLite2 (MaxMind)
and DB-IP Lite classify some ranges differently. On the reference run the top countries agreed
within ±5% while one mid-table country was **+42.7%**.

So: **do not treat the top-three agreement as a tolerance.** Disagreement widens below the
top of the distribution. Diff the whole thing, and report the spread rather than a single
percentage.

> **Geo is the one enrichment where "same data, same query" still does not mean "same
> answer". If a geo panel must match across a migration, migrate the *database*, not just the
> lookup.**

Two value-level details that look like bugs and are not:

- An **empty** country code means "no dictionary entry". Relabel it — `if(geo_country_code =
  '', 'unknown', geo_country_code)` — or it renders as an unlabelled bar.
- `ZZ` is a **different** thing: matched, and the answer is *not a country* (a reserved
  range). On the reference dataset that bucket was exactly the two health-checkers. Do not
  merge it with the empty bucket.
