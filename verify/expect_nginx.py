#!/usr/bin/env python3
"""Elasticsearch expectations for the migrated nginx tiles. Consumed by tilediff.run().

Every series here is built from `metrics-nginx.stubstatus-default` or `logs-nginx.*` with an
aggregation chosen to match what the *Kibana panel* did -- not what the tile does. Where the
two genuinely cannot agree the divergence is named in DIVERGENCES rather than tolerated
silently.
"""
import tilediff as td

STUB = "metrics-nginx.stubstatus-default"
ACCESS = "logs-nginx.access-default"
ERROR = "logs-nginx.error-default"
S = "nginx.stubstatus."

DASHBOARDS = {
    "[Metrics Nginx] Overview (migrated)": "6aaacb54fc9c4df5d97ebc23",
    "[Logs Nginx] Overview (migrated)": "6a87414da2ec56fc6f837a8d",
    "[Logs Nginx] Access and error logs (migrated)": "6a87414da2ec56fc6f837a90",
}

# Kibana's `Response codes over time` buckets on the status code and the tile groups on the
# FAMILY, so the Elastic terms have to be folded the same way to be comparable.
def _family(code):
    return "%dxx" % (int(code) // 100)


def build():
    e = {}

    # ---- [Metrics Nginx] Overview ------------------------------------------------
    # Raw counters: the panel plots max() of a monotonic counter inside each bucket.
    e["Total requests (SQL: raw counter)"] = {
        "Total requests": td.e_agg(S + "requests", "max", STUB)}
    e["Processed requests (SQL: raw counter)"] = {
        "Processed requests": td.e_agg(S + "handled", "max", STUB)}

    # Gauges averaged INSIDE the bucket. This is the shape that cannot be expressed as a
    # builder gauge tile at all -- HyperDX collapses a gauge to one sample per bucket (the
    # last) before aggFn runs, so `average()` returned the last sample and disagreed with
    # Kibana in 141/144 buckets by ~1%. These tiles are SQL for exactly that reason.
    e["Active connections"] = {"Active": td.e_agg(S + "active", "avg", STUB)}
    e["Reading / Writing / Waiting Rates"] = {
        "reading": td.e_agg(S + "reading", "avg", STUB),
        "writing": td.e_agg(S + "writing", "avg", STUB),
        "waiting": td.e_agg(S + "waiting", "avg", STUB)}

    # Counter differences. floor_zero=False because the tiles subtract without greatest():
    # flooring here would hide a real negative rather than surface it.
    e["Request Rate (SQL: Kibana-faithful)"] = {
        "Request Rate": td.e_diff(S + "requests", STUB, floor_zero=False)}
    e["Accepts and Handled Rate (SQL: Kibana-faithful)"] = {
        "Accepts": td.e_diff(S + "accepts", STUB, floor_zero=False),
        "Handled": td.e_diff(S + "handled", STUB, floor_zero=False)}
    # The integration computes `dropped` = accepts - handled per scrape; the tile recomputes
    # it per host from the two counters. Same quantity by two routes, which is worth checking.
    e["Drops Rate (SQL: accepted − handled, per host)"] = {
        "Drops Rate": td.e_diff(S + "dropped", STUB, floor_zero=False)}

    # Distinct hosts reporting in the bucket. This tile was a BUILDER tile with
    # aggFn=count_distinct until this harness reached it, and it under-counted: it returned 2
    # in 2 of 277 buckets where all three hosts had reported 10 points each with 10 distinct
    # values -- verified directly in ClickHouse, so the data was never in question. Same
    # family as the gauge collapse: an aggFn on a metric source does not run over the raw
    # rows. Converted to SQL, which is what the other seven tiles on this dashboard already
    # were, for the same class of reason.
    e["Heartbeat / Up (SQL: builder count_distinct under-counts)"] = {
        "Hosts up": td.e_agg("host.hostname", "cardinality", STUB)}

    # ---- [Logs Nginx] Overview ---------------------------------------------------
    # Log-derived series use the precision-aware comparison: Elastic stores these events with
    # SECOND resolution (all 499,964 access documents have millisecond == 0) while ClickStack
    # keeps the millisecond, so events within a second of a bucket edge land in different
    # 5-minute buckets. The bound is not a fudge -- it is the largest number of events that
    # share any single second, queried from the data, per series.
    AJ = "LogAttributes['log.stream'] = 'access_json'"
    ERR = "LogAttributes['log.stream'] = 'error'"
    FAMILY = "concat(toString(intDiv(toUInt16(LogAttributes['status']), 100)), 'xx')"

    e["Response codes over time"] = {
        "_kind": "grouped", "_alias": "Requests",
        "_shifted": td.max_per_second(AJ, FAMILY),
        "_data": td.e_group_agg(None, "count", "http.response.status_code", ACCESS,
                                keyfn=_family)}
    e["Errors over time"] = {
        "_kind": "grouped", "_alias": "Entries",
        "_shifted": td.max_per_second(ERR, "LogAttributes['level']"),
        "_data": td.e_group_agg(None, "count", "log.level", ERROR)}
    e["Data Volume"] = {
        "_shifted": td.max_per_second(
            AJ, value_expr="toUInt64(LogAttributes['body_bytes_sent'])"),
        "Bytes sent": td.e_agg("http.response.body.bytes", "sum", ACCESS)}
    e["Access logs over time"] = {
        "_shifted": td.max_per_second(AJ),
        "Requests": td.e_count(ACCESS)}

    return e


# Declared, not tolerated. Each is asserted in verify-nginx.sh so nobody "fixes" it.
DIVERGENCES = [
    "Nginx logs — requests by country: DB-IP vs MaxMind disagree by up to ~43% on CA. "
    "Checked as a distribution in verify-nginx.sh, not here.",
    "Operating systems / Browsers breakdown: uap-core returns the literal 'Other' where "
    "Elastic omits user_agent.os.name entirely. Full 8- and 25-bucket diffs live in "
    "verify-nginx.sh.",
    "Top pages: 503 requests whose request line contains a backslash have no url.original "
    "in Elastic at all (the integration's grok drops it), so the distributions differ by "
    "one key. Asserted in verify-apache.sh's sibling check.",
]
