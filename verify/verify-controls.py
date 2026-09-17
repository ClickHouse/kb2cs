#!/usr/bin/env python3
"""Verify the migrated dashboards' CONTROL BAR against Kibana's, option by option.

    python3 verify-controls.py            # check
    python3 verify-controls.py --mutate   # prove each check can fail

Kibana calls it `controlGroupInput`; ClickStack calls it dashboard-level `filters`. It is a
row of field-bound dropdowns that filter every panel on the dashboard.

WHY THIS EXISTS, three times over
---------------------------------
1. Seven of seventeen migrated dashboards had silently LOST their control bar. A control
   lives outside `panelsJSON`, so a panel inventory never sees it and no tile check misses it.

2. Three more rendered an EMPTY dropdown because the filter lacked `sourceMetricType`, which
   picks the metric table the option list is read from. The schema documents that field as
   "required only when `sourceId` is a Metric source" -- so it is *not* in the top-level
   `required` list, and a metric filter without it validates, saves, and shows nothing.
   A conditionally-required field is one that nothing will reject.

3. Fixing (2) made three dropdowns go from zero options to some options, and "some" read as
   done. It was not. Each had picked up `d40a066c0be9` -- the ClickStack container's own id,
   because the all-in-one image self-monitors into `otel_metrics_*`. A non-empty assertion
   cannot see that, and neither can a COUNT: those dropdowns resolve eight options against
   Kibana's eight and the sets still differ, because the metric-table split costs each of
   them one real host and the collector's id pads it back. Two errors cancelling in the
   total is the whole argument for diffing sets rather than lengths.

WHAT MAKES IT NOT A TAUTOLOGY
-----------------------------
Every option list is resolved from the LIVE filter object -- its `expression` and its
`sourceMetricType` decide which table is queried -- and then diffed against a set obtained
from Elasticsearch. Nothing here is derived from the thing being checked. Deleting a filter,
dropping its `sourceMetricType`, or changing its `expression` all reach the query and fail it.
`--mutate` demonstrates exactly that; a check that cannot be made to fail is not a check.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conf  # noqa: E402  endpoints and credentials, all environment-driven
import mcp as cs  # noqa: E402  the MCP-over-plain-HTTP helper, same directory

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"
_state = {"pass": 0, "fail": 0, "quiet": False}


def ok(m):
    if not _state["quiet"]:
        print(f"  {G}ok{X}   {m}")
    _state["pass"] += 1


def bad(m):
    if not _state["quiet"]:
        print(f"  {R}bad{X}  {m}")
    _state["fail"] += 1


def note(m):
    print(f"  {Y}note{X} {m}")


def hdr(m):
    print(f"\n{B}{m}{X}")


def ch(q):
    """Target query, or None when it fails -- a filter whose expression does not compile is
    exactly the `sourceMetricType` bug this verifier exists to catch, so it must be a
    reportable result rather than a crash."""
    try:
        return conf.ch_query(q, fmt="TSV")
    except conf.QueryError:
        return None


def es_terms(index, field):
    d = conf.es_post("/%s/_search?size=0" % index,
                     {"aggs": {"a": {"terms": {"field": field, "size": 500}}}})
    return {b["key"] for b in d["aggregations"]["a"]["buckets"]}


# A filter's option list is `SELECT DISTINCT <expression>` over the table that
# `sourceMetricType` selects. Absent metric type => the log table. That mapping IS the bug
# from (2): a metric filter that loses the field silently reads otel_logs, where its
# expression usually does not even compile.
TABLE = {"sum": "otel_metrics_sum", "gauge": "otel_metrics_gauge",
         "histogram": "otel_metrics_histogram", "summary": "otel_metrics_summary",
         None: "otel_logs", "": "otel_logs"}


def resolve(flt):
    """The option set a user would actually see in this dropdown, or None if it errors."""
    tbl = TABLE.get(flt.get("sourceMetricType"), "otel_logs")
    expr = flt["expression"]
    out = ch(f"SELECT DISTINCT {expr} AS v FROM {conf.table(tbl)} "
             f"WHERE {expr} != '' ORDER BY v")
    return None if out is None else {l for l in out.splitlines() if l}


# What each control must resolve to, and where the truth comes from. `es` is the
# (index-pattern, field) Kibana's own control is bound to -- every one of these controls is
# bound to the BROAD logs-*/metrics-* data view rather than the integration's index pattern,
# which is what makes the two sides comparable at all.
#
# `tolerate_missing` names a host the ClickStack table legitimately lacks because the dataset
# is split across metric types: replica status is all gauges, postgres all counters.
SPEC = [
    # dashboard, filter name, es index, es field, tolerate_missing, exact?
    ("[Metrics PostgreSQL] Database Overview", "Database",
     "metrics-*", "postgresql.database.name", None, True),
    ("[Metrics Nginx] Overview", "Host",
     "metrics-*", "host.hostname", "mysql-replica-01", False),
    ("[Metrics Apache] Overview", "Host",
     "metrics-*", "host.hostname", "mysql-replica-01", False),
    ("[Metrics MySQL] Replica Status", "Hostname",
     "metrics-*", "host.hostname", "pg-primary-01", False),
    ("[Logs Nginx] Overview", "Nginx instance", "logs-*", "host.hostname", None, True),
    ("[Logs Nginx] Access and error logs", "Nginx instance",
     "logs-*", "host.hostname", None, True),
    ("[Logs Apache] Access and error logs", "Hostname", "logs-*", "host.hostname", None, True),
    # Not migration-invented: LogAttributes['level'] is Elastic's log.level, value for value,
    # including the three casings postgres emits (LOG / Warning / WARNING).
    ("[Logs PostgreSQL] Overview", "Level", "logs-*", "log.level", None, True),
]
# Controls with no Kibana counterpart -- added during migration. Asserted against the dataset
# itself, since there is no source to diff against.
EXTRA = [
    ("[Logs PostgreSQL] Overview", "Database", {"analytics", "postgres", "shop"}),
    ("[Logs PostgreSQL] Query Duration Overview", "Database",
     {"analytics", "postgres", "shop"}),
    ("[Logs Apache] Access and error logs", "Service",
     {"apache", "mysql", "nginx", "postgresql", "system"}),
    ("[Logs Apache] Access and error logs", "Stream",
     {"access_combined", "access_json", "apache_access", "apache_error", "error",
      "mysql_error", "mysql_slowlog", "postgres_log", "system_auth", "system_syslog"}),
]


def live_filters(mutate=None):
    """{(dashboard-prefix, filter-name): filter} from the running ClickStack."""
    out = {}
    for d in cs.call("clickstack_get_dashboard", {}):
        full = cs.call("clickstack_get_dashboard", {"id": d["id"]})
        for f in (full.get("filters") or []):
            f = dict(f)
            if mutate:
                f = mutate(d["name"], f)
                if f is None:
                    continue
            out[(d["name"], f["name"])] = f
    return out


def match(dash, name, filters):
    """Look a control up by dashboard title, tolerating the '(migrated)' suffix."""
    for (dn, fn), f in filters.items():
        if fn == name and dn.startswith(dash[:28]):
            return f
    return None


def run(filters, quiet=False):
    _state["pass"] = _state["fail"] = 0
    _state["quiet"] = quiet

    # The container's own id: resolved, not hardcoded, because it changes on recreate.
    self_host = ch("SELECT DISTINCT ResourceAttributes['host.name'] FROM %s "
                   "WHERE MetricName LIKE 'otelcol%%' "
                   "AND ResourceAttributes['host.name'] != '' LIMIT 1"
                   % conf.table("otel_metrics_gauge"))
    self_host = (self_host or "").strip() or None

    if not quiet:
        hdr("The control bar survived the migration at all")
    if len(filters) >= 12:
        ok(f"{len(filters)} filters across the 17 migrated dashboards")
    else:
        bad(f"only {len(filters)} filters across the migrated dashboards, expected 12")

    if not quiet:
        hdr("Option sets vs Elasticsearch, value by value")
    for dash, name, idx, field, tolerate, exact in SPEC:
        f = match(dash, name, filters)
        if f is None:
            bad(f"{dash} / {name}: FILTER MISSING")
            continue
        got = resolve(f)
        if got is None:
            bad(f"{dash} / {name}: option query FAILED "
                f"(sourceMetricType={f.get('sourceMetricType')!r} -> "
                f"{TABLE.get(f.get('sourceMetricType'),'otel_logs')}, expr={f['expression']})")
            continue
        if not got:
            bad(f"{dash} / {name}: resolves to NOTHING "
                f"(sourceMetricType={f.get('sourceMetricType')!r})")
            continue
        want = es_terms(idx, field)
        # The collector's own id can never be in Elastic; account for it, do not ignore it.
        extra = got - want - ({self_host} if self_host else set())
        missing = want - got
        if self_host and self_host in got:
            selfnote = f" [+{self_host}]"
        else:
            selfnote = ""
        if exact:
            if not extra and not missing:
                ok(f"{dash} / {name}: identical to Kibana ({len(got)}){selfnote}")
            else:
                bad(f"{dash} / {name}: missing={sorted(missing)} unexpected={sorted(extra)}")
        else:
            if not extra and missing == ({tolerate} if tolerate else set()):
                ok(f"{dash} / {name}: all {len(want)} Kibana hosts bar "
                   f"{tolerate!r} (no such metric type){selfnote}")
            elif not extra and not missing:
                ok(f"{dash} / {name}: identical to Kibana ({len(got)}){selfnote}")
            else:
                bad(f"{dash} / {name}: missing={sorted(missing)} unexpected={sorted(extra)}")

    if not quiet:
        hdr("Controls added by the migration (no Kibana counterpart)")
    for dash, name, want in EXTRA:
        f = match(dash, name, filters)
        if f is None:
            bad(f"{dash} / {name}: FILTER MISSING")
            continue
        got = resolve(f)
        if got == want:
            ok(f"{dash} / {name}: {len(got)} option(s), exact")
        elif got is None:
            bad(f"{dash} / {name}: option query FAILED")
        else:
            bad(f"{dash} / {name}: missing={sorted(want-got)} unexpected={sorted(got-want)}")

    if not quiet:
        hdr("Why the self-telemetry rows are inert")
    if self_host is None:
        ok("no collector self-telemetry in otel_metrics_* -- metric dropdowns are exact")
    else:
        pref = ("nginx.", "apache.", "mysql.", "postgresql.", "system.", "process.")
        cond = " OR ".join(f"MetricName LIKE '{p}%'" for p in pref)
        gauge, summ = conf.table("otel_metrics_gauge"), conf.table("otel_metrics_sum")
        n = ch(f"SELECT count() FROM (SELECT 1 FROM {gauge} "
               f"WHERE ResourceAttributes['host.name']='{self_host}' UNION ALL "
               f"SELECT 1 FROM {summ} "
               f"WHERE ResourceAttributes['host.name']='{self_host}')")
        d = ch(f"SELECT count() FROM (SELECT 1 FROM {gauge} "
               f"WHERE ResourceAttributes['host.name']='{self_host}' AND ({cond}) UNION ALL "
               f"SELECT 1 FROM {summ} "
               f"WHERE ResourceAttributes['host.name']='{self_host}' AND ({cond}))")
        if d == "0":
            ok(f"{self_host!r} is the container id: {n} self-telemetry rows, 0 dataset rows")
            if not quiet:
                note("HyperDX's OpAMP config injects a prometheus receiver scraping the")
                note("bundled collector's :8888 into the metrics pipeline. Not fixable from")
                note("the migration: a QUERY_EXPRESSION filter is {name, expression,")
                note("sourceId, sourceMetricType} with no predicate field, so the option")
                note("list is always SELECT DISTINCT over the whole table. See INTEGRATIONS.md.")
        else:
            bad(f"{self_host!r} carries {d} DATASET metric rows -- not self-telemetry, investigate")

    # The reason the rows above are harmless: nothing queryable can reach them.
    unscoped = []
    for d_ in cs.call("clickstack_get_dashboard", {}):
        for t in cs.call("clickstack_get_dashboard", {"id": d_["id"]})["tiles"]:
            c = t.get("config") or {}
            s = c.get("sqlTemplate") or ""
            if "otel_metrics_" in s and "MetricName" not in s:
                unscoped.append(f"{d_['name']}/{t['name']} (sql)")
            sels = c.get("select")
            for sel in (sels if isinstance(sels, list) else []):
                if not isinstance(sel, dict):
                    continue  # `search` tiles store `select` as a column-list STRING
                mn = sel.get("metricName")
                if mn and not mn.startswith(("nginx.", "apache.", "mysql.",
                                             "postgresql.", "system.", "process.")):
                    unscoped.append(f"{d_['name']}/{t['name']} -> {mn}")
    if not unscoped:
        ok("every metric tile scopes by MetricName; none names a non-dataset metric")
    else:
        bad(f"tiles reachable by self-telemetry: {', '.join(unscoped)}")

    return _state["pass"], _state["fail"]


MUTATIONS = [
    ("drop the [Metrics Nginx] Host filter entirely (the lost-control-bar bug)",
     lambda dn, f: None if (dn.startswith("[Metrics Nginx]") and f["name"] == "Host") else f),
    ("drop sourceMetricType from [Metrics PostgreSQL] Database (the empty-dropdown bug)",
     lambda dn, f: ({k: v for k, v in f.items() if k != "sourceMetricType"}
                    if dn.startswith("[Metrics PostgreSQL]") and f["name"] == "Database" else f)),
    ("point [Metrics MySQL] Hostname at the sum table (wrong metric type)",
     lambda dn, f: (dict(f, sourceMetricType="sum")
                    if dn.startswith("[Metrics MySQL]") and f["name"] == "Hostname" else f)),
    ("change the [Logs Nginx] Overview expression to ServiceName (wrong field)",
     lambda dn, f: (dict(f, expression="ServiceName")
                    if dn.startswith("[Logs Nginx] Overview") and f["name"] == "Nginx instance"
                    else f)),
    ("change the [Logs PostgreSQL] Level expression to SeverityNumber (wrong field)",
     lambda dn, f: (dict(f, expression="toString(SeverityNumber)")
                    if dn.startswith("[Logs PostgreSQL] Overview") and f["name"] == "Level"
                    else f)),
]


def mutate_suite():
    """A check that cannot be made to fail is not a check. Prove each one fires."""
    print(f"\n{B}Mutation test: each mutation must turn at least one check red{X}")
    base_p, base_f = run(live_filters(), quiet=True)
    print(f"  {Y}note{X} baseline: {base_p} pass, {base_f} fail")
    if base_f:
        print(f"  {R}bad{X}  baseline is not clean; fix that before trusting the mutations")
        return 1
    bad_muts = 0
    for desc, fn in MUTATIONS:
        p, f = run(live_filters(mutate=fn), quiet=True)
        if f > 0:
            print(f"  {G}fires{X}  {desc}  ({f} check(s) red)")
        else:
            print(f"  {R}SILENT{X} {desc}  -- the check does not catch this")
            bad_muts += 1
    return bad_muts


if __name__ == "__main__":
    if "--mutate" in sys.argv:
        # Mutations are applied to an in-memory copy of the filter objects; nothing is saved
        # back to ClickStack, so this is safe to run against the live stack.
        sys.exit(1 if mutate_suite() else 0)
    p, f = run(live_filters())
    print()
    if f == 0:
        print(f"{G}{p} checks passed.{X} The control bar matches its source.")
    else:
        print(f"{R}{f} check(s) failed.{X} ({p} passed)")
    print()
    sys.exit(f)
