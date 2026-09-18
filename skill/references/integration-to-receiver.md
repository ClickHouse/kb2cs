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

## Status of this document

Every row below was read off the receiver's documentation or `metadata.yaml` on **2026-09-18**.
Rows are marked **[v]** verified or **[?]** not yet checked. Receivers move; re-read rather
than trust this.

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
| `innodb.buffer_pool.{read.requests,pool.reads}` | `mysql.buffer_pool.operations` + `operation` | **reshaped** |
| `open.{files,tables,streams}` | `mysql.opened_resources` + `kind` | *cumulative opened*, not current — `mysql.table.open` is the current gauge |
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

## system — `hostmetricsreceiver` **[v for cpu and process, ? for the rest]**

| Elastic | receiver | note |
|---|---|---|
| `system.cpu.{user,system,nice,irq,softirq,iowait}.norm.pct` | `system.cpu.utilization` + `cpu` + `state` | **6 → 1**, and **optional** — the default is `system.cpu.time` (cumulative **seconds**), so reproducing a percentage panel from a default config means a rate ÷ core count |
| — | state values | `idle, interrupt, nice, softirq, steal, system, user, wait` — note **`interrupt`** not `irq` and **`wait`** not `iowait` |
| `process.cpu.pct` | `process.cpu.utilization` (V0, **not** normalised) | optional |
| `system.process.cpu.total.norm.pct` | `process.cpu.utilization@v1` (**normalised by CPU count**, attribute `cpu.mode`) | optional — **the two differ by core count; picking the wrong one is a 4–16× error** |
| process identity | resource attrs `process.pid`, `process.executable.name`, `process.executable.path`, `process.command`, `process.owner` | |
| `system.{memory,load,network,diskio,filesystem}.*` | **[?] not yet checked** — each hostmetrics scraper has its own `documentation.md` | |

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
