#!/usr/bin/env python3
"""
Backfill the nginx stub_status metric series into the Fleet integration's metrics data
stream, so the shipped [Metrics Nginx] Overview dashboard has something to draw.

  metrics-nginx.stubstatus-default   <- data/metrics/nginx-stubstatus.jsonl

Usage (inside the compose stack):
    docker compose run --rm load python /load-metrics.py --align-hour
    docker compose run --rm -e SHIFT_ANCHOR_EPOCH=... load python /load-metrics.py --align-hour

Two things make this different from load-to-datastream.py, and both are properties of the
target rather than of the data.

1. **The data stream is TSDB** (`index.mode: time_series`). A TSDB write index only accepts
   timestamps inside `index.look_back_time` of now -- **two hours** by default -- so a 24h
   backfill is rejected with "the document timestamp is outside of the allowed range". This
   script therefore writes `look_back_time` into the data stream's `@custom` component
   template BEFORE creating the stream. That template is the supported place for user
   overrides: Fleet creates it empty and does not overwrite it on package upgrade.

   The setting is read when a backing index is created, so it must be in place first. Order
   matters: template, then delete, then write.

2. **Counters must not go backwards.** The generator emits cumulative counters from a
   per-node base, which is what nginx itself does. Nothing here resets them, so the
   `time_series_metric: counter` fields stay valid.

The timestamp shift is computed from data/access.log -- the LOGS corpus -- on purpose: the
metrics were derived from those same requests, so they have to land on the same clock or the
"requests delta == access log lines" invariant stops being checkable side by side.
"""

import argparse
import base64
import calendar
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

ES = os.environ.get("ES", "http://localhost:9200")
ES_USER = os.environ.get("ES_USER", "elastic")
ES_PASS = os.environ.get("ELASTIC_PASSWORD", "changeme")
_AUTH = "Basic " + base64.b64encode(("%s:%s" % (ES_USER, ES_PASS)).encode()).decode()

# TSDB's write index rejects any timestamp older than `look_back_time` before NOW (2h by
# default), so a backfill needs it widened. A fixed value cannot be right: the corpus is a
# static 24h block, so it AGES, and a value that worked when the data was fresh starts
# rejecting the oldest end of it later. 30h was fine on 2026-09-16 and rejected 4,930 of
# 7,200 cpu documents on 2026-09-17, because by then the corpus began 46h in the past.
# So it is computed from the data's own age. LOOK_BACK_MIN is the floor for a freshly
# anchored corpus (24h of data + slack for --align-hour).
LOOK_BACK_MIN_HOURS = 30
LOOK_BACK_MARGIN_HOURS = 3   # slack so a slow load does not age past the limit mid-run

NGINX_COUNTERS = ("requests", "accepts", "handled", "dropped")
NGINX_GAUGES = ("active", "reading", "writing", "waiting", "current")


def nginx_doc(rec, iso):
    node = rec["node"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": "nginx.stubstatus",
                        "namespace": "default"},
        "event": {"dataset": "nginx.stubstatus", "module": "nginx"},
        "service": {"type": "nginx", "address": "http://%s/nginx_status" % node},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
        "nginx": {"stubstatus": dict(
            {k: rec[k] for k in NGINX_COUNTERS},
            **{k: rec[k] for k in NGINX_GAUGES},
            hostname=node)},
    }


def apache_doc(rec, iso):
    """mod_status. Note `scoreboard.total` is MaxRequestWorkers, not a twelfth slot state."""
    node = rec["node"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": "apache.status",
                        "namespace": "default"},
        "event": {"dataset": "apache.status", "module": "apache"},
        "service": {"type": "apache", "address": "http://%s/server-status" % node},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
        "apache": {"status": {
            "total_accesses": rec["total_accesses"],
            "total_bytes": rec["total_bytes"],
            "requests_per_sec": rec["requests_per_sec"],
            "bytes_per_sec": rec["bytes_per_sec"],
            "bytes_per_request": rec["bytes_per_request"],
            "uptime": {"uptime": rec["uptime"], "server_uptime": rec["server_uptime"]},
            "workers": {"busy": rec["workers_busy"], "idle": rec["workers_idle"]},
            "scoreboard": dict(rec["scoreboard"], total=rec["scoreboard_total"]),
            "connections": {"total": rec["connections_total"],
                            "async": {"writing": rec["conn_async_writing"],
                                      "keep_alive": rec["conn_async_keep_alive"],
                                      "closing": rec["conn_async_closing"]}},
            "cpu": {"user": rec["cpu_user"], "system": rec["cpu_system"],
                    "children_user": rec["cpu_children_user"],
                    "children_system": rec["cpu_children_system"],
                    "load": rec["cpu_load"]},
            "load": {"1": rec["load_1"], "5": rec["load_5"], "15": rec["load_15"]},
        }},
    }


def pg_database_doc(rec, iso):
    """pg_stat_database. Cumulative -- which is what postgres reports and what the
    dashboard's differences() formulas need, even though Elastic types rows.* as `gauge`."""
    node = rec["host"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": "postgresql.database",
                        "namespace": "default"},
        "event": {"dataset": "postgresql.database", "module": "postgresql"},
        "service": {"type": "postgresql", "address": "%s:5432" % node},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
        "postgresql": {"database": {
            "name": rec["database"], "oid": rec["oid"],
            "rows": {"fetched": rec["rows_fetched"], "returned": rec["rows_returned"],
                     "inserted": rec["rows_inserted"], "updated": rec["rows_updated"],
                     "deleted": rec["rows_deleted"]},
            "transactions": {"commit": rec["xact_commit"], "rollback": rec["xact_rollback"]},
            "blocks": {"time": {"read": {"ms": rec["blk_read_time_ms"]},
                                "write": {"ms": rec["blk_write_time_ms"]}}},
            "conflicts": rec["conflicts"], "deadlocks": rec["deadlocks"],
        }},
    }


def pg_statement_doc(rec, iso):
    """pg_stat_statements, one document per (database, normalised query)."""
    node = rec["host"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": "postgresql.statement",
                        "namespace": "default"},
        "event": {"dataset": "postgresql.statement", "module": "postgresql"},
        "service": {"type": "postgresql", "address": "%s:5432" % node},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
        "postgresql": {"statement": {
            "database": {"oid": 16384},
            "query": {
                "id": rec["query_id"], "text": rec["query"],
                "calls": rec["calls"], "rows": rec["rows"],
                "time": {"total": {"ms": rec["time_total_ms"]}},
                "memory": {"local": {"read": rec["mem_local_read"],
                                     "hit": rec["mem_local_hit"]},
                           "shared": {"read": rec["mem_shared_read"],
                                      "hit": rec["mem_shared_hit"]}},
            },
        }},
    }


def mysql_status_doc(rec, iso):
    """SHOW GLOBAL STATUS.

    Almost everything here is typed `counter` by the integration, so a migrated tile must
    de-cumulate -- the one exception worth flagging is `max_used_connections`, which MySQL
    reports as a HIGH-WATER MARK yet Elastic types as a counter. It only ever rises, so the
    typing is not wrong exactly, but a counter_rate over it means nothing.

    `threads.created` is the mirror-image quirk: MySQL's Threads_created is cumulative, and
    Elastic types it `gauge`. It is emitted cumulative here because that is what the server
    reports; a panel wanting a rate has to difference it itself.
    """
    node = rec["host"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": "mysql.status",
                        "namespace": "default"},
        "event": {"dataset": "mysql.status", "module": "mysql"},
        "service": {"type": "mysql", "address": "%s:3306" % node},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
        "mysql": {"status": {
            "questions": rec["questions"],
            "connections": rec["connections"],
            "max_used_connections": rec["max_used_connections"],
            "command": {"select": rec["command_select"], "insert": rec["command_insert"],
                        "update": rec["command_update"], "delete": rec["command_delete"]},
            "bytes": {"sent": rec["bytes_sent"], "received": rec["bytes_received"]},
            "aborted": {"clients": rec["aborted_clients"],
                        "connects": rec["aborted_connects"]},
            "connection": {"errors": {
                "select": rec["ce_select"], "peer_address": rec["ce_peer_address"],
                "internal": rec["ce_internal"], "max": rec["ce_max"],
                "accept": rec["ce_accept"], "tcpwrap": rec["ce_tcpwrap"]}},
            "cache": {
                "table": {"open_cache": {"hits": rec["oc_hits"], "misses": rec["oc_misses"],
                                         "overflows": rec["oc_overflows"]}},
                "ssl": {"hits": rec["ssl_hits"], "misses": rec["ssl_misses"],
                        "size": rec["ssl_size"]}},
            "open": {"files": rec["open_files"], "tables": rec["open_tables"],
                     "streams": rec["open_streams"]},
            "threads": {"connected": rec["threads_connected"],
                        "running": rec["threads_running"],
                        "cached": rec["threads_cached"],
                        "created": rec["threads_created"]},
            "innodb": {"buffer_pool": {
                "pages": {"total": rec["bp_pages_total"], "free": rec["bp_pages_free"],
                          "data": rec["bp_pages_data"], "dirty": rec["bp_pages_dirty"]},
                "read": {"requests": rec["bp_read_requests"]},
                "pool": {"reads": rec["bp_pool_reads"]}}},
        }},
    }


def mysql_replica_doc(rec, iso):
    """SHOW REPLICA STATUS, from the replica host.

    Note what the DIMENSIONS are here: this data stream's routing_path is
    `[user.name, mysql.replica_status.source.server.uuid, host.name]`, so the replication
    user and the source's server UUID are part of the series identity. `source.port` is the
    ECS top-level field, NOT `mysql.replica_status.source.port` -- the latter does not exist
    in the mapping, which is easy to get wrong because every other source.* field here does
    live under the mysql prefix.
    """
    node = rec["host"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": "mysql.replica_status",
                        "namespace": "default"},
        "event": {"dataset": "mysql.replica_status", "module": "mysql"},
        "service": {"type": "mysql", "address": "%s:3306" % node},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
        "user": {"name": rec["user"]},
        "source": {"port": rec["source_port"]},
        "mysql": {"replica_status": {
            "seconds_behind_source": rec["seconds_behind_source"],
            "thread": {"sql": {"delay": {"sec": rec["sql_delay_sec"]}}},
            "source": {
                "host": {"name": rec["source_host"]},
                "server": {"id": rec["source_server_id"], "uuid": rec["source_uuid"]},
                "binary_log_file": rec["binlog_file"],
                "file_info": rec["file_info"],
                "log_position": {"read": rec["read_pos"], "exec": rec["exec_pos"]},
            },
            "is_io_thread_running": "Yes",
            "is_sql_thread_running": "Yes",
        }},
    }


# --- system ------------------------------------------------------------------------------
# Eight data streams, more than the other four services combined, and every one of them is
# TSDB -- so each gets its own `look_back_time` override before its stream exists.
#
# The dimensions are not decoration: `metrics-system.network`'s routing_path includes
# `system.network.name`, diskio's includes `system.diskio.name`, and filesystem's includes
# BOTH `mount_point` and `device_name`. Omit one and every interface/mount/device collapses
# onto a single series -- which is exactly the failure mode that made the mysql replica
# metrics read an arbitrary point per bucket.
def _sys_base(rec, iso, dataset):
    node = rec["host"]
    return {
        "@timestamp": iso,
        "data_stream": {"type": "metrics", "dataset": dataset, "namespace": "default"},
        "event": {"dataset": dataset, "module": "system"},
        "service": {"type": "system"},
        "agent": {"id": "backfill-%s" % node, "type": "metricbeat"},
        "host": {"name": node, "hostname": node},
    }


def system_cpu_doc(rec, iso):
    """Percentages are already normalised per core (`.norm.pct`), which is what the panels
    chart. `total` is the sum of the six components by construction, not a separate reading."""
    d = _sys_base(rec, iso, "system.cpu")
    d["system"] = {"cpu": {
        "cores": rec["cores"],
        "user": {"norm": {"pct": rec["user"]}},
        "system": {"norm": {"pct": rec["system"]}},
        "nice": {"norm": {"pct": rec["nice"]}},
        "irq": {"norm": {"pct": rec["irq"]}},
        "softirq": {"norm": {"pct": rec["softirq"]}},
        "iowait": {"norm": {"pct": rec["iowait"]}},
        "idle": {"norm": {"pct": rec["idle"]}},
        "total": {"norm": {"pct": rec["total"]}},
    }}
    return d


def system_load_doc(rec, iso):
    d = _sys_base(rec, iso, "system.load")
    d["system"] = {"load": {"1": rec["load1"], "5": rec["load5"], "15": rec["load15"],
                            "cores": rec["cores"],
                            "norm": {"1": round(rec["load1"] / rec["cores"], 4),
                                     "5": round(rec["load5"] / rec["cores"], 4),
                                     "15": round(rec["load15"] / rec["cores"], 4)}}}
    return d


def system_memory_doc(rec, iso):
    d = _sys_base(rec, iso, "system.memory")
    d["system"] = {"memory": {
        "total": rec["total"], "free": rec["free"],
        "used": {"bytes": rec["used"],
                 "pct": round(rec["used"] / float(rec["total"]), 6)},
        "actual": {"free": rec["total"] - rec["actual_used"],
                   "used": {"bytes": rec["actual_used"], "pct": rec["actual_used_pct"]}},
        "swap": {"total": rec["swap_total"],
                 "used": {"pct": rec["swap_used_pct"],
                          "bytes": int(rec["swap_total"] * rec["swap_used_pct"])},
                 "free": int(rec["swap_total"] * (1.0 - rec["swap_used_pct"]))},
    }}
    return d


def system_network_doc(rec, iso):
    """CUMULATIVE counters. `system.network.name` is a TSDB dimension."""
    d = _sys_base(rec, iso, "system.network")
    d["system"] = {"network": {
        "name": rec["name"],
        "in": {"bytes": rec["in_bytes"], "packets": rec["in_packets"],
               "dropped": rec["in_dropped"], "errors": 0},
        "out": {"bytes": rec["out_bytes"], "packets": rec["out_packets"],
                "dropped": rec["out_dropped"], "errors": 0},
    }}
    return d


def system_diskio_doc(rec, iso):
    """CUMULATIVE counters. `system.diskio.name` is a TSDB dimension."""
    d = _sys_base(rec, iso, "system.diskio")
    d["system"] = {"diskio": {
        "name": rec["name"],
        "read": {"bytes": rec["read_bytes"]},
        "write": {"bytes": rec["write_bytes"]},
    }}
    return d


def system_filesystem_doc(rec, iso):
    d = _sys_base(rec, iso, "system.filesystem")
    d["system"] = {"filesystem": {
        "mount_point": rec["mount_point"], "device_name": rec["device_name"],
        "type": "ext4", "total": rec["total"], "free": rec["free"],
        "available": rec["free"],
        "used": {"bytes": rec["used_bytes"], "pct": rec["used_pct"]},
    }}
    return d


def system_fsstat_doc(rec, iso):
    """The fleet-wide filesystem rollup. Metricbeat computes it by summing the per-mount
    numbers, which is why `total_size.used` here is exactly the sum of the filesystem
    stream's used bytes for the same host and scrape -- a cross-stream invariant."""
    d = _sys_base(rec, iso, "system.fsstat")
    d["system"] = {"fsstat": {
        "count": rec["count"],
        "total_size": {"total": rec["total_size_total"], "used": rec["total_size_used"],
                       "free": rec["total_size_free"]},
    }}
    return d


def system_process_doc(rec, iso):
    """`process.pid` is the TSDB dimension; `process.name` is NOT in the integration's
    explicit mapping but is mapped dynamically as a keyword by `ecs@mappings`
    (`path_match: *.name`), which is what makes `terms(process.name)` work at all."""
    d = _sys_base(rec, iso, "system.process")
    d["process"] = {"name": rec["name"], "pid": rec["pid"],
                    "cpu": {"pct": rec["cpu_pct"]}, "state": "running"}
    d["system"] = {"process": {
        "cpu": {"total": {"norm": {"pct": rec["cpu_norm_pct"]}}},
        "memory": {"rss": {"pct": rec["memory_rss_pct"]}},
    }}
    return d


SERVICES = {
    "nginx": {"ds": "metrics-nginx.stubstatus-default",
              "custom": "metrics-nginx.stubstatus@custom",
              "src": "/data/metrics/nginx-stubstatus.jsonl",
              "logs": "/data/access.log",
              "doc": nginx_doc},
    "apache": {"ds": "metrics-apache.status-default",
               "custom": "metrics-apache.status@custom",
               "src": "/data/metrics/apache-status.jsonl",
               # apache's own access log, so the metrics land on the apache logs' clock
               "logs": "/data/apache/access.log",
               "doc": apache_doc},
    # postgresql is the first service with more than one metric data stream, so its entry
    # carries a list of parts. Each part is its own data stream, @custom template and doc
    # shape; they share one shift, computed from the postgres query log.
    "postgresql": {"logs": "/data/postgres/postgresql.log",
                   "parts": [
                       {"ds": "metrics-postgresql.database-default",
                        "custom": "metrics-postgresql.database@custom",
                        "src": "/data/metrics/postgres-database.jsonl",
                        "doc": pg_database_doc},
                       {"ds": "metrics-postgresql.statement-default",
                        "custom": "metrics-postgresql.statement@custom",
                        "src": "/data/metrics/postgres-statement.jsonl",
                        "doc": pg_statement_doc},
                   ]},
    # mysql is the second multi-part service. Its shift is anchored on the SLOW LOG, which
    # is the only anchor file in the repo whose last line is not a timestamp -- see the
    # mysql branch of compute_delta.
    "mysql": {"logs": "/data/mysql/slowlog.log",
              "parts": [
                  {"ds": "metrics-mysql.status-default",
                   "custom": "metrics-mysql.status@custom",
                   "src": "/data/metrics/mysql-status.jsonl",
                   "doc": mysql_status_doc},
                  {"ds": "metrics-mysql.replica_status-default",
                   "custom": "metrics-mysql.replica_status@custom",
                   "src": "/data/metrics/mysql-replica.jsonl",
                   "doc": mysql_replica_doc},
              ]},
    # Eight parts -- the widest service here. The shift is anchored on the SYSLOG corpus so
    # the host metrics land on the same clock as the host's own log lines.
    "system": {"logs": "/data/system/syslog.log",
               "parts": [
                   {"ds": "metrics-system.cpu-default",
                    "custom": "metrics-system.cpu@custom",
                    "src": "/data/metrics/system-cpu.jsonl", "doc": system_cpu_doc},
                   {"ds": "metrics-system.load-default",
                    "custom": "metrics-system.load@custom",
                    "src": "/data/metrics/system-load.jsonl", "doc": system_load_doc},
                   {"ds": "metrics-system.memory-default",
                    "custom": "metrics-system.memory@custom",
                    "src": "/data/metrics/system-memory.jsonl", "doc": system_memory_doc},
                   {"ds": "metrics-system.network-default",
                    "custom": "metrics-system.network@custom",
                    "src": "/data/metrics/system-network.jsonl", "doc": system_network_doc},
                   {"ds": "metrics-system.diskio-default",
                    "custom": "metrics-system.diskio@custom",
                    "src": "/data/metrics/system-diskio.jsonl", "doc": system_diskio_doc},
                   {"ds": "metrics-system.filesystem-default",
                    "custom": "metrics-system.filesystem@custom",
                    "src": "/data/metrics/system-filesystem.jsonl",
                    "doc": system_filesystem_doc},
                   {"ds": "metrics-system.fsstat-default",
                    "custom": "metrics-system.fsstat@custom",
                    "src": "/data/metrics/system-fsstat.jsonl", "doc": system_fsstat_doc},
                   {"ds": "metrics-system.process-default",
                    "custom": "metrics-system.process@custom",
                    "src": "/data/metrics/system-process.jsonl", "doc": system_process_doc},
               ]},
}


def parts_of(svc):
    """A service is one metric data stream, or several."""
    return svc.get("parts") or [svc]


def http(method, path, body=None, ctype="application/json"):
    req = urllib.request.Request(ES + path, method=method)
    req.add_header("Content-Type", ctype)
    req.add_header("Authorization", _AUTH)
    data = body.encode() if isinstance(body, str) else body
    with urllib.request.urlopen(req, data, timeout=300) as r:
        return json.loads(r.read() or b"{}")


def compute_delta(log_path, mode, anchor_epoch=None):
    """Seconds to add so the LOG corpus's last event lands on the anchor.

    Deliberately the same computation load-to-datastream.py does, on the same file, so the
    metrics and the logs receive an identical shift.
    """
    import calendar
    import re
    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    ACCESS_TS = re.compile(r"\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]")
    # postgres anchors on its own query log, whose prefix is `%t`: 2026-08-17 09:14:22.481 UTC
    POSTGRES_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})")
    # `Aug 17 00:00:01` -- syslog, and it carries NO YEAR, so the year must be supplied and
    # must match SYSLOG_YEAR in load-to-datastream.py or the two loaders disagree by a year.
    SYSLOG_TS = re.compile(r"^(\w{3})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
    SYSLOG_YEAR = 2026
    # mysql anchors on its slow log, whose records are multi-line and END with the SQL
    # statement. So there is no timestamp on the last line at all and the tail has to be
    # scanned for the final `SET timestamp=` -- the same value load-to-datastream.py uses.
    MYSQL_SET_TS = re.compile(r"^SET timestamp=(\d+);", re.M)
    if mode == "none":
        return 0
    with open(log_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        tail = f.read().decode("utf-8", "replace")
        last = tail.strip().split("\n")[-1]
    m = ACCESS_TS.search(last)
    if m:
        d, mon, y, hh, mm, ss, _ = m.groups()
        end = calendar.timegm((int(y), MONTHS.index(mon) + 1, int(d),
                               int(hh), int(mm), int(ss), 0, 0, 0))
    else:
        pm = POSTGRES_TS.match(last)
        if pm:
            y, mo, d, hh, mm, ss, _ms = pm.groups()
            end = calendar.timegm((int(y), int(mo), int(d),
                                   int(hh), int(mm), int(ss), 0, 0, 0))
        else:
            sm = SYSLOG_TS.match(last)
            if sm:
                mon, d, hh, mm, ss = sm.groups()
                end = calendar.timegm((SYSLOG_YEAR, MONTHS.index(mon) + 1, int(d),
                                       int(hh), int(mm), int(ss), 0, 0, 0))
            else:
                hits = MYSQL_SET_TS.findall(tail)
                if not hits:
                    sys.exit("cannot read a timestamp from the end of %s" % log_path)
                end = int(hits[-1])
    anchor = int(anchor_epoch) if anchor_epoch else int(time.time())
    if mode == "align-hour":
        anchor = (anchor // 3600) * 3600
    return anchor - end


def look_back_hours(oldest_epoch):
    """Hours of look-back needed to admit `oldest_epoch`, floored and with margin."""
    age_h = int(math.ceil((time.time() - oldest_epoch) / 3600.0))
    return max(LOOK_BACK_MIN_HOURS, age_h + LOOK_BACK_MARGIN_HOURS)


def ensure_look_back(template, hours):
    """Put look_back_time on the @custom component template, before any index exists."""
    body = json.dumps({"template": {"settings": {"index": {"look_back_time": "%dh" % hours}}}})
    http("PUT", "/_component_template/%s" % template, body)
    print("[load] %s: index.look_back_time=%dh (TSDB rejects anything older at the 2h "
          "default)" % (template, hours))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", choices=sorted(SERVICES), default="nginx",
                    help="which service's metric series to load (default nginx). Each writes "
                         "only its own data stream")
    ap.add_argument("--src", default=None)
    ap.add_argument("--logs", default=None,
                    help="the log corpus the shift is computed from")
    ap.add_argument("--no-shift", action="store_true")
    ap.add_argument("--align-hour", action="store_true")
    ap.add_argument("--anchor-epoch", type=int, default=None)
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--part", default=None,
                    help="for a multi-stream service, load only the part whose source file "
                         "name contains this substring (e.g. --part system-cpu). Mirrors the "
                         "same flag on stack/clickstack/load-metrics.py, so one stream can be "
                         "regenerated and reloaded on both stacks without touching the rest")
    args = ap.parse_args()

    svc = SERVICES[args.service]
    parts = parts_of(svc)
    if args.part:
        parts = [p for p in parts if args.part in p["src"]]
        if not parts:
            sys.exit("--part %r matches none of: %s"
                     % (args.part, ", ".join(p["src"] for p in parts_of(svc))))
        print("[load] restricted to %s" % ", ".join(p["src"] for p in parts))
    args.logs = args.logs or svc.get("logs")
    if args.src:
        if len(parts) > 1:
            sys.exit("--src cannot be used with %s: it has %d metric data streams"
                     % (args.service, len(parts)))
        parts = [dict(parts[0], src=args.src)]
    for part in parts:
        if not os.path.exists(part["src"]):
            sys.exit("missing %s -- run generator/generate-%s*.py"
                     % (part["src"], args.service))

    mode = "none" if args.no_shift else ("align-hour" if args.align_hour else "now")
    anchor = args.anchor_epoch or (os.environ.get("SHIFT_ANCHOR_EPOCH") or None)
    delta = compute_delta(args.logs, mode, anchor)
    print("[load] time shift: %+d seconds (%.2f days), mode=%s%s — computed from %s so the "
          "metrics land on the logs' clock"
          % (delta, delta / 86400.0, mode, (", anchor=%s" % anchor) if anchor else "",
             os.path.basename(args.logs)))

    info = http("GET", "/")
    print("[load] elasticsearch %s" % info["version"]["number"])

    # The corpus is 24h long and ends at the anchor, so its oldest point is anchor-24h.
    # Derive the look-back from that rather than hardcoding it -- see LOOK_BACK_MIN_HOURS.
    oldest = None
    for part in parts:
        with open(part["src"], encoding="utf-8") as fh:
            first = fh.readline()
        if first.strip():
            t = json.loads(first)["ts"]
            e = calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%S.000Z")) + delta
            oldest = e if oldest is None else min(oldest, e)
    lb_hours = look_back_hours(oldest) if oldest else LOOK_BACK_MIN_HOURS
    if oldest:
        print("[load] oldest point is %s (%.1fh old) -> look_back_time=%dh"
              % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(oldest)),
                 (time.time() - oldest) / 3600.0, lb_hours))

    overall = []
    for part in parts:
        DS, doc_for = part["ds"], part["doc"]
        ensure_look_back(part["custom"], lb_hours)

        if not args.append:
            try:
                http("DELETE", "/_data_stream/%s" % DS)
                print("[load] %s: cleared existing data stream" % DS)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                print("[load] %s: nothing to clear" % DS)

        sent = errors = 0
        first_errors = []
        buf = []

        def flush():
            nonlocal sent, errors, buf
            if not buf:
                return
            resp = http("POST", "/%s/_bulk" % DS, "".join(buf), "application/x-ndjson")
            n = len(buf) // 2
            if resp.get("errors"):
                for item in resp.get("items", []):
                    err = item.get("create", {}).get("error")
                    if err:
                        errors += 1
                        if len(first_errors) < 3:
                            first_errors.append("%s: %s" % (err.get("type"),
                                                            str(err.get("reason"))[:240]))
            sent += n
            buf = []
            print("    %s: %d sent, %d rejected" % (DS, sent, errors), end="\r", flush=True)

        with open(part["src"], encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                epoch = time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S.000Z")
                import calendar as _cal
                iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                    time.gmtime(_cal.timegm(epoch) + delta))
                buf.append('{"create":{}}\n')
                buf.append(json.dumps(doc_for(rec, iso), ensure_ascii=False) + "\n")
                if len(buf) >= 4000:
                    flush()
        flush()
        print()
        http("POST", "/%s/_refresh" % DS)
        overall.append((DS, sent, errors, first_errors))

    print("\n[load] summary")
    failed = 0
    for DS, sent, errors, msgs in overall:
        print("    %-40s %7d sent, %d rejected" % (DS, sent, errors))
        for m in msgs:
            print("      ! %s" % m)
        failed += errors
    if failed:
        sys.exit(1)
    print("\n[load] open Kibana -> Analytics > Dashboard > search '%s'" % args.service)


if __name__ == "__main__":
    main()
