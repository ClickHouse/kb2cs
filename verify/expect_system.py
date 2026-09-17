#!/usr/bin/env python3
"""Elasticsearch expectations for the migrated system (Linux) tiles.

Ported 2026-09-17 from the ad-hoc harnesses the system migration was verified with, which
had only ever existed outside the repo. Porting them widened the coverage: the throwaway
version checked one side of each bidirectional counter and skipped one of the two degraded
heatmaps, because those were the series that had already been eyeballed.

Two fields here are deliberately NOT the ones the tiles read, and that is the point of the
file existing at all:

  * `Top processes by CPU usage` is diffed against **`process.cpu.pct`**, the field the
    *Kibana panel* aggregates -- not `system.process.cpu.total.norm.pct`, which is the same
    quantity divided by core count. A wrong-field tile once passed this very check because
    the expectation had been derived from whatever the tile happened to read, making the
    comparison a tautology that agreed 4-16x away from the truth.
  * the memory `cache` series is `used - actual_used`, computed from two Elastic aggregations,
    because Elastic has no cache field and the tile derives it the same way.
"""
import tilediff as td

# Elastic data streams. A customer's are named differently; these are the reference corpus's.
CPU = "metrics-system.cpu-default"
LOAD = "metrics-system.load-default"
MEM = "metrics-system.memory-default"
PROC = "metrics-system.process-default"
FS = "metrics-system.filesystem-default"
NET = "metrics-system.network-default"
DISK = "metrics-system.diskio-default"
AUTH = "logs-system.auth-default"
SYSLOG = "logs-system.syslog-default"

DASHBOARDS = {
    "[Metrics System] Host overview": "6aabe4f6fc9c4df5d97ed876",
    "[Metrics System] Overview": "6aabe4f6fc9c4df5d97ed886",
    "[Logs System] Sudo commands": "6aabe214fc9c4df5d97ed74f",
    "[Logs System] Syslog dashboard": "6aabe214fc9c4df5d97ed758",
    "[Logs System] New users and groups": "6aabe214fc9c4df5d97ed765",
    "[Logs System] SSH login attempts": "6aabe214fc9c4df5d97ed771",
}

# `system.process.cpu.*` and the percentage fields are mapped scaled_float(1000), so Elastic
# stores three decimals where ClickHouse keeps the Float64. Half a quantum bounds it; a
# process averaging 1e-4 is stored as 0.0 on the Elastic side and is not a disagreement.
SCALED_FLOAT_TOL = 5e-4


def _exists(field):
    return [{"exists": {"field": field}}]


def build():
    e = {}

    # ================= [Metrics System] Host overview =========================
    # Gauges averaged inside the bucket -- SQL tiles, because a builder gauge tile collapses
    # to one sample per bucket before aggFn runs.
    e["CPU usage over time"] = {
        "_tol": 1e-3,
        **{state: td.e_agg("system.cpu.%s.norm.pct" % state, "avg", CPU)
           for state in ("user", "system", "nice", "irq", "softirq", "iowait")}}
    e["System load"] = {
        "_tol": 1e-3,
        "load 1m": td.e_agg("system.load.1", "avg", LOAD),
        "load 5m": td.e_agg("system.load.5", "avg", LOAD),
        "load 15m": td.e_agg("system.load.15", "avg", LOAD)}

    mem = td.esb(MEM, {"au": {"avg": {"field": "system.memory.actual.used.bytes"}},
                       "u": {"avg": {"field": "system.memory.used.bytes"}},
                       "f": {"avg": {"field": "system.memory.free"}}})
    e["Memory usage over time"] = {
        "_tol": 1e-3,
        "actual used": {td.k(b): b["au"]["value"] for b in mem},
        # Elastic has no cache field; both sides derive it as used - actual_used.
        "cache": {td.k(b): b["u"]["value"] - b["au"]["value"] for b in mem},
        "free": {td.k(b): b["f"]["value"] for b in mem}}

    # Tables: full distributions, not the top-N that happens to be visible.
    e["Top processes by CPU usage"] = {
        "_kind": "terms", "_key_col": "Process", "_value_col": "Average CPU",
        "_tol": SCALED_FLOAT_TOL,
        "_data": td.e_terms("process.cpu.pct", "avg", "process.name", PROC, size=50)}
    e["Top mountpoints by disk usage"] = {
        "_kind": "terms", "_key_col": "Mount point", "_value_col": "Average used",
        "_tol": 1e-3,
        "_data": td.e_terms("system.filesystem.used.pct", "avg",
                            "system.filesystem.mount_point", FS, size=50)}
    # Both directions. The throwaway harness checked only `In (bytes)`, which would not have
    # noticed the two columns being swapped.
    e["Network traffic per interface"] = {
        "_kind": "terms", "_key_col": "Interface", "_value_col": "In (bytes)",
        "_tol": 1e-9,
        "_data": td.e_terms("system.network.in.bytes", "max",
                            "system.network.name", NET, size=50)}
    e["Network traffic per interface (out)"] = {
        "_kind": "terms", "_key_col": "Interface", "_value_col": "Out (bytes)",
        "_tol": 1e-9, "_tile": "Network traffic per interface",
        "_data": td.e_terms("system.network.out.bytes", "max",
                            "system.network.name", NET, size=50)}

    # counter_rate charts. The second series of each is plotted NEGATED so the chart mirrors
    # around zero, so the expectation is negated too -- otherwise every bucket disagrees.
    def rate(field, ds, extra=None, negate=False):
        raw = [(td.k(b), b["v"]["value"])
               for b in td.esb(ds, {"v": {"sum": {"field": field}}}, extra)]
        out = {}
        for i in range(1, len(raw)):
            v = max(raw[i][1] - raw[i - 1][1], 0) / td.BUCKET
            out[raw[i][0]] = -v if negate else v
        return out

    not_loopback = [{"bool": {"must_not": [{"prefix": {"system.network.name": "l"}}]}}]
    e["Rate of disk IO"] = {
        "read bytes/s": rate("system.diskio.read.bytes", DISK),
        "write bytes/s": rate("system.diskio.write.bytes", DISK, negate=True)}
    e["Network traffic (bytes)"] = {
        "in bytes/s": rate("system.network.in.bytes", NET, not_loopback),
        "out bytes/s": rate("system.network.out.bytes", NET, not_loopback, negate=True)}

    # `number` tiles showing the CURRENT value: the newest scrape in the window, averaged
    # across hosts. A window average would look plausible and be wrong every run.
    e["Load (5m)"] = {
        "_kind": "scalar", "_tol": 1e-3,
        "Load 5m": td.e_scalar_newest("system.load.5", "avg", LOAD)}
    e["Memory Usage [Metrics System]"] = {
        "_kind": "scalar", "_tol": 1e-3,
        "Memory used": td.e_scalar_newest("system.memory.actual.used.pct", "avg", MEM)}

    # ================= [Metrics System] Overview ==============================
    # The two panels that degraded: Kibana renders a host x time heatmap, ClickStack has no
    # categorical heatmap, so both became multi-series lines. One series per host.
    e["Top hosts by CPU usage over time"] = {
        "_kind": "long", "_series_col": "host", "_value_col": "avg cpu user",
        "_tol": 1e-3,
        "_data": td.e_group_agg("system.cpu.user.norm.pct", "avg", "host.name", CPU)}
    e["Top hosts by memory usage over time"] = {
        "_kind": "long", "_series_col": "host", "_value_col": "avg memory used",
        "_tol": 1e-3,
        "_data": td.e_group_agg("system.memory.actual.used.pct", "avg", "host.name", MEM)}

    # ================= [Logs System] x 4 ======================================
    # Whole-window distributions. Asserted EXACTLY: unlike the nginx/apache access logs,
    # `window_edge_slack` is 0 for both system streams, so there is no bucket-edge ambiguity
    # to allow for here.
    e["Sudo commands by user"] = {
        "_kind": "builder_terms", "_alias": "Commands",
        "_data": td.e_terms(None, "count", "user.name", AUTH, size=50,
                            extra=_exists("system.auth.sudo.command"))}
    e["Sudo errors"] = {
        "_kind": "builder_terms", "_alias": "Errors",
        "_data": td.e_terms(None, "count", "system.auth.sudo.error", AUTH, size=50,
                            extra=_exists("system.auth.sudo.error"))}
    # A table keyed on TWO columns: (command, user). Keyed on either alone it is a different
    # distribution, and the tile carries LIMIT 5 matching the source panel's `size: 5`.
    e["Top sudo commands"] = {
        "_kind": "terms", "_key_col": ("Command", "User"), "_value_col": "Count",
        "_data": _pairs("system.auth.sudo.command", "user.name", AUTH,
                        _exists("system.auth.sudo.command"))}

    e["Syslog events by hostname"] = {
        "_kind": "builder_terms", "_alias": "Events",
        "_data": td.e_terms(None, "count", "host.hostname", SYSLOG, size=50)}
    e["Syslog hostnames and processes"] = {
        "_kind": "builder_terms", "_alias": "Events",
        "_data": _pairs("host.hostname", "process.name", SYSLOG, sep=" - ")}

    e["New users over time"] = {
        "_kind": "builder_terms", "_alias": "New users",
        "_data": td.e_terms(None, "count", "user.name", AUTH, size=50,
                            extra=_exists("system.auth.useradd.shell"))}
    e["New users by shell"] = {
        "_kind": "builder_terms", "_alias": "New users",
        "_data": _pairs("system.auth.useradd.shell", "user.name", AUTH,
                        _exists("system.auth.useradd.shell"), sep=" - ")}
    e["New users by home directory"] = {
        "_kind": "builder_terms", "_alias": "New users",
        "_data": _pairs("system.auth.useradd.home", "user.name", AUTH,
                        _exists("system.auth.useradd.home"), sep=" - ")}
    e["New groups over time"] = {
        "_kind": "builder_terms", "_alias": "New groups",
        "_data": td.e_terms(None, "count", "group.name", AUTH, size=50,
                            extra=_exists("group.name"))}

    e["SSH login attempts"] = {
        "_kind": "builder_terms", "_alias": "Attempts",
        "_data": td.e_terms(None, "count", "system.auth.ssh.event", AUTH, size=50,
                            extra=_exists("system.auth.ssh.event"))}
    e["Successful SSH logins"] = {
        "_kind": "builder_terms", "_alias": "Logins",
        "_data": td.e_terms(None, "count", "system.auth.ssh.method", AUTH, size=50,
                            extra=[{"term": {"system.auth.ssh.event": "Accepted"}}])}

    return e


def _pairs(outer, inner, ds, extra=None, sep=" | ", size=50):
    """{'<outer><sep><inner>': count} -- a two-dimension distribution from one ES query."""
    res = td.esflat(ds, {"o": {"terms": {"field": outer, "size": size},
                               "aggs": {"i": {"terms": {"field": inner, "size": size}}}}},
                    extra)
    out = {}
    for ob in res["o"]["buckets"]:
        for ib in ob["i"]["buckets"]:
            out["%s%s%s" % (ob["key"], sep, ib["key"])] = float(ib["doc_count"])
    return out


DIVERGENCES = [
    "SSH users of failed login attempts: Elastic's `system.auth` grok captures the username "
    "with its separator still attached on the `Failed password for invalid user X` variant, "
    "so Kibana reports 27 distinct usernames where there are 14 -- splitting every bot "
    "target in two (`admin` 880 AND `\" admin\"` 759 against ClickStack's 1,639). The panel "
    "this ruins is the source's, not the migration's. Asserted in verify-system.sh.",
    "SSH failed login attempts source locations: DB-IP against MaxMind. Same query, same "
    "data, different country -- differs by ~43% on some. Asserted in verify-system.sh.",
    "Syslog logs / SSH login attempts (search): `search` tiles show raw rows, not an "
    "aggregation. Row counts asserted in verify-system.sh.",
]
