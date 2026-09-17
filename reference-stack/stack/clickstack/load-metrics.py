#!/usr/bin/env python3
"""
Ship the nginx stub_status series into ClickStack as OTel **metrics**.

    export SHIFT_NS=$(../../ingest/clickstack/shift-ns.sh --align-hour)
    python3 load-metrics.py

Posts OTLP/JSON straight to the collector's :4318/v1/metrics. No collector config and no
compose service: the logs need a filelog receiver to tail files, metrics do not -- the
payload is already structured, so the shortest correct path is to be the client.

Two things to know about the endpoint, both verified here rather than assumed:
  * `authorization` takes the **bare ingestion key**, no `Bearer` prefix (that is the MCP
    endpoint's scheme, and sending it here returns 401 "does not match expected scheme").
  * a Sum needs `aggregationTemporality: 2` (CUMULATIVE) and `isMonotonic: true` to land as
    a counter; they arrive in `otel_metrics_sum`, gauges in `otel_metrics_gauge`.

## The naming reshape, which is the actual migration work

The Elastic integration and the OTel nginx receiver model stub_status *differently*, and this
loader deliberately emits the **OTel receiver's** model rather than Elastic's field names. A
customer running standard OTel instrumentation gets this shape, so translating onto it is the
realistic exercise; an identity mapping would prove nothing.

| Elastic (`metrics-nginx.stubstatus`) | here (OTel) |
|---|---|
| `nginx.stubstatus.requests`  counter | `nginx.requests` Sum |
| `nginx.stubstatus.accepts`   counter | `nginx.connections_accepted` Sum |
| `nginx.stubstatus.handled`   counter | `nginx.connections_handled` Sum |
| `nginx.stubstatus.dropped`   counter | **no metric** -- accepted minus handled |
| `nginx.stubstatus.active`    gauge   | `nginx.connections_current{state="active"}` |
| `nginx.stubstatus.reading`   gauge   | `nginx.connections_current{state="reading"}` |
| `nginx.stubstatus.writing`   gauge   | `nginx.connections_current{state="writing"}` |
| `nginx.stubstatus.waiting`   gauge   | `nginx.connections_current{state="waiting"}` |
| `nginx.stubstatus.current`   gauge   | **no metric** -- it duplicates `requests` |
| `host.hostname`  dimension           | resource attribute `host.name` |

Note the two structural consequences, which no field-mapping table would have surfaced:

  * **Four gauge fields collapse into one metric with a `state` attribute.** The Kibana panel
    that charts reading/writing/waiting as three series becomes ONE select item grouped by
    `Attributes['state']` -- fewer select items, not more.
  * **`dropped` stops being a metric.** It was only ever accepts-handled, and the OTel
    receiver does not emit it, so the "Drops Rate" panel cannot be a builder tile: it needs
    the difference of two counters' increases, which no `aggFn` expresses.
"""

import argparse
import calendar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

OTLP = "http://localhost:4318/v1/metrics"
BUILD = None
SERVICE_NAME = "nginx"
SECRETS_VOLUME = "nginx-training-clickstack_secrets"
CH = ["docker", "compose", "exec", "-T", "clickstack", "clickhouse-client", "--query"]

NGINX_SUMS = (("requests", "nginx.requests"),
              ("accepts", "nginx.connections_accepted"),
              ("handled", "nginx.connections_handled"))
NGINX_STATES = ("active", "reading", "writing", "waiting")

# --- apache: the standard apachereceiver's model ---------------------------------------
# Deliberately NOT Elastic's field names, and deliberately NOT everything Elastic has:
#
#   requests_per_sec / bytes_per_sec / bytes_per_request
#       mod_status computes these (counter / uptime) and the OTel receiver does not emit
#       them. Derive them on the target from apache.requests and apache.uptime.
#   connections.async.{writing,keep_alive,closing}
#       no standard metric exists at all. That leaves one panel with no source on the
#       target -- a gap in COLLECTION, not in chart types, which is a residue class the
#       logs migrations never produced.
APACHE_SUMS = (("uptime", "apache.uptime"),
               ("total_accesses", "apache.requests"),
               ("total_bytes", "apache.traffic"))
APACHE_SCOREBOARD = (("open_slot", "open"), ("waiting_for_connection", "waiting"),
                     ("starting_up", "starting"), ("reading_request", "reading"),
                     ("sending_reply", "sending"), ("keepalive", "keepalive"),
                     ("dns_lookup", "dnslookup"), ("closing_connection", "closing"),
                     ("logging", "logging"), ("gracefully_finishing", "finishing"),
                     ("idle_cleanup", "idle_cleanup"))
# Elastic's five cpu fields become ONE metric with two attributes.
APACHE_CPU = ((("cpu_user", "self", "user"), ("cpu_system", "self", "system"),
               ("cpu_children_user", "children", "user"),
               ("cpu_children_system", "children", "system")))


def ingestion_key():
    key = os.environ.get("CLICKSTACK_API_KEY")
    if key:
        return key.strip()
    # setup.sh writes it into a named volume; the collector image has no shell to echo it.
    out = subprocess.run(["docker", "run", "--rm", "-v", "%s:/s" % SECRETS_VOLUME,
                          "alpine", "cat", "/s/api-key"],
                         capture_output=True, text=True)
    key = (out.stdout or "").strip()
    if not key:
        sys.exit("could not read the ingestion key from volume %s; is the stack up?"
                 % SECRETS_VOLUME)
    return key


def ch(query):
    out = subprocess.run(CH + [query], capture_output=True, text=True)
    return (out.stdout or "").strip()


def post(key, payload, tries=6):
    body = json.dumps(payload).encode()
    for attempt in range(tries):
        req = urllib.request.Request(OTLP, data=body, headers={
            "Content-Type": "application/json", "authorization": key})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            if e.code in (429, 503) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            sys.exit("OTLP POST failed: HTTP %s %s" % (e.code, detail))
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            sys.exit("could not reach %s: %s" % (OTLP, e.reason))


def datapoint(ns, value, attrs=None, start_ns=None):
    dp = {"asDouble": float(value), "timeUnixNano": str(ns)}
    if start_ns is not None:
        dp["startTimeUnixNano"] = str(start_ns)
    if attrs:
        dp["attributes"] = [{"key": k, "value": {"stringValue": v}}
                            for k, v in attrs.items()]
    return dp


def main():
    global BUILD, SERVICE_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", choices=sorted(SERVICES), default="nginx",
                    help="which service's metric series to ship (default nginx)")
    ap.add_argument("--src", default=None)
    ap.add_argument("--batch", type=int, default=400, help="scrapes per POST")
    ap.add_argument("--append", action="store_true",
                    help="add to whatever is already there instead of refusing")
    ap.add_argument("--part", default=None,
                    help="for a multi-stream service, load only the part whose source file "
                         "name contains this substring (e.g. --part replica). Reloading one "
                         "stream needs --append, since the others' points are still there")
    args = ap.parse_args()

    svc = SERVICES[args.service]
    SERVICE_NAME = args.service
    here = os.path.dirname(os.path.abspath(__file__))
    parts = svc.get("parts") or [svc]
    if args.part:
        parts = [p for p in parts if args.part in p["src"]]
        if not parts:
            sys.exit("--part %r matches none of: %s"
                     % (args.part, ", ".join(p["src"] for p in (svc.get("parts") or [svc]))))
        print("[load] restricted to %s" % ", ".join(p["src"] for p in parts))
    for part in parts:
        part["path"] = os.path.abspath(args.src or os.path.join(
            here, "..", "..", "data", "metrics", part["src"]))
        if not os.path.exists(part["path"]):
            sys.exit("missing %s -- run generator/generate-%s*.py"
                     % (part["path"], args.service))

    shift_ns = int(os.environ.get("SHIFT_NS", "0"))
    if shift_ns == 0:
        print("[load] NOTE: SHIFT_NS is unset, so the series stays on its original "
              "2026-08-17 dates and will not line up with the logs. For a comparable load:")
        print("[load]   export SHIFT_NS=$(../../ingest/clickstack/shift-ns.sh --align-hour)")

    like = "%s.%%" % ("postgresql" if args.service == "postgresql" else args.service)
    before = int(ch("SELECT count() FROM default.otel_metrics_sum WHERE MetricName LIKE '%s'"
                    % like) or 0)
    before_gauge = int(ch("SELECT count() FROM default.otel_metrics_gauge "
                          "WHERE MetricName LIKE '%s'" % like) or 0)
    print("[load] otel_metrics_sum holds %d %s points" % (before, args.service))
    if before and not args.append:
        print("[load] refusing to append -- that would double every counter series.")
        print("[load] to reload:")
        print("[load]   docker compose exec clickstack clickhouse-client --query \\")
        print("[load]     \"ALTER TABLE default.otel_metrics_sum DELETE WHERE MetricName LIKE '%s'\"" % like)
        print("[load]   docker compose exec clickstack clickhouse-client --query \\")
        print("[load]     \"ALTER TABLE default.otel_metrics_gauge DELETE WHERE MetricName LIKE '%s'\"" % like)
        sys.exit(1)

    key = ingestion_key()
    print("[load] posting OTLP/JSON to %s" % OTLP)

    points = 0
    for part in parts:
        globals()['BUILD'] = part["build"]
        per_node = {}
        batches = 0
        with open(part["path"], encoding="utf-8") as fh:
            pending = []
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                epoch = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S.000Z"))
                ns = epoch * 10**9 + shift_ns
                pending.append((rec, ns))
                if len(pending) >= args.batch:
                    points += flush(key, pending, per_node)
                    batches += 1
                    pending = []
                    print("\r[load]   %s points" % "{:,}".format(points), end="", flush=True)
            if pending:
                points += flush(key, pending, per_node)
                batches += 1
        print("\r[load]   %s: %s points in %d batches" % (
            part["src"], "{:,}".format(points), batches))

    print("[load] waiting for the write to settle")
    n = g = 0
    last = -1
    for _ in range(40):
        time.sleep(3)
        n = int(ch("SELECT count() FROM default.otel_metrics_sum WHERE MetricName LIKE '%s'" % like) or 0)
        g = int(ch("SELECT count() FROM default.otel_metrics_gauge WHERE MetricName LIKE '%s'" % like) or 0)
        if n + g == last and n + g > 0:
            break
        last = n + g
    print("[load] otel_metrics_sum: %d   otel_metrics_gauge: %d" % (n, g))
    # Compare the DELTA, not the totals. With --append (or --part, which requires it) the
    # tables already hold this service's other streams, and checking the total against what
    # this run posted reports a spurious mismatch on a load that was perfectly fine.
    landed = (n - before) + (g - before_gauge)
    if landed == points:
        print("[load] done: %d new points (%d sum + %d gauge in total), exactly as posted"
              % (landed, n, g))
    else:
        print("[load] WARNING: %d new points landed, %d posted (totals now %d sum + %d gauge)"
              % (landed, points, n, g))
        print("[load] OTLP delivery is at-least-once and these tables have no dedup, so a "
              "retry can duplicate a batch. Delete the points and reload.")
        sys.exit(1)


def flush(key, pending, per_node):
    """One POST carrying every node present in this slice of scrapes."""
    by_node = {}
    for rec, ns in pending:
        # nginx/apache call the host `node`; the postgres series calls it `host`. One
        # resource per host either way.
        host = rec.get("node") or rec.get("host")
        by_node.setdefault(host, []).append((rec, ns))

    resource_metrics = []
    written = 0
    for node, rows in by_node.items():
        metrics, n = BUILD(rows)
        written += n
        per_node[node] = per_node.get(node, 0) + len(rows)
        resource_metrics.append({
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": SERVICE_NAME}},
                {"key": "host.name", "value": {"stringValue": node}},
                {"key": "deployment.environment", "value": {"stringValue": "training"}}]},
            "scopeMetrics": [{"scope": {"name": "%s-backfill" % SERVICE_NAME},
                              "metrics": metrics}]})
    post(key, {"resourceMetrics": resource_metrics})
    return written




# --- postgres ---------------------------------------------------------------------------
# A DIFFERENT modelling choice from apache, and deliberately so.
#
# For apache this loader emits the standard apachereceiver's metric names, because that is
# what a customer scraping apache into ClickStack would have. Postgres is not like that: the
# OTel `postgresqlreceiver` does not model pg_stat_statements at all, and covers
# pg_stat_database only partially (`postgresql.operations`, `postgresql.rows` and friends do
# not include rows.fetched/returned). Mapping onto it would leave most of this dashboard with
# no target -- a finding already demonstrated by apache, and not worth demonstrating twice.
#
# A customer shipping these numbers today most likely uses the collector's generic
# `sqlqueryreceiver`, which emits whatever the operator's SELECT aliased the columns to. So
# the realistic third shape is: **the target's metric names are the source's column names**.
# That is what this emits, and it is the reason the postgres field mapping is near-identity
# while apache's was a reshape.
PG_DB_SUMS = (("rows_fetched", "postgresql.database.rows.fetched"),
              ("rows_returned", "postgresql.database.rows.returned"),
              ("rows_inserted", "postgresql.database.rows.inserted"),
              ("rows_updated", "postgresql.database.rows.updated"),
              ("rows_deleted", "postgresql.database.rows.deleted"),
              ("xact_commit", "postgresql.database.transactions.commit"),
              ("xact_rollback", "postgresql.database.transactions.rollback"),
              ("blk_read_time_ms", "postgresql.database.blocks.time.read.ms"),
              ("blk_write_time_ms", "postgresql.database.blocks.time.write.ms"),
              ("conflicts", "postgresql.database.conflicts"),
              ("deadlocks", "postgresql.database.deadlocks"))

PG_ST_SUMS = (("calls", "postgresql.statement.query.calls"),
              ("time_total_ms", "postgresql.statement.query.time.total.ms"),
              ("rows", "postgresql.statement.query.rows"),
              ("mem_local_read", "postgresql.statement.query.memory.local.read"),
              ("mem_local_hit", "postgresql.statement.query.memory.local.hit"),
              ("mem_shared_read", "postgresql.statement.query.memory.shared.read"),
              ("mem_shared_hit", "postgresql.statement.query.memory.shared.hit"))


def build_pg_database(rows):
    """pg_stat_database. `database` is an ATTRIBUTE, not a resource: one postgres instance
    serves many databases, so they share a host and differ per data point."""
    metrics, written = [], 0
    start = rows[0][1]
    for field, name in PG_DB_SUMS:
        metrics.append({"name": name, "unit": "1", "sum": {
            "aggregationTemporality": 2, "isMonotonic": True,
            "dataPoints": [datapoint(ns, r[field], attrs={"database": r["database"]},
                                     start_ns=start) for r, ns in rows]}})
        written += len(rows)
    return metrics, written


def build_pg_statement(rows):
    """pg_stat_statements: one series per (database, normalised query).

    `query_text` rides as an attribute, which makes it a high-cardinality dimension on the
    target -- 16 here, but a real pg_stat_statements has thousands. That is the thing to warn
    a customer about before they point a `terms` panel at it.
    """
    metrics, written = [], 0
    start = rows[0][1]
    for field, name in PG_ST_SUMS:
        metrics.append({"name": name, "unit": "1", "sum": {
            "aggregationTemporality": 2, "isMonotonic": True,
            "dataPoints": [datapoint(ns, r[field],
                                     attrs={"database": r["database"],
                                            "query_text": r["query"],
                                            "query_id": str(r["query_id"])},
                                     start_ns=start) for r, ns in rows]}})
        written += len(rows)
    return metrics, written


def build_nginx(rows):
    """Three cumulative Sums plus one gauge carrying four connection states."""
    metrics, written = [], 0
    for field, name in NGINX_SUMS:
        metrics.append({"name": name, "unit": "1", "sum": {
            "aggregationTemporality": 2, "isMonotonic": True,
            "dataPoints": [datapoint(ns, r[field], start_ns=rows[0][1]) for r, ns in rows]}})
        written += len(rows)
    current = []
    for state in NGINX_STATES:
        current.extend(datapoint(ns, r[state], attrs={"state": state}) for r, ns in rows)
    metrics.append({"name": "nginx.connections_current", "unit": "1",
                    "gauge": {"dataPoints": current}})
    written += len(rows) * len(NGINX_STATES)
    return metrics, written


def build_apache(rows):
    """mod_status as the OTel apachereceiver models it: attribute-carrying metrics, not
    one field per state. Eleven scoreboard fields become one metric; five cpu fields
    become one metric with `level` and `mode`."""
    metrics, written = [], 0
    start = rows[0][1]
    for field, name in APACHE_SUMS:
        unit = "By" if name == "apache.traffic" else ("s" if name == "apache.uptime" else "1")
        metrics.append({"name": name, "unit": unit, "sum": {
            "aggregationTemporality": 2, "isMonotonic": True,
            "dataPoints": [datapoint(ns, r[field], start_ns=start) for r, ns in rows]}})
        written += len(rows)

    metrics.append({"name": "apache.current_connections", "unit": "1", "gauge": {
        "dataPoints": [datapoint(ns, r["connections_total"]) for r, ns in rows]}})
    written += len(rows)

    workers = []
    for field, state in (("workers_busy", "busy"), ("workers_idle", "idle")):
        workers.extend(datapoint(ns, r[field], attrs={"state": state}) for r, ns in rows)
    metrics.append({"name": "apache.workers", "unit": "1", "gauge": {"dataPoints": workers}})
    written += len(rows) * 2

    sb = []
    for efield, state in APACHE_SCOREBOARD:
        sb.extend(datapoint(ns, r["scoreboard"][efield], attrs={"state": state})
                  for r, ns in rows)
    metrics.append({"name": "apache.scoreboard", "unit": "1", "gauge": {"dataPoints": sb}})
    written += len(rows) * len(APACHE_SCOREBOARD)

    cpu = []
    for field, level, mode in APACHE_CPU:
        cpu.extend(datapoint(ns, r[field], attrs={"level": level, "mode": mode},
                             start_ns=start) for r, ns in rows)
    metrics.append({"name": "apache.cpu.time", "unit": "s", "sum": {
        "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": cpu}})
    written += len(rows) * len(APACHE_CPU)

    metrics.append({"name": "apache.cpu.load", "unit": "%", "gauge": {
        "dataPoints": [datapoint(ns, r["cpu_load"]) for r, ns in rows]}})
    written += len(rows)
    for field, name in (("load_1", "apache.load.1"), ("load_5", "apache.load.5"),
                        ("load_15", "apache.load.15")):
        metrics.append({"name": name, "unit": "%", "gauge": {
            "dataPoints": [datapoint(ns, r[field]) for r, ns in rows]}})
        written += len(rows)
    return metrics, written


# --- mysql -------------------------------------------------------------------------------
# A THIRD modelling choice, and the reason mysql is worth having alongside the other two.
#
# apache reshapes onto the real apachereceiver (attribute-carrying metrics); postgres is
# near-identity (sqlqueryreceiver, source column names). mysql shows the reshape at its most
# aggressive: the OTel mysqlreceiver collapses whole families of SHOW GLOBAL STATUS variables
# into ONE metric with a discriminating attribute. So a Kibana panel naming
# `mysql.status.command.select` has to become `mysql.commands` filtered to
# `command="select"` -- a field that becomes a (metric, attribute) PAIR. That is a different
# translation problem from renaming a field, and it is the one that breaks naive mapping
# tables.
#
# THE SUM-VS-GAUGE SPLIT BELOW IS DRIVEN BY THE PANELS, NOT BY ELASTIC'S METRIC TYPE.
#
# This is the trap, and mysql is where it bites hardest. HyperDX de-cumulates every Sum: any
# aggFn over a Sum operates on the per-bucket INCREASE, never the raw counter. So the
# question for each field is not "is it a counter?" but "does the source panel read its
# absolute value or its difference?":
#
#   panel does differences()/rate  -> emit a Sum    (de-cumulation IS the differences())
#   panel does max()/last_value()  -> emit a Gauge  (a Sum would return the increase)
#
# Five fields here are typed `counter` by the integration and still have to be GAUGES,
# because their panels read them raw:
#
#   max_used_connections              panel 3  max()          a high-water mark, not a rate
#   innodb.buffer_pool.pool.reads     panel 12 max()          lifetime total, half a ratio
#   innodb.buffer_pool.read.requests  panel 12 max()          the other half (Elastic: gauge)
#   replica source.log_position.read  panel 3  last_value()   a binlog OFFSET
#   replica source.log_position.exec  panel 3  last_value()   ditto
#
# and one that looks like it should be a gauge has to be a SUM:
#
#   cache.ssl.size                    panel 16 differences()  constant 128, so Kibana plots a
#                                                             flat zero; a gauge would plot
#                                                             128 and silently disagree.
#
# Every one of those was read off the panel's own formula, not guessed from the field name.
# (record field, attribute value) for the families the receiver collapses into one metric
MYSQL_COMMANDS = (("command_select", "select"), ("command_insert", "insert"),
                  ("command_update", "update"), ("command_delete", "delete"))
MYSQL_CONN_ERRORS = (("ce_select", "select"), ("ce_peer_address", "peer_address"),
                     ("ce_internal", "internal"), ("ce_max", "max_connections"),
                     ("ce_accept", "accept"), ("ce_tcpwrap", "tcpwrap"))
MYSQL_ABORTED = (("aborted_clients", "clients"), ("aborted_connects", "connects"))
MYSQL_TRAFFIC = (("bytes_sent", "sent"), ("bytes_received", "received"))
MYSQL_OPEN_CACHE = (("oc_hits", "hit"), ("oc_misses", "miss"), ("oc_overflows", "overflow"))
# `size` rides with hits/misses because panel 16 differences all three together
MYSQL_SSL_CACHE = (("ssl_hits", "hit"), ("ssl_misses", "miss"), ("ssl_size", "size"))
MYSQL_THREADS = (("threads_connected", "connected"), ("threads_running", "running"),
                 ("threads_cached", "cached"), ("threads_created", "created"))
MYSQL_OPEN = (("open_files", "files"), ("open_tables", "tables"),
              ("open_streams", "streams"))
MYSQL_BP_PAGES = (("bp_pages_total", "total"), ("bp_pages_free", "free"),
                  ("bp_pages_data", "data"), ("bp_pages_dirty", "dirty"))
# the five counter-typed-but-must-be-gauge fields, plus read.requests which Elastic
# already types gauge
MYSQL_GAUGES = (("max_used_connections", "mysql.max_used_connections"),
                ("bp_pool_reads", "mysql.buffer_pool.reads"),
                ("bp_read_requests", "mysql.buffer_pool.read_requests"))

MYSQL_STATUS_SERIES = (2 + len(MYSQL_COMMANDS) + len(MYSQL_CONN_ERRORS)
                       + len(MYSQL_ABORTED) + len(MYSQL_TRAFFIC)
                       + len(MYSQL_OPEN_CACHE) + len(MYSQL_SSL_CACHE)
                       + len(MYSQL_THREADS) + len(MYSQL_OPEN)
                       + len(MYSQL_BP_PAGES) + len(MYSQL_GAUGES))


def build_mysql_status(rows):
    """SHOW GLOBAL STATUS, reshaped the way the mysqlreceiver models it."""
    metrics, written = [], 0
    start = rows[0][1]

    def add_sum(name, unit, points):
        metrics.append({"name": name, "unit": unit, "sum": {
            "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": points}})

    def add_gauge(name, unit, points):
        metrics.append({"name": name, "unit": unit, "gauge": {"dataPoints": points}})

    # plain cumulative counters, one series each
    for field, name in (("questions", "mysql.questions"),
                        ("connections", "mysql.connection.count")):
        add_sum(name, "1", [datapoint(ns, r[field], start_ns=start) for r, ns in rows])
        written += len(rows)

    # the collapsed families: one metric, one attribute per source field
    for family, attr, name, unit in (
            (MYSQL_COMMANDS, "command", "mysql.commands", "1"),
            (MYSQL_CONN_ERRORS, "error", "mysql.connection.errors", "1"),
            (MYSQL_ABORTED, "kind", "mysql.aborted", "1"),
            (MYSQL_TRAFFIC, "direction", "mysql.traffic", "By"),
            (MYSQL_OPEN_CACHE, "status", "mysql.table_open_cache", "1"),
            (MYSQL_SSL_CACHE, "status", "mysql.ssl_cache", "1")):
        pts = []
        for field, value in family:
            pts.extend(datapoint(ns, r[field], attrs={attr: value}, start_ns=start)
                       for r, ns in rows)
        add_sum(name, unit, pts)
        written += len(rows) * len(family)

    # gauges, likewise collapsed
    for family, attr, name in ((MYSQL_THREADS, "kind", "mysql.threads"),
                               (MYSQL_OPEN, "kind", "mysql.open_resources"),
                               (MYSQL_BP_PAGES, "kind", "mysql.buffer_pool.pages")):
        pts = []
        for field, value in family:
            pts.extend(datapoint(ns, r[field], attrs={attr: value}) for r, ns in rows)
        add_gauge(name, "1", pts)
        written += len(rows) * len(family)

    # the counter-typed fields whose panels read them raw -- see the header comment
    for field, name in MYSQL_GAUGES:
        add_gauge(name, "1", [datapoint(ns, r[field]) for r, ns in rows])
        written += len(rows)

    return metrics, written


MYSQL_REPLICA_SERIES = 4


def build_mysql_replica(rows):
    """SHOW REPLICA STATUS.

    All four series are gauges. The two lag figures are gauges in the integration too; the
    two binlog positions are typed `counter` there and are gauges here ON PURPOSE, because
    the Replica Status panels read them with last_value() -- an offset into a binary log,
    not a rate. Emitted as Sums, HyperDX would de-cumulate them and the panel would plot
    bytes-per-bucket while Kibana plots the position. See the header comment.

    The source's identity (host, port, server id/uuid, binlog file, replication user) is
    carried as data-point ATTRIBUTES: they are the columns the `Source overview` panel
    selects, and they are CONSTANT, so all 2,880 points of a metric share one attribute set
    and therefore one series.

    That constancy is load-bearing. `source.file_info` is "<binlog file> <byte position>",
    so it changes on every scrape -- attaching it here gave each point its own attribute set
    and produced 2,879 distinct series for one replica instead of 1. Nothing errors: the
    series just fragment, and because HyperDX aggregates a gauge ACROSS series after
    collapsing time by last-value, every tile then reads an arbitrary point per bucket. It
    is reconstructed in the Source overview tile's SQL instead, exactly as the generator
    builds it -- concat(binary_log_file, ' ', read position).

    ONE ATTRIBUTE THAT MOVES WITH THE VALUE IS ENOUGH TO DESTROY SERIES IDENTITY.
    """
    metrics, written = [], 0
    ident = {}
    r0 = rows[0][0]
    for k, v in (("source_host", "source.host.name"), ("source_port", "source.port"),
                 ("source_server_id", "source.server.id"),
                 ("source_uuid", "source.server.uuid"),
                 ("binlog_file", "source.binary_log_file"), ("user", "user.name")):
        ident[v] = str(r0[k])

    for field, name in (("seconds_behind_source", "mysql.replica.time_behind_source"),
                        ("sql_delay_sec", "mysql.replica.sql_delay"),
                        ("read_pos", "mysql.replica.log_position.read"),
                        ("exec_pos", "mysql.replica.log_position.exec")):
        pts = [datapoint(ns, r[field], attrs=ident) for r, ns in rows]
        metrics.append({"name": name, "unit": "s" if "delay" in name or "behind" in name
                        else "By", "gauge": {"dataPoints": pts}})
        written += len(rows)
    return metrics, written


# --- system ------------------------------------------------------------------------------
# A FOURTH modelling choice, and the one a customer is most likely to actually have: the OTel
# **hostmetricsreceiver**, which is the standard way host metrics reach ClickStack. Its model
# is attribute-heavy like the apachereceiver's, so this is another field -> (metric,
# attribute) reshape:
#
#   system.cpu.{user,system,nice,irq,softirq,iowait}.norm.pct
#       -> system.cpu.utilization{state=...}                      7 fields -> 1 metric
#   system.network.{in,out}.{bytes,packets,dropped}
#       -> system.network.{io,packets,dropped}{device,direction}  6 fields -> 3 metrics
#   system.filesystem.used.pct  -> system.filesystem.utilization{device,mountpoint}
#
# TWO FIELDS THE RECEIVER HAS NO EQUIVALENT FOR, both derived on the target instead:
#
#   system.cpu.total.norm.pct    hostmetrics has no `total` state. It is the sum of the six
#                                non-idle states, which is exact here because the generator
#                                builds total that way -- so the two CPU gauge tiles sum the
#                                six states rather than reading a field.
#   system.fsstat.total_size.*   fsstat is a Beats rollup with no receiver counterpart. It is
#                                the sum of the per-mount filesystem numbers, asserted equal
#                                on the Elastic side, so the Disk Used tiles sum
#                                system.filesystem.usage over mountpoints.
#
# Counters vs gauges follows the integration's mapping: network and disk are COUNTERS (their
# panels wrap them in counter_rate()), everything else is a GAUGE.
SYS_CPU_STATES = ("user", "system", "nice", "irq", "softirq", "iowait", "idle")
SYS_PROCS_PER_HOST = 6


def build_system_cpu(rows):
    metrics, written = [], 0
    pts = []
    for state in SYS_CPU_STATES:
        pts.extend(datapoint(ns, r[state], attrs={"state": state}) for r, ns in rows)
    metrics.append({"name": "system.cpu.utilization", "unit": "1",
                    "gauge": {"dataPoints": pts}})
    written += len(rows) * len(SYS_CPU_STATES)
    metrics.append({"name": "system.cpu.logical.count", "unit": "{cpu}", "gauge": {
        "dataPoints": [datapoint(ns, r["cores"]) for r, ns in rows]}})
    written += len(rows)
    return metrics, written


def build_system_load(rows):
    metrics, written = [], 0
    for field, name in (("load1", "system.cpu.load_average.1m"),
                        ("load5", "system.cpu.load_average.5m"),
                        ("load15", "system.cpu.load_average.15m")):
        metrics.append({"name": name, "unit": "1", "gauge": {
            "dataPoints": [datapoint(ns, r[field]) for r, ns in rows]}})
        written += len(rows)
    return metrics, written


def build_system_memory(rows):
    metrics, written = [], 0
    usage = []
    for field, state in (("used", "used"), ("free", "free"), ("actual_used", "actual_used")):
        usage.extend(datapoint(ns, r[field], attrs={"state": state}) for r, ns in rows)
    metrics.append({"name": "system.memory.usage", "unit": "By",
                    "gauge": {"dataPoints": usage}})
    written += len(rows) * 3
    metrics.append({"name": "system.memory.utilization", "unit": "1", "gauge": {
        "dataPoints": [datapoint(ns, r["actual_used_pct"], attrs={"state": "actual_used"})
                       for r, ns in rows]}})
    written += len(rows)
    metrics.append({"name": "system.memory.limit", "unit": "By", "gauge": {
        "dataPoints": [datapoint(ns, r["total"]) for r, ns in rows]}})
    written += len(rows)
    metrics.append({"name": "system.paging.utilization", "unit": "1", "gauge": {
        "dataPoints": [datapoint(ns, r["swap_used_pct"], attrs={"state": "used"})
                       for r, ns in rows]}})
    written += len(rows)
    return metrics, written


def build_system_network(rows):
    """CUMULATIVE Sums. One data point per (interface, direction) per scrape -- `rows` holds
    every interface for this host in the batch, so each row contributes its own series."""
    metrics, written = [], 0
    start = rows[0][1]
    for name, unit, fields in (
            ("system.network.io", "By", (("in_bytes", "receive"), ("out_bytes", "transmit"))),
            ("system.network.packets", "{packets}",
             (("in_packets", "receive"), ("out_packets", "transmit"))),
            ("system.network.dropped", "{packets}",
             (("in_dropped", "receive"), ("out_dropped", "transmit")))):
        pts = []
        for field, direction in fields:
            pts.extend(datapoint(ns, r[field],
                                 attrs={"device": r["name"], "direction": direction},
                                 start_ns=start) for r, ns in rows)
        metrics.append({"name": name, "unit": unit, "sum": {
            "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": pts}})
        written += len(rows) * len(fields)
    return metrics, written


def build_system_diskio(rows):
    metrics, written = [], 0
    start = rows[0][1]
    pts = []
    for field, direction in (("read_bytes", "read"), ("write_bytes", "write")):
        pts.extend(datapoint(ns, r[field],
                             attrs={"device": r["name"], "direction": direction},
                             start_ns=start) for r, ns in rows)
    metrics.append({"name": "system.disk.io", "unit": "By", "sum": {
        "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": pts}})
    written += len(rows) * 2
    return metrics, written


def build_system_filesystem(rows):
    """`mountpoint` and `device` are both dimensions in the integration's routing_path, so
    both ride as attributes here. system.fsstat.* is derived from this metric -- see the
    section header."""
    metrics, written = [], 0
    metrics.append({"name": "system.filesystem.utilization", "unit": "1", "gauge": {
        "dataPoints": [datapoint(ns, r["used_pct"],
                                 attrs={"device": r["device_name"],
                                        "mountpoint": r["mount_point"], "type": "ext4"})
                       for r, ns in rows]}})
    written += len(rows)
    usage = []
    for field, state in (("used_bytes", "used"), ("free", "free")):
        usage.extend(datapoint(ns, r[field],
                               attrs={"device": r["device_name"],
                                      "mountpoint": r["mount_point"], "state": state})
                     for r, ns in rows)
    metrics.append({"name": "system.filesystem.usage", "unit": "By",
                    "gauge": {"dataPoints": usage}})
    written += len(rows) * 2
    return metrics, written


def build_system_process(rows):
    """`process.name` and `process.pid` ride as DATA-POINT attributes rather than resource
    attributes, so one resource per host still holds every process -- the same choice the
    postgres statement series makes. Both are constant per series, so nothing fragments."""
    metrics, written = [], 0
    # Named `system.process.*`, not hostmetrics' `process.*`. Two reasons: this loader (and
    # its reload guard) scope a service by metric-name prefix, and hostmetrics carries process
    # identity in RESOURCE attributes -- which this repo deliberately flattens to data-point
    # attributes so one resource still covers a whole host. Having already departed from the
    # receiver on identity, matching the Elastic field names is the more useful choice: it
    # makes these two a near-identity mapping.
    for field, name in (("cpu_norm_pct", "system.process.cpu.utilization"),
                        ("memory_rss_pct", "system.process.memory.utilization")):
        metrics.append({"name": name, "unit": "1", "gauge": {
            "dataPoints": [datapoint(ns, r[field],
                                     attrs={"process.name": r["name"],
                                            "process.pid": str(r["pid"])})
                           for r, ns in rows]}})
        written += len(rows)
    # `system.process.cpu.pct`, mirroring the Elastic field name. It is NOT the same thing as
    # `...cpu.utilization` above: metricbeat's `process.cpu.pct` is not normalised by core
    # count, while `system.process.cpu.total.norm.pct` is -- they differ by exactly the
    # host's core count (4x, 8x or 16x across this fleet). `Top processes by CPU usage`
    # aggregates the NON-normalised one, so shipping only the normalised field made that tile
    # wrong by a per-host factor. Named for the source field precisely so the next person
    # picking a metric for a panel cannot confuse the two.
    metrics.append({"name": "system.process.cpu.pct", "unit": "1", "gauge": {
        "dataPoints": [datapoint(ns, r["cpu_pct"],
                                 attrs={"process.name": r["name"],
                                        "process.pid": str(r["pid"])}) for r, ns in rows]}})
    written += len(rows)
    return metrics, written


SERVICES = {
    "nginx": {"src": "nginx-stubstatus.jsonl", "build": build_nginx,
              "per_scrape": len(NGINX_SUMS) + len(NGINX_STATES)},
    "apache": {"src": "apache-status.jsonl", "build": build_apache,
               "per_scrape": len(APACHE_SUMS) + 1 + 2 + len(APACHE_SCOREBOARD)
                             + len(APACHE_CPU) + 1 + 3},
    # postgres has TWO series files, loaded as two passes over one --service
    "postgresql": {"parts": [
        {"src": "postgres-database.jsonl", "build": build_pg_database,
         "per_scrape": len(PG_DB_SUMS)},
        {"src": "postgres-statement.jsonl", "build": build_pg_statement,
         "per_scrape": len(PG_ST_SUMS)},
    ]},
    # eight series files -- the widest service here
    "system": {"parts": [
        {"src": "system-cpu.jsonl", "build": build_system_cpu,
         "per_scrape": len(SYS_CPU_STATES) + 1},
        {"src": "system-load.jsonl", "build": build_system_load, "per_scrape": 3},
        {"src": "system-memory.jsonl", "build": build_system_memory, "per_scrape": 6},
        {"src": "system-network.jsonl", "build": build_system_network, "per_scrape": 6},
        {"src": "system-diskio.jsonl", "build": build_system_diskio, "per_scrape": 2},
        {"src": "system-filesystem.jsonl", "build": build_system_filesystem,
         "per_scrape": 3},
        {"src": "system-process.jsonl", "build": build_system_process, "per_scrape": 3},
    ]},
    # mysql likewise has two series files
    "mysql": {"parts": [
        {"src": "mysql-status.jsonl", "build": build_mysql_status,
         "per_scrape": MYSQL_STATUS_SERIES},
        {"src": "mysql-replica.jsonl", "build": build_mysql_replica,
         "per_scrape": MYSQL_REPLICA_SERIES},
    ]},
}

if __name__ == "__main__":
    main()
