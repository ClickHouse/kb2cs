#!/usr/bin/env python3
"""Elasticsearch expectations for the migrated postgresql tiles. Consumed by tilediff.run()."""
import tilediff as td

DB = "metrics-postgresql.database-default"
STMT = "metrics-postgresql.statement-default"
LOG = "logs-postgresql.log-default"
D = "postgresql.database."
Q = "postgresql.statement.query."

DASHBOARDS = {
    "[Metrics PostgreSQL] Database Overview (migrated)": "6aaadb69fc9c4df5d97ec446",
    "[Logs PostgreSQL] Overview (migrated)": "6aaadae7fc9c4df5d97ec3f2",
    "[Logs PostgreSQL] Query Duration Overview (migrated)": "6aaadb2efc9c4df5d97ec41f",
}

# The two statement tiles exclude pg's own bookkeeping statements, exactly as the source
# panels did. Kept verbatim from the tile SQL so the two lists cannot drift apart silently.
EXCLUDE = ("BEGIN;", "begin", "commit", "end", "END;",
           "SELECT * FROM pg_stat_statements", "SELECT * FROM pg_stat_database",
           "SELECT * FROM pg_stat_bgwriter", "SELECT * FROM pg_stat_activity")


def _rate(field, ds=DB, base_agg="max"):
    """A per-second counter rate: the shape `greatest((m - lag(m)) / $__interval_s, 0)`."""
    return td.e_diff(field, ds, per_second=True, floor_zero=True, base_agg=base_agg)


def build():
    e = {}

    # ---- counter rates, divided by the bucket width -----------------------------
    e["Rows Fetched/Returned"] = {"rows.fetched": _rate(D + "rows.fetched"),
                                  "rows.returned": _rate(D + "rows.returned")}
    e["Database Transactions"] = {"rollback": _rate(D + "transactions.rollback"),
                                  "commit": _rate(D + "transactions.commit")}
    e["Fileblock IO"] = {"blocks.time.write.ms": _rate(D + "blocks.time.write.ms"),
                         "blocks.time.read.ms": _rate(D + "blocks.time.read.ms")}
    e["Rows Inserted/Deleted/Updated"] = {"rows.updated": _rate(D + "rows.updated"),
                                          "rows.inserted": _rate(D + "rows.inserted"),
                                          "rows.deleted": _rate(D + "rows.deleted")}
    # NOTE the asymmetry, which is the tile's and is preserved here deliberately: `deadlocks`
    # AVERAGES the samples in the bucket before differencing while `conflicts` takes the max.
    # Using max for both would disagree in almost every bucket, and the disagreement would
    # look like a migration bug rather than a base-aggregation mismatch.
    e["Conflict/Deadlock Rates"] = {
        "deadlocks": _rate(D + "deadlocks", base_agg="avg"),
        "conflicts": _rate(D + "conflicts")}
    e["Shared block cache stats"] = {"shared.read": _rate(Q + "memory.shared.read", STMT),
                                     "shared.hit": _rate(Q + "memory.shared.hit", STMT)}

    # ---- NOT a rate: this tile plots the raw counter, unlike its three siblings --
    e["Local block cache stats"] = {
        "local.read": td.e_agg(Q + "memory.local.read", "max", STMT),
        "local.hit": td.e_agg(Q + "memory.local.hit", "max", STMT)}

    # ---- per-query series -------------------------------------------------------
    # Differenced WITHIN each query's own series (the tile partitions by series), which is a
    # different number from differencing the total.
    # `query.time.total.ms` is mapped `float` in Elastic. `_source` holds full precision but
    # AGGREGATIONS read single-precision doc_values, so this cumulative counter is on a 0.25
    # grid by the time max() sees it, and differencing doubles that. Tolerance derived from
    # the counter's own magnitude, not fitted: two of thirteen series disagreed by 0.05.
    latency = td.e_group_diff(Q + "time.total.ms", Q + "text", STMT, size=500, drop=EXCLUDE)
    peak = max((max(v.values()) for v in latency.values() if v), default=0)
    cumulative = td.e_scalar(Q + "time.total.ms", "max", STMT) or peak
    e["Query Latency"] = {
        "_kind": "long", "_series_col": "series", "_value_col": "value",
        "_tol": td.float32_delta_tol(cumulative),
        "_data": latency}
    e["Top Queries"] = {
        "_kind": "terms", "_key_col": "series", "_value_col": "Calls",
        "_data": {qt: v for qt, v in
                  td.e_terms(Q + "calls", "max", Q + "text", STMT, size=500).items()
                  if qt not in EXCLUDE}}

    # ---- log dashboards ---------------------------------------------------------
    PL = "LogAttributes['log.stream'] = 'postgres_log'"
    e["Logs by level over time"] = {
        "_kind": "grouped", "_alias": "Logs",
        "_shifted": td.max_per_second(PL, "LogAttributes['level']"),
        "_data": td.e_group_agg(None, "count", "log.level", LOG)}
    # An UNBUCKETED total, so it carries the window-edge slack rather than a per-bucket one:
    # Elastic truncates to the second, so one document at the boundary falls inside its window
    # and outside the target's. Queried, not assumed -- it evaluates to 1, and the tile
    # differed by exactly 1 of 118,759.
    e["Log Level Count"] = {
        "_kind": "builder_terms", "_alias": "Count",
        "_tol": td.window_edge_slack(PL),
        "_data": td.e_terms(None, "count", "log.level", LOG)}
    # Both series, not just the count: the cumulated duration is the one a wrong unit would
    # break (Elastic files event.duration in NANOseconds) and the count would not.
    has_dur = [{"exists": {"field": "event.duration"}}]
    dur = "toFloat64OrZero(LogAttributes['duration_ns'])"
    e["Query count and cumulated duration"] = {
        # Per-alias bounds: the count series can move by one second's EVENTS, the duration
        # series by one second's worth of NANOSECONDS. Using the count bound for both failed
        # the duration series by +641 against a bound of 10, which reads as a broken tile.
        "_shifted": {"Queries": td.max_per_second(PL),
                     "Cumulated duration (ns)": td.max_per_second(PL, value_expr=dur)},
        "Queries": td.e_count(LOG, has_dur),
        "Cumulated duration (ns)": td.e_agg_window_sum("event.duration", LOG, has_dur)}

    return e


DIVERGENCES = [
    "Slow Queries / Query Durations / All Logs are `search` tiles -- raw rows, not an "
    "aggregation. Their row counts are asserted in verify-postgres.sh.",
]
