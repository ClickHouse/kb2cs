# Runbook — reproduce the whole thing from zero

Tear both stacks down, rebuild them, re-run the dashboard migration, and prove it landed.
Every step ends in a check that exits non-zero if it did not, so you can tell whether the
migration went smoothly without eyeballing a single chart.

**Total time: ~15 minutes**, most of it waiting on Fleet and the two data loads.

Prerequisites: Docker with ~8 GB available to it, `python3`, `curl`, and a route to
`epr.elastic.co` (the Fleet package registry) and `download.db-ip.com`.

---

## 0. Full teardown

Skip if you are starting clean. Otherwise this is the only way to be sure you are testing a
cold path rather than leftover state.

**Pass `--profile manual`.** Both `load` services sit behind a compose profile, and a plain
`docker compose down -v` skips them — leaving the stopped `load` container behind, which in
turn holds the `checkpoints` and `secrets` volumes open so they survive too. The filelog
checkpoint surviving is what makes the *next* load resume at EOF and silently ship zero rows.

```bash
cd stack/elastic    && docker compose --profile manual down -v
cd ../clickstack    && docker compose --profile manual down -v
cd ../..

docker volume ls | grep nginx-training    # expect no output
docker ps -a | grep nginx-training        # expect no output
```

Do **not** paper over this with `docker volume rm … 2>/dev/null || true`. That command fails
with `volume is in use` while the profiled container still exists, and hiding its stderr
leaves you believing you started clean when you did not:

```
Error response from daemon: remove nginx-training-clickstack_checkpoints: volume is in use
```

---

## 1. Confirm the source data is intact

The dataset is deterministic — same seed, no wall-clock reads — so this must match before
anything downstream can be trusted.

```bash
shasum -a 256 -c checksums.txt
```

Ten files: the three nginx logs, plus the optional apache service (step 5b), the nginx metrics
series (5c), the apache metrics series (5d) and postgres's log and two metric series (5e).
Lines for steps you have never run will fail; those are the only acceptable failures here.

Optional, ~17 s, if you want to prove the generator still produces those bytes:

```bash
python3 generator/generate.py && python3 generator/validate.py && shasum -a 256 -c checksums.txt
```

`generate-apache.py` is deterministic on the same terms (its own seed, no wall-clock reads);
verified by regenerating and comparing bytes. It has no `validate.py` counterpart — the
apache corpus is checked by loading it, where the integration's own grok is a stricter test
than anything a local validator would assert: `verify-apache.sh` requires zero parse failures
across every `module:level` and status shape in the files.

The OTel `filelog` receiver and Filebeat's `filestream` both read **plain text only**. If you
only have the `.gz` copies, decompress first.

---

## 2. Bring up Elasticsearch + Kibana

```bash
cd stack/elastic
docker compose up -d        # es + kibana, then installs the nginx integration
```

`up -d` returns when containers have *started*, not when Fleet has finished. The nginx
integration install is the slow part (30–60 s on first run, longer if the registry is far
away). Watch it finish:

```bash
docker compose logs -f setup     # wait for "done. Kibana: http://localhost:5601"
```

**If this step fails** it is almost always no route to `epr.elastic.co`. Nothing downstream
works without it, because Fleet is what installs the pipelines *and* the two dashboards you
are going to migrate.

---

## 3. Load Elasticsearch

```bash
# Pin one anchor and reuse it for BOTH stacks, so they land on the same clock.
export SHIFT_ANCHOR_EPOCH=$(date -u +%s)
echo "$SHIFT_ANCHOR_EPOCH"       # write this down; step 4 needs the same value

docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load.py --align-hour   # ~40 s, 511,440 documents
./verify.sh                      # 13 checks
```

`--align-hour` ends the dataset on the most recent whole hour rather than at "now". Combined
with a shared `SHIFT_ANCHOR_EPOCH` this is what keeps the two stacks comparable; plain
`docker compose run --rm load` still works but leaves them drifting apart. See
[Keeping the two stacks on the same clock](#keeping-the-two-stacks-on-the-same-clock).

`verify.sh` must print all 13 green. It checks document counts exactly, asserts **zero** grok
failures, and runs every dashboard panel's underlying aggregation.

> **Re-running this?** Filebeat's registry lives inside the container, so with `--rm` every
> run re-reads from the start. Delete the indices **by name** first — a wildcard
> `DELETE /nginx-training-*` is rejected by ES 8 unless
> `action.destructive_requires_name=false`, and if you miss that you end up with two copies of
> everything and every number doubles.

---

## 4. Bring up and load ClickStack

> **Stop Elasticsearch and Kibana for the duration of the load.** On a machine where Docker
> has ~8 GB this is not optional — skipping it is how the reference run lost 900k rows. You
> bring them straight back afterwards and leave both stacks up from then on; it is only the
> ingest burst that does not fit. See
> [Memory: load one at a time, then run both](#memory-load-one-at-a-time-then-run-both).

```bash
cd ../elastic    && docker compose stop kibana elasticsearch   # data lives on a volume
cd ../clickstack
docker compose up -d        # clickstack + registers a team, captures the ingestion key

# Load with a shift shared with the Elastic stack, so the two land on the same clock.
# SHIFT_ANCHOR_EPOCH is the value you exported in step 3.
export SHIFT_NS=$(../../ingest/clickstack/shift-ns.sh --align-hour)
./load.sh                   # ships all three files through OTLP, detects completion, stops
./verify.sh                 # 15 checks
```

Then bring Elastic back:

```bash
cd ../elastic && docker compose start elasticsearch kibana
```

Use `./load.sh`, not `docker compose run --rm load` — the collector never exits on its own, so
the compose form streams forever and you have to know when to Ctrl-C. `load.sh` watches the
row count and stops for you.

Two ordering traps this handles for you, worth knowing about:

- **OTLP does not exist until a team is registered.** Until then the bundled opamp-managed
  collector runs `receivers: [nop]` and 4317/4318 are *not bound at all* — you get
  connection-refused, not an auth error. `load.sh` gates on the port answering.
- **`load.sh` refuses to append to a non-empty `otel_logs`.** That is deliberate; appending is
  how you get 2,022,808 rows and numbers that are all exactly double.

---

## 5. Enrich: geo and user agent

Both recreate an enrichment Elastic performs at index time, and both take the same shape —
a third-party corpus loaded into a ClickHouse dictionary, exposed as `MATERIALIZED` columns.
Note the `cd`: step 4 ended in `stack/elastic` restarting Elasticsearch, and both scripts
live in `stack/clickstack`.

```bash
cd ../clickstack
./geoip.sh                  # DB-IP City Lite   -> ip_trie dictionary     -> geo_* columns
./ua.sh                     # uap-core regexes  -> regexp_tree dictionary -> ua_*  columns
```

`geoip.sh` is an 87 MB download plus a backfill mutation — a few minutes the first time,
idempotent afterwards. Without it the map-replacement panel has nothing to show.

`ua.sh` takes about 10 seconds and needs **Elasticsearch running**, because it extracts the
uap-core regex corpus from that container's own `ingest-user-agent` jar rather than
downloading it. uap-core is versioned and releases disagree about some agents, so using the
source platform's copy is what makes the two match exactly rather than approximately. If
Elastic is not up, point it at a corpus yourself:

```bash
UA_REGEXES=/path/to/regexes.yml ./ua.sh
```

Expected: browser coverage 95.3%, OS coverage 76.9%. Those are not shortfalls — they are
exactly Elastic's own numbers. Elastic emits **no** `user_agent.os.name` for bots and HTTP
clients (the field is simply absent), so 115,558 requests legitimately have no OS. A ClickHouse
column must hold something, so they land in `'Other'`; filter `ua_os != 'Other'` to compare
like for like with Kibana's donut.

> Step 4 told you to stop Elasticsearch during the ClickStack load. Start it again *before*
> this step — `ua.sh` needs it.

---

## 5b. Add the apache service (optional)

Everything above produces the nginx dataset. This step adds a **second service** — an apache
documentation site — which is what the `[Logs Apache] Access and error logs` migration is
verified against. Skip it if you only care about nginx; nothing else depends on it.

```bash
cd ../../generator
python3 generate-apache.py          # ~250k requests + 14,665 error entries, ~65 MB
cd ../                              # repo root
shasum -a 256 -c checksums.txt      # all five files, nginx + apache
```

Load Elasticsearch, reusing the **same** `SHIFT_ANCHOR_EPOCH` from step 3 so both services
land on one clock:

```bash
cd stack/elastic
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load.py --service apache --align-hour     # 264,662 documents
```

`--service apache` replaces only `logs-apache.*`; it does not touch the nginx data streams.
Then ClickStack, with apache's **own** shift — note `SERVICE=apache`, which anchors on
apache's last event rather than nginx's:

```bash
cd ../elastic && docker compose stop kibana elasticsearch     # the 8 GB rule still applies
cd ../clickstack
export SHIFT_NS=$(SERVICE=apache ../../ingest/clickstack/shift-ns.sh --align-hour)
./load-apache.sh                    # expects exactly 264,662 apache rows
cd ../elastic && docker compose start elasticsearch kibana
cd ../clickstack && ./verify-apache.sh                        # 20 checks
```

**Why this step comes after step 5, not with steps 3–4.** `geo_*` and `ua_*` are MATERIALIZED
columns, and MATERIALIZED computes **on insert**. Once `geoip.sh` and `ua.sh` have created the
columns and dictionaries, apache rows are enriched as they land — no second backfill, no
second dictionary. That is also why the apache collector deliberately emits nginx's attribute
names (`remote_addr`, `http_user_agent`): the column expressions are written in terms of
those. Load apache *before* step 5 and it still works, but you pay for a backfill mutation
over both services instead of one.

`load-apache.sh` is not `load.sh`. `load.sh` refuses any non-empty `otel_logs`, which is
correct when the table should hold exactly one corpus; apache is additive, so its guard counts
`ServiceName = 'apache'` rows instead. To reload just apache:

```bash
docker compose exec clickstack clickhouse-client --query \
  "ALTER TABLE default.otel_logs DELETE WHERE ServiceName = 'apache'"
docker compose rm -sf load-apache
docker volume rm nginx-training-clickstack_checkpoints_apache
```

---

## 6. Point the MCP servers at the fresh stack

The ClickStack personal API key is regenerated whenever the mongo volume is recreated, so the
`.mcp.json` from your last run is now stale.

```bash
cd ../..
./stack/clickstack/write-mcp-config.sh
```

Then **restart Claude Code from the repo root and approve both servers at the prompt.** MCP
servers attach at session start, so approving them cannot affect a session that is already
running. This is the step people miss.

This is not optional after a `down -v`: recreating the mongo volume issues a **new** personal
API key, so any session started before step 6 keeps using the old one and fails with

```
MCP server "clickstack" requires re-authorization (token expired)
```

Verified on the reference run — the key changed from `677dad4b…` to `64d5bd3d…`, and the
already-running session's ClickStack tools stopped working at exactly that point. The
Elasticsearch MCP server is unaffected (it authenticates with static credentials), so you can
be half-connected and not notice.

```bash
claude mcp list      # both must read "Connected", not "Pending approval"
```

---

## 7. Run the migration

With both servers connected, ask Claude Code to execute `MIGRATION.md`. The procedure in that
file is the spec; briefly, it exports the two dashboards from the Kibana Saved Objects API,
calls `clickstack_describe_source` before writing any query, translates the 10 panels using
the field-mapping tables, creates them with `clickstack_save_dashboard`, and validates every
tile with `clickstack_query_tiles`.

Expected output: **2 dashboards + 2 saved searches**, tagged `nginx` and
`migrated-from-kibana`.

| Object | Type | Tiles |
|---|---|---|
| `[Logs Nginx] Overview (migrated)` | dashboard | 8 (7 panels + a provenance note) |
| `[Logs Nginx] Access and error logs (migrated)` | dashboard | 4 (3 panels + a note) |
| `Nginx access logs (migrated)` | saved search | — |
| `Nginx error logs (migrated)` | saved search | — |

---

## 8. Prove it landed

```bash
./stack/clickstack/verify-migration.sh
```

**18 checks.** This is the answer to "did the migration go smoothly":

- all four objects exist, with the expected tile counts
- every queryable tile carries a `log.stream` predicate — the omission that silently doubles
  every access number
- the data invariants still hold (499,964 · 11,476 · 12,659 · 10,557,094,271 · status
  families · error levels)
- **the `multiIf` expressions stored inside the OS and Browser tiles are executed and diffed
  against Elastic's ua-parser output** — all 5 OS buckets and all 20 browser buckets, by name
  and count. This is the check that catches a migration that looks right and is not.
- geo is populated, with a reminder that DB-IP and MaxMind legitimately disagree

It exits non-zero and names the failing object. Expected tail:

```
18 checks passed. The migration reproduced cleanly.
```

---

## 9. Compare the two side by side

```
Kibana   http://localhost:5601   elastic / changeme      Analytics > Dashboard > "nginx"
HyperDX  http://localhost:8080   train@example.com / TrainingP4ss!
```

**Set both to the same timezone before comparing anything visually.** Both platforms store
UTC, but each renders in its own configured zone: Kibana's `dateFormat:tz` defaults to
`Browser`, and HyperDX has a separate *Settings → Use UTC time* switch. If they disagree, the
diurnal curve appears shifted by your UTC offset and the migration looks broken when it is
not. This affects labels only — every number in `verify-migration.sh` is queried with explicit
UTC bounds and is unaffected.

Also set the HyperDX time range to cover the data. The load shifts the dataset to end at
roughly "now", so **Last 24 hours** works; the default 15-minute window shows almost nothing.

---

## Memory: load one at a time, then run both

The constraint is **ingest**, not coexistence. Loading 1M records into the ClickStack
all-in-one is a burst that needs far more headroom than serving queries afterwards does, so
the rule is simply: load the two stacks one at a time, then bring both up and leave them up.

Measured on the reference machine (Docker 7.75 GiB, with an unrelated `kind` cluster also
taking 0.75 GiB):

| | ES | Kibana | ClickStack | total (excl. `kind`) |
|---|---:|---:|---:|---:|
| Both up, loaded, idle | 2.69 GiB | 0.56 GiB | 1.90 GiB | **5.15 GiB** |
| Both up, under concurrent dashboard queries | 2.71 GiB | 0.56 GiB | 2.20 GiB | **5.47 GiB** |

Both stacks ran healthy throughout 30 rounds of concurrent `date_histogram` + `terms`
aggregations against Elasticsearch and the equivalent grouped scans against ClickHouse. So
once the data is in, **you do not need to stop anything** — run both side by side, which is
the whole point of the exercise.

Ingest is the phase that does not fit.

### What happens if you ingest with the other stack running

**Found the hard way on the reference run.** With a fully loaded Elasticsearch (2.1 GiB) plus
Kibana (0.5 GiB) already resident, the ClickStack all-in-one died mid-load with **exit 129**,
and the failure is much worse than a crash:

```
[load]   100076 rows[load]   0 rows[load]   0 rows ...
[load] WARNING: 0 rows, expected 1011404
... dial tcp [::1]:4318: connect: connection refused
```

The all-in-one runs ClickHouse, Mongo, HyperDX and a collector in one memory budget, and the
compose file sets **no `mem_limit`** — so it competes with everything else on the host rather
than failing predictably.

**Why the second attempt is worse than the first.** The collector checkpoints how far it has
read. When ClickStack dies, in-flight batches are dropped (`Exporting failed. Dropping data`)
but the checkpoint has already advanced. Restarting the collector by hand — `docker compose up
-d load` — resumes *past* the dropped records, and they are gone for good. On the reference
run that produced a stuck total of 210,076 rows that no amount of waiting would fix.

**Recovery, in this order — all three steps, or you make it worse:**

```bash
docker compose stop load && docker compose rm -sf load
docker compose exec -T clickstack clickhouse-client --query "TRUNCATE TABLE default.otel_logs"
docker volume rm nginx-training-clickstack_checkpoints
```

Then free the memory and reload. Always use `./load.sh` rather than starting the collector
yourself: it clears the checkpoints for you, which is precisely the step that was skipped
above.

With Elasticsearch and Kibana stopped, the same load completed in **51 seconds** with
1,011,404 rows and zero duplicates — then both stacks came back up and have coexisted happily
since.

Two caveats on the numbers above. The peak ClickStack figure *during* ingest was not
measured — only that it fails alongside a loaded ES and succeeds without it — so treat "load
one at a time" as the tested rule rather than tuning against a threshold. And if Docker has
12 GB or more, or the host is otherwise idle, you may well get away with loading both
concurrently; `docker stats --no-stream` before you start is the cheap way to find out.

---

## Keeping the two stacks on the same clock

> **Re-anchoring now covers five services, not one.** Done on 2026-09-17; the corpus spans
> 2026-09-16 14:00 → 2026-09-17 14:00 UTC. Ten loads per stack:
>
> ```bash
> date -u +%s > /tmp/nginx-anchor.txt          # A=$(cat /tmp/nginx-anchor.txt)
> # Elasticsearch — logs then metrics, five services each
> cd stack/elastic
> for s in nginx apache postgresql mysql system; do
>   docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$A" load \
>     python /load.py --service "$s" --align-hour --anchor-epoch "$A"
>   docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$A" load \
>     python /load-metrics.py --service "$s" --align-hour --anchor-epoch "$A"
> done
> docker compose stop elasticsearch kibana      # the service is `elasticsearch`, not `es`
> # ClickStack — truncate, clear every checkpoint volume, reload
> cd ../clickstack
> for t in otel_logs otel_metrics_sum otel_metrics_gauge; do
>   docker compose exec -T clickstack clickhouse-client --query "TRUNCATE TABLE default.$t"; done
> docker compose rm -sf load load-apache load-postgres load-mysql load-system
> for v in checkpoints checkpoints_apache checkpoints_postgres checkpoints_mysql checkpoints_system; do
>   docker volume rm "nginx-training-clickstack_$v"; done
> for p in nginx:load.sh apache:load-apache.sh postgres:load-postgres.sh \
>          mysql:load-mysql.sh system:load-system.sh; do
>   SHIFT_NS=$(SHIFT_ANCHOR_EPOCH="$A" SERVICE="${p%%:*}" ../../ingest/clickstack/shift-ns.sh --align-hour) \
>     ./"${p##*:}"; done
> cd ../elastic && docker compose start elasticsearch kibana && cd ../clickstack
> for s in nginx apache postgresql mysql system; do
>   k=$s; [ "$s" = postgresql ] && k=postgres
>   SHIFT_NS=$(SHIFT_ANCHOR_EPOCH="$A" SERVICE="$k" ../../ingest/clickstack/shift-ns.sh --align-hour) \
>     python3 load-metrics.py --service "$s"; done
> ```
>
> **Enrichment does not need re-running.** `TRUNCATE` keeps the materialized `geo_*`/`ua_*`
> columns and the dictionaries stay loaded, so new rows are enriched on insert. Don't be
> fooled by `element_count = 0` on the `regexp_tree` dictionaries — that is how they report.
>
> **Expect verifier failures if any check embeds an absolute date.** Thirteen did on
> 2026-09-17; `verify-{metrics,apache-metrics,postgres,apache}.sh` and
> `verify-tiles-vs-elastic.py` now derive their comparison window from the data instead.

**Following steps 3 and 4 as written will leave the two stacks tens of minutes apart, and the
same wall-clock window will return different counts on each.** This is the single most
confusing thing about comparing the two platforms, so it is worth understanding before you
conclude the migration is broken.

### Why it happens

Both loaders shift the dataset forward so it ends at "now" — and each computes that shift
independently, at the moment it runs:

| | reference | evaluated |
|---|---|---|
| Elastic (`load-to-datastream.py`) | last event in `access.log` | once, when the script starts |
| ClickStack (`otel-collector-nginx.yaml`) | the literal `2026-08-17T23:59:59Z` | `Now()`, **per record**, as the collector processes it |

So the skew between the two stacks equals **the wall-clock gap between the two loads**. Step 3
(Fleet install + ES load) takes several minutes; if you then break for coffee before step 4,
that break is baked into the data. On the reference run the two ended up 40 minutes apart:

```
Elasticsearch   2026-08-19T13:24:19Z → 2026-08-20T13:24:18Z
ClickStack      2026-08-19T14:04:25Z → 2026-08-20T14:04:32Z
```

The consequence is the one people actually hit: the half-hour bucket at local `00:00` held
**15,278** rows in Kibana and **14,310** in HyperDX. Nothing was missing — the two platforms
were showing different 30 minutes of the same diurnal curve.

Note also that the collector's `Now()` is evaluated *per record*, so the ClickStack window is
very slightly stretched rather than rigidly translated (4–8 s of spread across 24 h, and up to
3.4 s between the two access streams, which are read concurrently). Immaterial for charts;
worth knowing if you ever compare timestamps across streams.

### Detecting it

`verify-migration.sh` reports the skew as an advisory check whenever Elasticsearch is
reachable. Or directly:

```bash
curl -s -u elastic:changeme "http://localhost:9200/logs-nginx.access-default/_search" \
  -H 'Content-Type: application/json' \
  -d '{"size":0,"aggs":{"mn":{"min":{"field":"@timestamp"}}}}' \
  | python3 -c "import json,sys;print('ES:',json.load(sys.stdin)['aggregations']['mn']['value_as_string'])"

docker compose -f stack/clickstack/docker-compose.yml exec -T clickstack \
  clickhouse-client --query "SELECT 'CH:', min(Timestamp) FROM default.otel_logs
                             WHERE LogAttributes['log.stream']='access_json'"
```

### Fixing it

Pick based on what you are doing:

| | How | Result |
|---|---|---|
| **A. Hour anchor + shared epoch** *(recommended — what steps 3 and 4 do)* | Export `SHIFT_ANCHOR_EPOCH` once. ES: `--align-hour` with that variable. ClickStack: `export SHIFT_NS=$(./ingest/clickstack/shift-ns.sh --align-hour)` with the same variable. | **Measured: 0 s apart.** Both datasets end on the same whole hour. Bucket edges line up with the time picker too. |
| **B. Don't shift at all** | ES: `--no-shift`. ClickStack: `SHIFT_NS` unset *and* the `Now()` statement commented out in the collector config. | Both keep the original `2026-08-17` dates and match exactly. You must use an absolute time range in both UIs; no relative picker finds the data. |
| **C. Leave it and compare relatively** | nothing | Fine for `verify-migration.sh`, which is entirely window-independent, and for any comparison of totals or distributions. Not fine for eyeballing two time charts. |

### How the anchor works

Both loaders accept the same two knobs, and they agree on the arithmetic:

- `SHIFT_ANCHOR_EPOCH` — the instant the dataset's **last** event is moved to. Default: now.
  Pinning it is what makes two loads that run minutes (or hours) apart produce an identical
  shift.
- `--align-hour` — rounds that anchor down to a whole hour.

Used together they are fully deterministic. On the reference run the ClickStack shift was
recomputed 20 minutes after the Elastic load, across a `:00` boundary, and still produced a
byte-identical `SHIFT_NS=230400086999893` — which is the entire point of pinning the anchor.
`--align-hour` *without* a shared anchor is only deterministic inside one clock hour.

Measured afterwards:

```
ES: 2026-08-19T16:00:01.000Z -> 2026-08-20T16:00:00.000Z
CH: 2026-08-19 16:00:00.307  -> 2026-08-20 15:59:59.999999893
```

and the half-hour bucket that started this — local `00:00` — now reads **15,532** in Kibana
against **15,535** in HyperDX. Three rows, 0.019%, and they are the whole-second vs
millisecond boundary rounding described above, not a shift.

> **`--align-hour` trades recency for alignment.** The data can be up to two hours old by the
> time you look at it, so HyperDX's *Last 1h* preset will look empty — use *Last 24 hours*.
> `verify.sh` allows this (its recency threshold is 2 h) and prints a note when it applies.

---

## What "smoothly" looks like

| Step | Check | Expect |
|---|---|---|
| 1 | `shasum -a 256 -c checksums.txt` | 3 × OK |
| 3 | `stack/elastic/verify.sh` | 13 checks pass |
| 4 | `stack/clickstack/verify.sh` | 15 checks pass |
| 8 | `stack/clickstack/verify-migration.sh` | 18 checks pass, alignment `0s` |

Anything less and the failing check names what to fix.

This runbook has been executed end to end twice from a cold teardown. Second run: 18/18,
alignment `0s`, and the local half-hour bucket that motivated the whole alignment exercise read
15,532 in Kibana against 15,535 in HyperDX — the irreducible 3-row precision residual, nothing
else.

Reference timings, measured on a full teardown-and-rebuild (Docker 7.75 GiB, Apple silicon):

| Step | Time |
|---|---|
| 2 — ES + Kibana up, Fleet + nginx integration installed | ~15–25 s |
| 3 — ES load, 511,440 docs | ~40 s |
| 4 — ClickStack up, then load 1,011,404 rows (**ES stopped**) | ~50 s |
| 5 — `geoip.sh`, 3.65 M CIDR blocks, 96.6% coverage | ~10 s (cached; minutes on first download) |
| 5 — `ua.sh`, 323 browser + 164 os + 616 device patterns | ~8 s (needs ES running) |

Well under the 15 minutes budgeted at the top, provided you do not fight the memory problem.

## Known-good divergences — not bugs

Several things differ between the platforms by design. All are documented in `MIGRATION.md`;
they are listed here so a first-time runner does not chase them.

The first four below are cases where the two platforms legitimately disagree. The last three
are different in kind: **Elasticsearch is simply wrong and ClickStack is right**, so matching
Elastic would mean reproducing a bug. They only appear if you ran step 5b (apache), and
`verify-apache.sh` asserts each one so nobody "corrects" them.

- **No map.** The Kibana Overview dashboard has a geo map; ClickStack has no map tile type at
  all (its chart types are line, stacked_bar, table, number, pie, bar, heatmap, search,
  event_patterns, markdown, sql). The panel is migrated as a country bar chart. The *data*
  migrated fine — `geo_latitude`/`geo_longitude` are queryable, just not plottable.
- **Geo counts differ by vendor.** Elastic uses MaxMind GeoLite2, `geoip.sh` uses DB-IP City
  Lite. The top three agree within ~3%, but **Canada differs by +42.7%**. Two databases, same
  synthetic IPs, different answers. Do not treat the ±3% on the big countries as a tolerance.
- **The same wall-clock window returns different counts on each stack.** Expected unless you
  loaded both from a common shift — each loader shifts to "now" independently, so the two
  datasets sit tens of minutes apart. Totals and distributions still match exactly. See
  [Keeping the two stacks on the same clock](#keeping-the-two-stacks-on-the-same-clock).
- **Sub-second precision differs.** Elastic's access index is built from `access.log` (stock
  combined, whole seconds); ClickStack ingests `access.json.log` (`msec`, milliseconds). An
  event at `12:29:59.400` is `12:29:59` to Elastic and `12:29:59.400` to ClickStack — the same
  instant to the second, but the two can fall on opposite sides of a bucket edge. Measured
  with the stacks perfectly aligned: **0–3 rows per 30-minute bucket, ≤0.02%**.

  **This is irreducible.** It is not a shift, a rounding bug, or something a query can correct
  — the two platforms are reading *different files* with different timestamp resolutions, and
  the second-precision one physically does not contain the information needed to place those
  events more precisely. The only way to remove it is to change what one stack ingests: point
  Filebeat at `access.json.log` instead of `access.log`, which would also give the Elastic side
  the upstream/timing/TLS fields it currently lacks — and would stop the two stacks
  demonstrating the combined-vs-JSON parsing contrast the dataset exists to show. Not worth it.
  Treat sub-1% bucket differences as expected and compare totals instead.

Now the three where Elastic is the one at fault (apache only, step 5b):

- **IPv6 client addresses are truncated by Elastic.** The apache integration's grok cannot
  match a bare IPv6 address in a combined log line: it keeps only the final hextet and files
  it under `source.domain` rather than `source.ip`. 7,881 of 249,997 apache requests are
  affected, and because 60 distinct addresses collapse onto 59 trailing hextets, Kibana
  reports **5,716** unique client IPs where ClickStack reports **5,718**. Nothing is logged —
  no `_grokparsefailure`, because a looser alternative in the pattern still matches.
- **Elastic drops `url.original` when the request line contains a backslash.** The 503
  requests to the ThinkPHP probe path (`/index.php?s=/Index/\x5Cthink…`) have a status code,
  a client IP and a user agent in Elasticsearch but **no URL at all**, so they are silently
  missing from Kibana's URL panel: 190 distinct URLs there against 191 on ClickStack.
- **Elastic's user-agent version can end in a stray dot.** `Firefox/141.0` becomes
  `user_agent.version = "141.0."`, because uap-core's Firefox rule has a trailing `(\d*)`
  group that *matches the empty string* and Elastic appends a separator for it. A ClickHouse
  back-reference cannot distinguish "matched empty" from "did not match", so
  `ua_browser_version` is `141.0` — the same 15,801 rows, rendered correctly.

None of the three were visible from reading panel definitions. They surfaced from diffing
whole distributions against the live source, which is the argument for doing that rather than
spot-checking a few numbers you wrote down.

---

## 5c. Add the nginx metrics series (optional)

Gives `[Metrics Nginx] Overview` something to draw, on both stacks. Independent of step 5b
(apache); skip either or both. The series is **derived from the nginx access log**, so it only
makes sense alongside the nginx logs of steps 3–4.

```bash
cd generator
python3 generate-nginx-metrics.py     # 8,640 scrapes, ~1.6 MB, reads data/access.json.log
cd ..
shasum -a 256 -c checksums.txt        # six files now
```

Elasticsearch, reusing the **same** `SHIFT_ANCHOR_EPOCH` as step 3 so the metrics land on the
logs' clock:

```bash
cd stack/elastic
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load-metrics.py --align-hour        # 8,640 documents
```

Then ClickStack. No collector and no compose service: metrics are already structured, so the
loader posts OTLP/JSON straight to `:4318/v1/metrics`.

```bash
cd ../clickstack
export SHIFT_NS=$(../../ingest/clickstack/shift-ns.sh --align-hour)   # nginx's shift
python3 load-metrics.py                       # 25,920 sum + 34,560 gauge points
./verify-metrics.sh                           # 19 checks
```

Unlike step 5b this does **not** need Elasticsearch stopped: 60k metric points is nothing
next to a 1M-row log ingest.

**Two traps worth knowing before you hit them.**

`metrics-nginx.stubstatus` is a **TSDB** data stream (`index.mode: time_series`), and a TSDB
write index only accepts timestamps within `index.look_back_time` of now — **two hours** by
default. A 24h backfill is rejected outright. `load-metrics.py` therefore writes
`look_back_time: 30h` into the `metrics-nginx.stubstatus@custom` component template *before*
creating the data stream; the setting is read when a backing index is created, so the order
matters. `@custom` is the supported place for this and survives package upgrades.

Fleet may also have installed the index templates for the metrics data stream **without** its
ingest pipeline, in which case the first write fails with
`pipeline with id [metrics-nginx.stubstatus-3.2.2] does not exist`. Force a reinstall:

```bash
curl -s -u elastic:changeme -X POST \
  "http://localhost:5601/api/fleet/epm/packages/nginx/3.2.2" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' -d '{"force":true}'
```

---

## 5d. Add the apache metrics series (optional)

Needs step 5b (the apache logs) first — the counters are derived from `data/apache/access.log`.

```bash
cd generator && python3 generate-apache-metrics.py && cd ..
shasum -a 256 -c checksums.txt              # seven files now

cd stack/elastic
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load-metrics.py --service apache --align-hour     # 5,760 documents

cd ../clickstack
export SHIFT_NS=$(SERVICE=apache ../../ingest/clickstack/shift-ns.sh --align-hour)
python3 load-metrics.py --service apache     # 40,320 sum + 103,680 gauge points
./verify-apache-metrics.sh                   # 21 checks
```

Note `SERVICE=apache` on `shift-ns.sh` and `--service apache` on both loaders: apache's series
is anchored on the *apache* access log, so it lands on the apache logs' clock rather than
nginx's. Mixing them up shifts the metrics by a few seconds relative to their own logs, which
breaks the "total_bytes equals sum(body_bytes_sent)" cross-check.

Unlike the nginx metrics step this one has no TSDB surprise: the `metrics-apache.status`
ingest pipeline is installed by default, so no `force:true` package reinstall is needed. The
`look_back_time` override still is, and the loader still applies it.

---

## 5e. Add the PostgreSQL service (optional)

Independent of steps 5b–5d. Postgres is a third service with its own log and **two** metric
series, so it is the fullest of the optional steps — and the only one where nothing about the
dashboards is unmigratable.

```bash
cd generator && python3 generate-postgres.py && cd ..   # writes all THREE files
shasum -a 256 -c checksums.txt                          # ten files now

# Elasticsearch: the query log, then both metric data streams
cd stack/elastic
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load.py --service postgresql --align-hour            # 120,421 documents
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load-metrics.py --service postgresql --align-hour    # 8,640 + 4,598 documents

# ClickStack: the log via a collector, the metrics via OTLP
cd ../clickstack
export SHIFT_NS=$(SERVICE=postgres ../../ingest/clickstack/shift-ns.sh --align-hour)
./load-postgres.sh                             # 120,421 rows into otel_logs
python3 load-metrics.py --service postgresql   # 127,226 metric points
./verify-postgres.sh                           # 20 checks
```

Note `SERVICE=postgres` on `shift-ns.sh` (the data directory and shift key) but
`--service postgresql` on the loaders (the **integration** name). They differ on purpose:
`event.module` and `service.type` are `constant_keyword` in the integration's mapping and must
read `postgresql`, so a service key of `postgres` is rejected at index time with a
`document_parsing_exception`. That is what the error looks like if you get it wrong.

Three things specific to this step:

- **The generator writes three files at once**, and the two metric series are *derived from the
  log* — pg_stat_statements is the aggregate of the statements postgres ran. That is what makes
  `sum(query.calls) == 120,000` a checkable invariant rather than a coincidence.
- **Two metric data streams, one `--service`.** `load-metrics.py` loops over them; each gets
  its own `look_back_time` override, since both are TSDB.
- **No `force:true` reinstall needed.** Unlike nginx, the postgresql package installs all
  eight of its ingest pipelines on the first install.

## 5f. Add the MySQL service (optional)

Independent of steps 5b–5e. MySQL is a fourth service with **two** logs and **two** metric
series, and it is the only one whose log format is not one record per line — see below.

```bash
cd generator && python3 generate-mysql.py && cd ..   # writes all FOUR files
shasum -a 256 -c checksums.txt                       # fourteen files now

# Elasticsearch: both logs, then both metric data streams
cd stack/elastic
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load.py --service mysql --align-hour              # 2,513 + 267 documents
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load-metrics.py --service mysql --align-hour      # 2,880 + 2,880 documents

# ClickStack: the logs via a collector, the metrics via OTLP
cd ../clickstack
export SHIFT_NS=$(SERVICE=mysql ../../ingest/clickstack/shift-ns.sh --align-hour)
./load-mysql.sh                            # 2,780 rows into otel_logs
python3 load-metrics.py --service mysql    # 115,200 metric points
./verify-mysql.sh                          # 36 checks
python3 ../../../verify/verify-tiles-vs-elastic.py mysql   # 42 of its 157 series
```

Run the second one on any migration you touch. `verify-mysql.sh` asserts totals and structure;
`../verify/verify-tiles-vs-elastic.py` reads each **sql** tile's stored `sqlTemplate` back and runs it,
and re-issues each **builder** tile through `clickstack_timeseries` with the tile's own
select/groupBy/where — or compiles the tile's own `aggFn`/`where`/`groupBy` to SQL when it is
grouped, because that tool caps its rows with no way to page and returned 107 of ~1,100 for
one tile. It is the only pass that catches a series which is the right shape and wrong in
every bucket — it found 8 such series in the *already signed-off* nginx and apache metrics
dashboards, off by about 1%, and on being extended past mysql it found three more defects
(`INTEGRATIONS.md`, "Closing the value gap").

With no arguments it runs all four integrations and the estate-wide row-cap preflight; pass
integration names (`mysql nginx apache postgres`) to narrow it, or `--list` to see coverage
and each integration's declared divergences.

### The slow log is a multi-line record

This is the one genuinely new ingest problem in the repo. A single slow query is five lines:

```
# Time: 2026-08-17T00:00:00.266864Z
# User@Host: cms_ro[cms_ro] @ app-01 [10.0.2.11]  Id: 17579
# Query_time: 1.636744  Lock_time: 0.003343 Rows_sent: 59  Rows_examined: 10856
SET timestamp=1786924800;
SELECT t.slug, count(*) AS n FROM posts p JOIN terms t ON t.post_id = p.id GROUP BY t.slug ...;
```

and the integration's grok expects all five in one `message`. Read line by line you get five
documents per query, four of which contain no query at all — **and nothing errors**, because
each of those lines is a perfectly valid log line. The symptom is a row count five times too
high: 12,565 instead of 2,513. Both loaders therefore split on the `# Time: ` header:

- `load-to-datastream.py` — `read_records(f, record_start)`, selected by a 4th element on the
  service's `files` tuple. `verify-mysql.sh` asserts 2,513 rows *and* that no header line
  leaked into a parsed query, which is what a half-working reader produces.
- `otel-collector-mysql.yaml` — `multiline.line_start_pattern: '^# Time: '` on the slowlog
  receiver only. The error log is ordinary one-line-per-record and must NOT get it, which is
  why they are two receivers rather than one `include:` list.

One consequence of `line_start_pattern`: a record is not emitted until the next one starts or
`force_flush_period` (500 ms) expires, so the last query in the file arrives half a second
after the rest. Harmless for a bounded file; it matters if you point it at a live log.

### `SET timestamp` is the timestamp, not `# Time:`

Measured against `logs-mysql.slowlog-1.28.1/_simulate`, not assumed: make the two disagree
and `@timestamp` follows `SET timestamp`; delete the `SET` line and `@timestamp` is not set at
all; delete `# Time:` and nothing changes. So the shifter rewrites `SET timestamp` (and
rewrites `# Time:` too, purely to keep the file self-consistent).

That has a convenient consequence: `SET timestamp` is whole seconds by format, so both stacks
compute a byte-identical delta for mysql — the same property apache has, and the reason
neither needs nginx's sub-second reconciliation. Note the slow log is also the only anchor
file in the repo whose **last line is not a timestamp** (it is the SQL), so `shift-ns.sh` and
both `compute_delta`s scan the tail backwards for the final `SET timestamp=` instead of
reading the last line.

### The error log's `@timestamp` is re-derived from the message

`logs-mysql.error-1.28.1` renames the incoming `@timestamp` to `event.created` and then sets
`@timestamp` from the text. A loader that shifts only the document field would have its value
silently discarded. Two side effects worth knowing:

- processor 5 is `rename @timestamp -> event.created` with **no `ignore_missing`**, so
  `_simulate` aborts with `field [@timestamp] doesn't exist` unless you supply one. That is a
  simulate artifact, not a pipeline bug — real ingest always has an `@timestamp`.
- mysqld writes 6 fractional digits; Elastic's date processor keeps 3, the collector keeps all
  6. The two stacks therefore differ by up to 999 µs on error-log rows. Irrelevant for
  bucketing, but do not expect a byte-equal instant.

### Three things the metrics step gets right on purpose

- **Sum vs Gauge is decided by the PANEL, not by Elastic's metric type.** Five fields the
  integration types `counter` ship as gauges because their panels read the absolute value:
  `max_used_connections` (a high-water mark, read with `max()`), `innodb.buffer_pool.pool.reads`
  and `.read.requests` (two halves of a lifetime ratio), and the replica's two binlog positions
  (read with `last_value()`). One field that looks like a gauge ships as a **Sum**:
  `cache.ssl.size` is a constant 128 and its panel `differences()` it, so Kibana plots a flat
  zero — a gauge would have plotted 128 and disagreed silently.
- **A gauge panel that aggregates within a bucket needs a SQL tile.** HyperDX collapses a
  gauge to one sample per bucket (the last) *before* `aggFn` runs, so `avg`/`max`/`min`/`sum`/
  `last_value` all return the same number. Measured: a bucket whose samples give max=14,
  avg=10.8, last=10 returns **10 for all five**. Six of the 16 Database Overview panels are
  SQL tiles for this reason; `verify-mysql.sh` asserts they stay that way.
- **One attribute that moves with the value destroys series identity.**
  `source.file_info` is "<binlog file> <byte position>", so attaching it to each data point
  gave 2,879 distinct series for one replica instead of 1. Nothing errors — the tiles just
  read an arbitrary point per bucket. It is reconstructed in the Source overview tile's SQL
  instead. `verify-mysql.sh` asserts `uniqExact(Attributes) = 1` per replica metric.

## 5g. Add the SYSTEM service (optional)

Independent of steps 5b–5f, and the widest of them: **10 data streams**, more than the other
four services combined. Two generators, two logs, eight metric series.

```bash
cd generator
python3 generate-system-logs.py      # data/system/{syslog,auth}.log
python3 generate-system-metrics.py   # data/metrics/system-*.jsonl, eight files
cd .. && shasum -a 256 -c checksums.txt          # twenty-four files now

# Elasticsearch: both logs, then all eight metric streams
cd stack/elastic
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load.py --service system --align-hour            # 40,000 + 16,224 documents
docker compose run --rm -e SHIFT_ANCHOR_EPOCH="$SHIFT_ANCHOR_EPOCH" load \
  python /load-metrics.py --service system --align-hour     # 108,000 documents

# ClickStack: the logs via a collector, the metrics via OTLP
cd ../clickstack
export SHIFT_NS=$(SERVICE=system ../../ingest/clickstack/shift-ns.sh --align-hour)
./load-system.sh                           # 56,224 rows into otel_logs
python3 load-metrics.py --service system    # 396,000 metric points
./verify-system.sh                          # 44 checks
python3 ../../../verify/verify-tiles-vs-elastic.py   # 157 tile series bucket-for-bucket
                                            #   (mysql/nginx/apache/postgres; system pending)
python3 ../../../verify/verify-controls.py   # 15 checks on the dashboard dropdowns
```

Both loaders take `--part <substring>` to reload one stream of a multi-stream service without
touching the rest — e.g. `--part system-cpu` after regenerating only that file.

### Syslog has no year, and the two streams disagree about the timestamp

`Aug 17 00:00:01` carries no year, so every reader supplies one and they all default to the
current year — Elastic Agent, Filebeat and the OTel stanza parser alike. A syslog archive
backfilled across a New Year boundary is misdated by all of them. `SYSLOG_YEAR` is set
explicitly in both loaders and in `shift-ns.sh`; they must agree or the two stacks land a year
apart.

Worse, the two streams derive `@timestamp` in **opposite** ways — measured against
`_simulate`, not assumed:

| stream | behaviour |
|---|---|
| `logs-system.syslog` | **re-derives** `@timestamp` from the message text, discarding the document's |
| `logs-system.auth` | **keeps** the document's `@timestamp` (its date processors are gated on `ctx['@timestamp'] == null`) |

So `shift_syslog_line` rewrites the timestamp inside the line *and* returns an ISO for the
document. Do only one and one stream lands on the wrong day while the other looks perfect.

### `look_back_time` must be computed, not hardcoded

The first metrics load rejected **4,930 of 7,200** cpu documents. TSDB's write index refuses
any timestamp older than `look_back_time` before *now*; the loader hardcoded `30h`; and this
static corpus had aged to begin **46 hours** in the past. The same command had worked the day
before. `look_back_time` is now derived from the data's own age and floored at 30h — the only
form that survives the corpus ageing. If you see mass rejections on a metrics backfill, check
the data's age before anything else.

### What the two metrics dashboards cost

37 of the 50 data tiles are SQL — and the split is total: **all 33 metrics data tiles are raw
SQL, while 13 of the 17 logs tiles are builder tiles.** Five separate reasons, all listed in
`INTEGRATIONS.md`, but two are new here:

- **`system.cpu.total.norm.pct` and `system.fsstat.total_size.*` have no OTel equivalent.**
  hostmetrics has no `total` CPU state (it is the sum of the six non-idle ones) and no fsstat
  rollup (it is the sum of the per-mount filesystem numbers). Both are derived on the target,
  and `verify-system.sh` asserts each derivation against the source.
- **`reducedTimeRange='30s'`** asks for a 30-second window that a 60-second scrape interval
  cannot fill, so the Kibana panel reads 0 as well. The tiles use the newest two scrapes.

### The two heatmaps cannot be migrated as heatmaps

ClickStack has a `heatmap` displayType, but it is a **value-distribution** heatmap: one
series, a numeric `valueExpression` bucketed against time, **no `groupBy`**. Kibana's two
Overview panels put `terms(host.name)` on the y-axis. Both degrade to a line chart grouped by
host — same data, different rendering — and `inventory-panels.py --triage` now flags any
heatmap carrying a `terms()` breakdown so this is caught at inventory time rather than at
save time.
