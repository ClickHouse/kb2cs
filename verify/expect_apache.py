#!/usr/bin/env python3
"""Elasticsearch expectations for the migrated apache tiles. Consumed by tilediff.run()."""
import tilediff as td

STATUS = "metrics-apache.status-default"
ACCESS = "logs-apache.access-default"
ERROR = "logs-apache.error-default"
A = "apache.status."

DASHBOARDS = {
    "[Metrics Apache] Overview (migrated)": "6aaad0c3fc9c4df5d97ebf57",
    "[Logs Apache] Access and error logs (migrated)": "6aaab251fc9c4df5d97eb703",
}

# The target names a scoreboard slot by a short state; Elastic files each state as its own
# FIELD with a longer name. Same eleven states, two spellings.
SCOREBOARD = {
    "closing": "closing_connection", "dnslookup": "dns_lookup",
    "finishing": "gracefully_finishing", "idle_cleanup": "idle_cleanup",
    "keepalive": "keepalive", "logging": "logging", "open": "open_slot",
    "reading": "reading_request", "sending": "sending_reply",
    "starting": "starting_up", "waiting": "waiting_for_connection",
}
# The CPU tile array-joins five metrics into one series column.
CPU = {"cpu.load": "cpu.load", "cpu.user": "cpu.user", "cpu.system": "cpu.system",
       "cpu.children_user": "cpu.children_user", "cpu.children_system": "cpu.children_system"}


def _per_host(field, agg, label):
    """{'<label> @ <host>': {bucket: value}} -- the tile's own series naming."""
    return {"%s @ %s" % (label, host): series
            for host, series in td.e_group_agg(field, agg, "host.hostname", STATUS).items()}


def build():
    e = {}

    # ---- number tiles: one value over the whole window --------------------------
    e["Uptime (SQL: raw counter)"] = {
        "_kind": "scalar",
        "Uptime": td.e_scalar(A + "uptime.server_uptime", "max", STATUS)}
    e["Total accesses (SQL: raw counter)"] = {
        "_kind": "scalar",
        "Total accesses": td.e_scalar(A + "total_accesses", "max", STATUS)}
    e["Total egress (SQL: raw counter)"] = {
        "_kind": "scalar",
        "Total egress": td.e_scalar(A + "total_bytes", "max", STATUS)}

    # ---- ratios computed inside the bucket --------------------------------------
    # The tile reduces to one value per (scrape, host) and then averages the ratio. Elastic
    # has one document per (scrape, host), so averaging the ratio over documents is the same
    # thing -- but it needs a script, because there is no ratio field to aggregate.
    # NOTE the `1.0 *`: both operands are longs, so Painless does INTEGER division and the
    # expectation came back 0.0 in all 276 buckets -- which looks exactly like a broken tile.
    e["Requests per sec (SQL: requests / uptime)"] = {
        "_tol": 1e-6,
        "Requests per sec": td.e_agg_script(
            "1.0 * doc['%stotal_accesses'].value / doc['%suptime.server_uptime'].value"
            % (A, A),
            "avg", STATUS)}
    e["Bytes per sec (SQL: traffic / uptime)"] = {
        "_tol": 1e-6,
        "Bytes per sec": td.e_agg_script(
            "1.0 * doc['%stotal_bytes'].value / doc['%suptime.server_uptime'].value"
            % (A, A),
            "avg", STATUS)}

    # ---- gauges averaged inside the bucket --------------------------------------
    e["Total connections"] = {
        "Total connections": td.e_agg(A + "connections.total", "max", STATUS)}
    e["Workers"] = {"Busy": td.e_agg(A + "workers.busy", "avg", STATUS),
                    "Idle": td.e_agg(A + "workers.idle", "avg", STATUS)}
    # `Average server load` is the tile that disagreed with Kibana in 144/144 buckets as a
    # builder gauge tile (3.63 against 3.67) before it became SQL.
    e["Average server load"] = {"Load 1m": td.e_agg(A + "load.1", "avg", STATUS),
                                "Load 5m": td.e_agg(A + "load.5", "avg", STATUS),
                                "Load 15m": td.e_agg(A + "load.15", "avg", STATUS)}

    # ---- long-format tiles: one row per (bucket, series) ------------------------
    # 22 series (11 states x 2 hosts) and 10 series (5 metrics x 2 hosts). Both exceed what a
    # builder tile renders, which is why they are SQL -- and why a wide-format harness would
    # have skipped them entirely.
    sb = {}
    for cs_state, es_suffix in SCOREBOARD.items():
        sb.update(_per_host(A + "scoreboard." + es_suffix, "avg", cs_state))
    e["Scoreboard (SQL: 22 series x buckets exceeds a 5000-row cap)"] = {
        "_kind": "long", "_series_col": "series", "_value_col": "slots", "_data": sb}

    cpu = {}
    for label, es_suffix in CPU.items():
        cpu.update(_per_host(A + es_suffix, "avg", label))
    # Tolerance 5e-4 is DERIVED, not chosen: these five fields are mapped `scaled_float`
    # with scaling_factor=1000, so Elastic stores each sample rounded to the nearest 1/1000
    # while ClickStack keeps the four decimals the scrape produced. Half a quantum bounds the
    # disagreement between averaging quantised samples and averaging exact ones. Checked
    # against the mapping rather than fitted to the failure -- and it is why the raw values
    # 0.0003 and 0.0007 are indistinguishable from 0.000 and 0.001 on the Elastic side.
    e["CPU usage (SQL: 5 metrics x host)"] = {
        "_kind": "long", "_series_col": "series", "_value_col": "value",
        "_tol": 5e-4, "_data": cpu}

    # Restored 2026-09-18. This panel had been declared "not migratable -- the standard OTel
    # apachereceiver emits no async-connection metric", which was simply false: the receiver
    # emits `apache.connections.async` + `connection_state` and enables it BY DEFAULT. The
    # corpus and the Elastic loader had carried the data all along; only the ClickStack loader
    # skipped it. `max`, not `avg`, because that is the operation the Kibana panel uses.
    e["Connections"] = {
        "Writing": td.e_agg(A + "connections.async.writing", "max", STATUS),
        "Keep alive": td.e_agg(A + "connections.async.keep_alive", "max", STATUS),
        "Closing": td.e_agg(A + "connections.async.closing", "max", STATUS)}

    # ---- log dashboard ----------------------------------------------------------
    # Precision-aware: Elastic stores these at second resolution, ClickStack keeps the
    # millisecond, so a bucket edge moves up to one second's events. Bound queried per series.
    AA = "LogAttributes['log.stream'] = 'apache_access'"
    AE = "LogAttributes['log.stream'] = 'apache_error'"
    e["Response codes over time"] = {
        "_kind": "grouped", "_alias": "Requests",
        "_shifted": td.max_per_second(AA, "LogAttributes['status']"),
        "_data": td.e_group_agg(None, "count", "http.response.status_code", ACCESS)}
    e["Error logs over time"] = {
        "_kind": "grouped", "_alias": "Entries",
        "_shifted": td.max_per_second(AE, "LogAttributes['level']"),
        "_data": td.e_group_agg(None, "count", "log.level", ERROR)}

    return e


DIVERGENCES = [
    "Unique IPs by country: DB-IP vs MaxMind. Distribution-checked in verify-apache.sh.",
    "Operating systems / Browsers breakdown: uap-core 'Other' vs Elastic omitting the field. "
    "Full bucket diffs in verify-apache.sh.",
    "Top URLs by response code: Elastic drops url.original on request lines containing a "
    "backslash (503 requests), so it reports 190 distinct URLs to ClickStack's 191. "
    "Asserted in verify-apache.sh.",
]
