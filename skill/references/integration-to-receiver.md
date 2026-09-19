# Elastic integration → OTel receiver

The mapping a dashboard migration actually needs. **Not** ECS → OTel semantic conventions:
when ingestion moves to the OTel collector, what decides your field names is the *receiver*,
and a receiver **reshapes** rather than renames.

Three things this answers that a name-level mapping table cannot:

1. **Reshaping.** Several source fields collapse into one metric carrying a dimension as an
   attribute. A panel plotting six fields becomes one metric filtered six ways — which also
   changes whether it is expressible as a builder tile.
2. **Default-off.** Much of what these dashboards need is an *optional* metric. "The receiver
   supports it" and "your collector is emitting it" are different statements.
3. **Absent.** Some panels have no target on the standard receiver. Usually the answer is a
   different receiver, not a lost panel.

> **Read the receiver's own `metadata.yaml` before declaring anything absent.** On the
> reference project this was got wrong twice in the same week — see "What this file corrects"
> at the end.

## Verdicts

The same six words are used throughout, and `scripts/receiver-map.json` carries them per
field so `plan-collector.py` can act on them:

| verdict | meaning |
|---|---|
| **default** | the receiver emits it with a default config — nothing to configure |
| **optional** | the receiver has it but it is OFF by default — enable it explicitly |
| **check** | a near equivalent exists but the semantics or the attribute value were not confirmed. **Currently empty** — all seven were resolved, five by reading a `metadata.yaml` and two by measurement (below) |
| **absent** | no equivalent on this receiver — see its alternative |
| **derive** | no metric needed; compute it in the tile |
| **dimension** | not a metric at all; it becomes an attribute |

**reshaped** is orthogonal and can apply to any of them: several source fields collapsing into
one metric that carries the distinction as an attribute.

## Status of this document

Every row below was read off the receiver's documentation or `metadata.yaml` on
**2026-09-18**, and as of that date **every row is verified** — the `[v]` markers say which
receiver was read. Keep the `[?]` convention for anything added later without checking, and
prefer re-reading to trusting this: receivers move, and one of them has a rename already
behind a feature gate.

---

## nginx — `nginxreceiver` **[v]**

| Elastic (`nginx.stubstatus.*`) | receiver | shape |
|---|---|---|
| `requests` | `nginx.requests` | Sum, cumulative, monotonic |
| `accepts` | `nginx.connections_accepted` | Sum, cumulative, monotonic |
| `handled` | `nginx.connections_handled` | Sum, cumulative, monotonic |
| `active`, `reading`, `writing`, `waiting` | `nginx.connections_current` + `state` | **4 fields → 1 metric**, Sum non-monotonic |
| `dropped` | — | derive: `accepted − handled` |

## apache — `apachereceiver` **[v]**

| Elastic (`apache.status.*`) | receiver | shape |
|---|---|---|
| `total_accesses` | `apache.requests` | Sum |
| `total_bytes` | `apache.traffic` | Sum |
| `uptime.server_uptime` | `apache.uptime` | Sum |
| `scoreboard.*` (11 fields) | `apache.scoreboard` + `scoreboard_state` | **11 → 1** |
| `workers.{busy,idle}` | `apache.workers` + `workers_state` | **2 → 1** |
| `connections.async.{writing,keep_alive,closing}` | `apache.connections.async` + `connection_state` | **3 → 1**, default-ON |
| `connections.total` | `apache.current_connections` | gauge |
| `cpu.{user,system,children_user,children_system}` | `apache.cpu.time` + `level` + `mode` | **4 → 1, two attributes** |
| `cpu.load` | `apache.cpu.load` | gauge |
| `load.{1,5,15}` | `apache.load.{1,5,15}` | gauge |
| `requests_per_sec`, `bytes_per_sec`, `bytes_per_request` | — | derive: counter ÷ `apache.uptime` |

**Deprecation in flight.** A feature gate (`receiver.apache.disableOldFormatMetrics` /
`enableNewFormatMetrics`) renames these to `apache.request.count`, `apache.worker.status`,
`apache.connection.status`, and moves the attributes to `apache.*.state`. Pin your
expectations to one format.

## mysql — `mysqlreceiver` **[v]**

| Elastic (`mysql.status.*`) | receiver | note |
|---|---|---|
| `threads.*` | `mysql.threads` + `kind` | default |
| `innodb.buffer_pool.pages.*` | `mysql.buffer_pool.pages` + `kind` | default |
| `innodb.buffer_pool.{read.requests,pool.reads}` | `mysql.buffer_pool.operations` + `operation`(`read_requests`, `reads`) | default, **reshaped** |
| `open.files` | `mysql.file.open` | optional. A **gauge** of currently open files |
| `open.{tables,streams}` | **absent** | `mysql.opened_resources` is a *monotonic cumulative* count of resources opened, not the current number open, and there is no gauge for tables or streams → `sqlqueryreceiver` on `SHOW GLOBAL STATUS LIKE 'Open_tables'` |
| `command.*` | `mysql.commands` + `command` | **optional** |
| `connections` | `mysql.connection.count` | optional |
| `connection.errors.*` | `mysql.connection.errors` + `error` | optional |
| `max_used_connections` | `mysql.max_used_connections` | optional |
| `cache.table.open_cache.*` | `mysql.table_open_cache` + `status` | optional |
| `bytes.{sent,received}` | `mysql.client.network.io` + `kind` | optional, **renamed** |
| `questions` | `mysql.query.count` | optional, **renamed** |
| `replica_status.thread.sql.delay.sec` | `mysql.replica.sql_delay` | optional |
| `replica_status.seconds_behind_source` | `mysql.replica.time_behind_source` | optional |
| per-query statistics | `mysql.statement_event.{count,wait.time}` + `digest`, `digest_text` | optional — **MySQL can do per-query; Postgres cannot** |
| `cache.ssl.*` | **absent** | `Ssl_*` status vars not exposed → `sqlqueryreceiver` |
| `aborted.{clients,connects}` | **absent** | `connection.errors` covers `Connection_errors_*`, not `Aborted_*` |
| `replica_status.source.log_position.*` | **absent** | binlog positions not exposed |

## postgresql — `postgresqlreceiver` **[v]**

| Elastic (`postgresql.*`) | receiver | verdict |
|---|---|---|
| `database.transactions.commit` | `postgresql.commits` | maps (default) |
| `database.transactions.rollback` | `postgresql.rollbacks` | maps (default) |
| `database.rows.{fetched,returned,inserted,updated,deleted}` | `postgresql.tup_*` (per database, `db.namespace`) | maps (**optional**) |
| `database.deadlocks` | `postgresql.deadlocks` | maps (optional) |
| `database.conflicts` | `postgresql.query.conflicts` | maps (optional) |
| `database.blocks.time.{read,write}.ms` | **absent** | `pg_stat_database.blk_*_time` not exposed → `sqlqueryreceiver` |
| `statement.query.calls` | **absent** | no per-statement metric → `sqlqueryreceiver` |
| `statement.query.time.total.ms` | `postgresql.query.execution.time` is **per database** | degrades — loses the per-query dimension |
| `statement.query.memory.{shared,local}.{hit,read}` | `postgresql.blks_hit` / `blks_read`, per database | degrades — loses the per-query dimension |

Attributes are semconv: `db.namespace`, `db.collection.name`. Durations are **seconds**.

**Per-tile verdict for the migrated dashboard** — of 9 data tiles: **4 map** (Database
Transactions, Rows Fetched/Returned, Rows Inserted/Deleted/Updated, Conflict/Deadlock Rates),
**2 degrade** to per-database (Local and Shared block cache stats), **3 need
`sqlqueryreceiver`** (Top Queries, Query Latency, Fileblock IO).

## system — `hostmetricsreceiver` **[v]**

Each scraper documents itself separately (`internal/scraper/<name>scraper/documentation.md`);
the rows below were read from the cpu, memory, load, network, disk, filesystem, process and
paging scrapers — all eight.

| Elastic | receiver | default? | shape |
|---|---|---|---|
| `system.cpu.{user,system,nice,irq,softirq,iowait}.norm.pct` | `system.cpu.utilization` + `cpu` + `state` | **optional** | **6 → 1.** Default is `system.cpu.time` (cumulative **seconds**) |
| — | its `state` values | | `idle, interrupt, nice, softirq, steal, system, user, wait` — **`interrupt`** not `irq`, **`wait`** not `iowait` |
| `system.load.{1,5,15}` | `system.cpu.load_average.{1m,5m,15m}` | default | renamed and re-parented |
| `system.memory.free` | `system.memory.usage` + `state=free` | default | **n → 1.** States: `buffered, cached, inactive, free, slab_reclaimable, slab_unreclaimable, used` |
| `system.memory.actual.used.bytes` | `system.memory.usage` + `state=used` | default | **measured, not inferred** — the receiver's `used` excludes buffers and cache, exactly like Elastic's *actual* used |
| `system.memory.used.bytes` | **derive** | | Elastic's `used.bytes` is `MemTotal − MemFree`, so it *includes* buffers and cache and matches no single state. Use `system.memory.limit − system.memory.usage{state=free}` (enable `limit`). **Do not** use `used+buffered+cached`: the receiver's `cached` includes `SReclaimable`, so that sum overshoots by roughly that much |
| `system.memory.actual.used.pct` | `system.memory.utilization` + `state` | **optional** | carries `state`, so a single percentage needs picking a state |
| `system.memory.total` | `system.memory.limit` | **optional** | |
| `system.network.{in,out}.bytes` | `system.network.io` + `device` + `direction` | default | **2 → 1**; direction is `receive`/`transmit` |
| `system.network.{in,out}.packets` | `system.network.packets` + `device` + `direction` | default | **2 → 1** |
| `system.network.{in,out}.dropped` | `system.network.dropped` + `device` + `direction` | default | **2 → 1** |
| `system.diskio.{read,write}.bytes` | `system.disk.io` + `device` + `direction` | default | **2 → 1**; direction is `read`/`write` |
| `system.filesystem.used.bytes` | `system.filesystem.usage` + `device` + `mode` + `mountpoint` + `type` + `state` | default | states `free, reserved, used` |
| `system.filesystem.used.pct` | `system.filesystem.utilization` (**no** `state`) | **optional** | |
| `process.cpu.pct` | `process.cpu.utilization` — **V0, not normalised** | optional | note: **no `system.` prefix** |
| `system.process.cpu.total.norm.pct` | `process.cpu.utilization@v1` — **normalised by CPU count**, attribute `cpu.mode` | optional | **the two differ by core count: a 4–16× error** |
| process identity | **resource** attributes `process.pid`, `process.executable.name`, `process.executable.path`, `process.command`, `process.owner` | | not data-point attributes |
| `system.memory.swap.*` / swap usage | `system.paging.usage` + `device` + `state`(cached/free/used) | default | `system.paging.utilization` is **optional**, same pattern |

> **The memory states, measured rather than assumed.** The receiver's metadata does not define
> what `used` includes, so this was settled by running `hostmetricsreceiver` 0.142.0 and reading
> `/proc/meminfo` in the same second, seven matched pairs:
>
> | receiver state | equals |
> |---|---|
> | `free` | `MemFree` exactly |
> | `buffered` | `Buffers` exactly |
> | `cached` | `Cached + SReclaimable` **exactly** |
> | `used` | 5.69 GB where `MemTotal − MemFree` was 8.07 GB — so it **excludes** buffers and cache (`MemTotal − MemFree − Buffers − Cached` matched to within 0.25%) |
>
> That is why `actual.used` maps straight onto `state=used`, and why `used.bytes` has to be
> derived from `limit − free` instead of summed from states.

> **Every `*.utilization` metric is optional; the default is the absolute counter.**
> `system.cpu.utilization`, `system.memory.utilization` and `system.filesystem.utilization` are
> all off by default — as is `system.paging.utilization` — while `system.cpu.time`,
> `system.memory.usage`, `system.filesystem.usage` and `system.paging.usage` are on. Elastic hands you percentages; OTel hands you absolutes and
> makes the percentages opt-in. **Any dashboard built on Elastic's `*.pct` fields needs either
> a collector config change or a rate/ratio computed in the tile** — and that is a planning
> decision, not a translation detail.

## The escape hatch — `sqlqueryreceiver` **[v]**

Turns arbitrary SQL into metrics: "one OTel metric per row returned". `value_column` supplies
the value, `attribute_columns` turn other columns into attributes — so a per-query dimension
works. `data_type: gauge|sum`, `monotonic`, `aggregation: cumulative|delta` are all settable.
Drivers include postgres, mysql, sqlserver, oracle, snowflake, clickhouse.

**Metrics support is `alpha`** ("behavior, configuration fields, and data model are subject to
change"). So "achievable" is not "recommended for production" — say both.

Because metric names here are whatever the operator's SELECT aliased, a `sqlqueryreceiver`
target has *no canonical names to map to*. Two consequences: the mapping becomes near-identity
(the target's names are the source's column names), and the naming is a decision you own.
**Alias your database column to `db.namespace`**, not `database`, so one dashboard control can
filter both these metrics and any `postgresqlreceiver` ones.

## Where this repo's reference stack deviates

The reference stack's loader stands in for a receiver. For nginx and apache it reproduces the
receiver's model closely; elsewhere it does not, and the gaps are listed here rather than left
for someone to discover by copying a tile.

| integration | deviation | why it matters |
|---|---|---|
| **apache** | attribute key `state` where the receiver uses `scoreboard_state` / `workers_state` | a tile copied from here filters on the wrong key. The newer `apache.connections.async` uses the correct `connection_state`, so apache currently carries **two conventions** |
| **mysql** | `mysql.open_resources` → for files the receiver has `mysql.file.open`, a gauge; for tables and streams it has **nothing current** (`mysql.opened_resources` is a monotonic cumulative count of opens) | wrong name **and** wrong semantics |
| **mysql** | `mysql.traffic` + `direction` → receiver is `mysql.client.network.io` + `kind` | name and attribute both differ |
| **mysql** | `mysql.questions` → receiver is `mysql.query.count` | renamed |
| **mysql** | `mysql.buffer_pool.{read_requests,reads}` → `mysql.buffer_pool.operations` + `operation`(`read_requests`/`reads`) | two metrics become one + a dimension |
| **mysql** | `mysql.ssl_cache`, `mysql.aborted`, `mysql.replica.log_position.*` | no receiver equivalent → `sqlqueryreceiver` |
| **system** | `system.process.*` → receiver emits `process.*` (**no `system.` prefix**) | wrong namespace |
| **system** | `system.process.cpu.pct` | not a receiver metric; see the V0/V1 normalisation trap above |
| **system** | `process.name` as a data-point attribute → receiver uses **resource** attribute `process.executable.name` | different key *and* different location |
| **system** | `state` values `irq` / `iowait` → receiver uses `interrupt` / `wait` | a tile filtering `state='iowait'` returns nothing |
| **system** | `state: actual_used` on `system.memory.usage` | not in the receiver's state vocabulary |
| **postgresql** | metric names are the source's column names | **deliberate, not a defect** — this models `sqlqueryreceiver`, whose names the operator chooses. See that section |

Nothing above affects the migration's *correctness against Elasticsearch* — the tiles and the
expectations agree, and 203 tile series are diffed bucket-for-bucket. It affects **portability**:
a customer's collector will not emit these names, so the value of this repo's postgres, mysql
and system tile SQL is the *technique*, not the identifiers.

## What this file corrects

Two claims in this repo were wrong, both from assuming a receiver's capability instead of
reading its metadata — which is the same failure the verification chapters warn about, applied
to the migration's own planning:

- **apache async connections** were recorded as "not migratable — the standard apachereceiver
  emits no async-connection metric". It emits `apache.connections.async` **by default**, the
  corpus had the data all along, and the panel is now migrated.
- **postgres per-statement metrics** were then recorded the same way. True of
  `postgresqlreceiver`, but `sqlqueryreceiver` scrapes `pg_stat_statements` directly, so the
  answer was a second receiver rather than a lost panel.

What survives as a genuine residue class is narrower and more useful: **the metric is not in
the collector's default set** — optional and switched off, or needing a receiver nobody
configured. Both are answered by collector configuration, not by abandoning a panel.
