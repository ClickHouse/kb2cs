#!/usr/bin/env python3
"""Elasticsearch expectations for the migrated mysql tiles. Consumed by tilediff.run().

Extracted unchanged (bar the helper signatures) from the original mysql-only
`verify-tiles-vs-elastic.py` when that harness was generalised to four integrations.
"""
import tilediff as td

STATUS = "metrics-mysql.status-default"
REPLICA = "metrics-mysql.replica_status-default"
F = "mysql.status."
R = "mysql.replica_status."

DASHBOARDS = {
    "[Logs MySQL] Overview": "6aaae566fc9c4df5d97ecb24",
    "[Metrics MySQL] Database Overview": "6aaae625fc9c4df5d97ecb81",
    "[Metrics MySQL] Replica Status": "6aaae646fc9c4df5d97ecbb4",
}


def build():
    def agg(f, a):
        return td.e_agg(F + f, a, STATUS)

    def diff(f, per_second=False):
        return td.e_diff(F + f, STATUS, per_second=per_second)

    def last(f):
        return td.e_last(R + f, REPLICA)

    e = {
        "Open Tables, Files, Streams": {"Files": agg("open.files", "avg"),
                                        "Tables": agg("open.tables", "avg"),
                                        "Streams": agg("open.streams", "avg")},
        "Thread Activity": {"Running (avg)": agg("threads.running", "avg"),
                            "Running (max)": agg("threads.running", "max"),
                            "Connected (max)": agg("threads.connected", "max")},
        "Buffer Pool Pages": {"Total": agg("innodb.buffer_pool.pages.total", "avg"),
                              "Free": agg("innodb.buffer_pool.pages.free", "avg"),
                              "Data": agg("innodb.buffer_pool.pages.data", "avg")},
        "Statements Executed": {"Statements/s": diff("questions", True)},
        "Rate of SELECT statements": {"SELECT/s": diff("command.select", True)},
        "Rate of INSERT, UPDATE, DELETE": {
            "INSERT/s": diff("command.insert", True),
            "UPDATE/s": diff("command.update", True),
            "DELETE/s": diff("command.delete", True)},
        "Aborted Connections Rate": {
            "Aborted clients/s": diff("aborted.clients", True),
            "Aborted connects/s": diff("aborted.connects", True)},
        # The tile plots received traffic as a NEGATIVE series so the chart mirrors around
        # zero; the expectation has to be negated to match, or all 276 buckets disagree.
        "Network Traffic": {
            "Sent/s": diff("bytes.sent", True),
            "Received/s": {kk: -v for kk, v in diff("bytes.received", True).items()}},
        "Connections": {"Threads connected": agg("threads.connected", "max"),
                        "Max used connections": agg("max_used_connections", "max"),
                        "Connections/s": diff("connections", True)},
        "Connection Errors": {
            "select": diff("connection.errors.select"),
            "peer_address": diff("connection.errors.peer_address"),
            "internal": diff("connection.errors.internal"),
            "accept": diff("connection.errors.accept"),
            "max": diff("connection.errors.max")},
        "Commands Operations": {"INSERT": diff("command.insert"),
                                "DELETE": diff("command.delete"),
                                "UPDATE": diff("command.update"),
                                "SELECT": diff("command.select")},
        "Open Tables Cache": {"Hits": diff("cache.table.open_cache.hits"),
                              "Misses": diff("cache.table.open_cache.misses"),
                              "Overflows": diff("cache.table.open_cache.overflows")},
        "SSL Cache": {"Misses": diff("cache.ssl.misses"),
                      "Size": diff("cache.ssl.size"),
                      "Hits": diff("cache.ssl.hits")},
        "SQL thread delay": {"SQL thread delay (s)": last("thread.sql.delay.sec")},
        "Replication lag": {"Seconds behind source": last("seconds_behind_source")},
        "Position of the IO and SQL threads in the source binary log": {
            "Read position (IO thread)": last("source.log_position.read"),
            "Exec position (SQL thread)": last("source.log_position.exec")},
    }

    # Two ratio tiles, each computed from two Elastic aggregations in the same pass.
    bp = td.esb(STATUS, {"tot": {"max": {"field": F + "innodb.buffer_pool.pages.total"}},
                         "free": {"max": {"field": F + "innodb.buffer_pool.pages.free"}},
                         "reads": {"max": {"field": F + "innodb.buffer_pool.pool.reads"}},
                         "reqs": {"max": {"field": F + "innodb.buffer_pool.read.requests"}}})
    e["Buffer Pool Utilization"] = {"Utilization": {
        td.k(b): ((b["tot"]["value"] - b["free"]["value"]) / b["tot"]["value"]
                  if b["tot"]["value"] else 0.0) for b in bp}}
    e["Buffer Pool Efficiency"] = {"Miss rate pct": {
        td.k(b): (b["reads"]["value"] / b["reqs"]["value"] * 100
                  if b["reqs"]["value"] else 0.0) for b in bp}}
    return e


DIVERGENCES = [
    "`Source overview` is a most-recent-N table (LIMIT 500) rather than a series; its row "
    "shape is asserted in verify-mysql.sh.",
    "The `[Logs MySQL] Overview` tiles are search/terms panels, asserted in verify-mysql.sh.",
]
