#!/usr/bin/env python3
"""Machinery for diffing migrated ClickStack tiles against Elasticsearch BUCKET FOR BUCKET.

Not a script. `verify-tiles-vs-elastic.py` is the entry point; the per-integration
expectations live in `expect_{mysql,nginx,apache,postgres}.py`.

This was extracted from `verify-tiles-vs-elastic.py` when the mysql-only harness had to cover
nginx, apache and postgres too. Everything here is integration-agnostic; everything that
names a metric or a field lives in an `expect_*` module.

WHAT IT VERIFIES, AND WHY THAT SHAPE
------------------------------------
Totals, whole-window averages and a careful visual pass all miss the same class of error: a
series that is the right shape and quietly wrong in every bucket. In this repo that class has
appeared twice -- eight nginx/apache gauge series that disagreed in 141/144 and 144/144
buckets by ~1%, after both migrations had passed their verifier and been eyeballed.

So tiles are re-run the way the dashboard runs them:

  * **sql tiles** -- the stored `sqlTemplate` is read back from the live dashboard, its macros
    expanded, and executed. A hand-written "equivalent" would re-encode whatever
    misunderstanding produced the tile, and pass.
  * **builder tiles** -- re-issued through `clickstack_timeseries` with the tile's own
    select/groupBy/where, which is the code path the tile itself uses.

Four tile shapes exist in the estate and each needs different handling, which is why
expectations declare a `_kind`:

  | `_kind`  | tile SQL shape                  | expectation                       |
  |----------|---------------------------------|-----------------------------------|
  | `wide`   | `ts, "alias1", "alias2"`        | `{alias: {bucket: value}}`        |
  | `long`   | `ts, series, value`             | `{group: {bucket: value}}`        |
  | `scalar` | `"alias"` (a `number` tile)     | `{alias: value}`                  |
  | `terms`  | `series, "alias"` (a bar/table) | `{group: value}`                  |

`long` exists because a tile with more series than a builder tile will render must be written
as SQL emitting one row per (bucket, series) -- apache's 22-series scoreboard, its 10-series
CPU breakdown, postgres's per-query latency. Those are exactly the tiles a wide-format
harness would silently skip.
"""
import base64
import json
import math
import os
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conf  # noqa: E402  endpoints and credentials, all environment-driven
import mcp as cs  # noqa: E402  the MCP-over-plain-HTTP helper, same directory

BUCKET = 300
# Re-exported so the expect_* modules and older call sites keep working.
AUTH = conf.ES_AUTH

# Every metric this dataset generates. Used to derive the window while EXCLUDING the
# ClickStack image's own self-telemetry (otelcol_*/scrape_*/up), which is live-scraped and
# would otherwise drag `max(TimeUnix)` to now and put the window past the corpus.
DATASET_PREFIXES = ("nginx.", "apache.", "mysql.", "postgresql.", "system.", "process.")


def ch(q, fmt="JSONCompact"):
    """Query the target, returning `{"_error": ...}` instead of raising.

    The callers here deliberately want a soft failure -- a tile whose stored SQL no longer
    compiles must be REPORTED as a failed series, not crash the run before the other 156 are
    checked.
    """
    try:
        return conf.ch_query(q, fmt)
    except conf.QueryError as exc:
        return {"_error": str(exc)} if fmt == "JSONCompact" else None


def corpus_window():
    """Derive ONE absolute window from the data, inset by an hour at the start.

    A relative window ("last 24h") puts the two stacks on different edges of a static corpus
    and manufactures differences no query can fix -- it produced two false bug reports here.
    A hardcoded window is wrong the moment the corpus is re-anchored, which is a routine
    operation (RUNBOOK, "Keeping the two stacks on the same clock"); hardcoding it once
    already meant hand-editing every harness after a re-anchor.

    Rounded to WHOLE HOURS, a multiple of every bucket size used. A window ending mid-bucket
    makes the final bucket partial on the Elasticsearch side while a builder `increase` fills
    it completely from data past endTime -- which looked like a 2.5x bug on six series.

    Override with TILEDIFF_WIN_START / TILEDIFF_WIN_END.
    """
    s, e = os.environ.get("TILEDIFF_WIN_START"), os.environ.get("TILEDIFF_WIN_END")
    if s and e:
        return s, e
    cond = " OR ".join("MetricName LIKE '%s%%'" % p for p in DATASET_PREFIXES)
    out = ch("SELECT toString(toStartOfHour(min(TimeUnix)) + INTERVAL 1 HOUR), "
             "       toString(toStartOfHour(max(TimeUnix))) "
             "FROM " + conf.table("otel_metrics_sum") + " WHERE " + cond, fmt="TSV")
    lo, hi = out.split("\t")
    return lo.replace(" ", "T") + "Z", hi.replace(" ", "T") + "Z"


START, END = corpus_window()
CH_FROM, CH_TO = START.replace("T", " ").rstrip("Z"), END.replace("T", " ").rstrip("Z")


def expand(sql):
    """Expand the HyperDX macros a stored sqlTemplate contains."""
    sql = sql.replace("$__interval_s", str(BUCKET))
    sql = re.sub(r"\$__timeInterval\(([^)]+)\)",
                 r"toStartOfInterval(\1, INTERVAL %d second)" % BUCKET, sql)
    sql = re.sub(r"\$__timeFilter(?:_ms)?\(([^)]+)\)",
                 r"(\1 >= toDateTime('%s') AND \1 <= toDateTime('%s'))" % (CH_FROM, CH_TO),
                 sql)
    return sql


# ------------------------------------------------------------------ Elasticsearch side
def _post(path, body):
    return conf.es_post(path, body)


def _query(extra=None):
    f = [{"range": {"@timestamp": {"gte": START, "lte": END}}}]
    if extra:
        f += extra
    return {"bool": {"filter": f}}


def esb(ds, aggs, extra=None):
    """Per-bucket aggregation over the window."""
    body = {"size": 0, "query": _query(extra),
            "aggs": {"t": {"date_histogram": {"field": "@timestamp",
                                              "fixed_interval": "%ds" % BUCKET,
                                              "min_doc_count": 1},
                           "aggs": aggs}}}
    return _post("/" + ds + "/_search", body)["aggregations"]["t"]["buckets"]


def esflat(ds, aggs, extra=None):
    """Aggregation over the whole window, no time bucketing."""
    body = {"size": 0, "query": _query(extra), "aggs": aggs}
    return _post("/" + ds + "/_search", body)["aggregations"]


def k(b):
    """Normalise a bucket key to 'YYYY-MM-DD HH:MM'.

    Elastic renders '15:00' and ClickHouse '15:00:00'. Joining the raw strings yields an
    EMPTY intersection, which reads as "everything is broken" rather than "the harness is".
    """
    return b["key_as_string"][:16].replace("T", " ")


def e_agg(field, agg, ds):
    """{bucket: agg(field)} -- for a tile that aggregates a gauge inside the bucket."""
    return {k(b): b["v"]["value"] for b in esb(ds, {"v": {agg: {"field": field}}})
            if b["v"]["value"] is not None}


def e_agg_script(script, agg, ds):
    """{bucket: agg(expression)} -- for a panel plotting a RATIO of two fields.

    Needed because several tiles divide one metric by another inside the bucket
    (requests/uptime, traffic/uptime) and Elasticsearch has no field to aggregate for that.
    """
    return {k(b): b["v"]["value"]
            for b in esb(ds, {"v": {agg: {"script": {"source": script}}}})
            if b["v"]["value"] is not None}


def e_diff(field, ds, per_second=False, floor_zero=True, base_agg="max"):
    """{bucket: increase of a counter} -- Kibana's differences() on base_agg(field).

    The first bucket is absent, exactly as Kibana's differences() leaves it null.
    `base_agg` is not always max: one postgres tile averages the samples in the bucket before
    differencing, and using max there would disagree in every bucket.
    """
    raw = [(k(b), b["v"]["value"]) for b in esb(ds, {"v": {base_agg: {"field": field}}})]
    raw = [(kk, v) for kk, v in raw if v is not None]
    d = BUCKET if per_second else 1
    out = {}
    for i in range(1, len(raw)):
        delta = raw[i][1] - raw[i - 1][1]
        out[raw[i][0]] = (max(delta, 0) if floor_zero else delta) / d
    return out


def e_last(field, ds):
    """{bucket: newest document's value} -- Lens last_value."""
    out = {}
    for b in esb(ds, {"v": {"top_hits": {"size": 1, "sort": [{"@timestamp": "desc"}],
                                         "_source": [field]}}}):
        h = b["v"]["hits"]["hits"]
        if not h:
            continue
        src = h[0]["_source"]
        for part in field.split("."):
            src = src[part]
        out[k(b)] = float(src)
    return out


def e_count(ds, extra=None):
    """{bucket: doc count} -- a log panel's count-over-time."""
    return {k(b): float(b["doc_count"]) for b in esb(ds, {}, extra)}


def e_group_agg(field, agg, term, ds, extra=None, size=200, keyfn=None):
    """{group: {bucket: value}} -- one series per terms bucket.

    `keyfn` relabels the Elastic term to whatever the tile emits, which is how a status code
    becomes a `2xx` family or a raw state name becomes the target's own spelling.
    """
    aggs = {"g": {"terms": {"field": term, "size": size},
                  "aggs": ({} if agg == "count" else {"v": {agg: {"field": field}}})}}
    out = {}
    for b in esb(ds, aggs, extra):
        for gb in b["g"]["buckets"]:
            key = str(gb["key"])
            key = keyfn(key) if keyfn else key
            val = float(gb["doc_count"]) if agg == "count" else gb["v"]["value"]
            if val is None:
                continue
            # keyfn may fold several terms into one series (status -> status family)
            out.setdefault(key, {}).setdefault(k(b), 0.0)
            out[key][k(b)] += float(val)
    return out


def e_group_diff(field, term, ds, per_second=False, floor_zero=True, base_agg="max",
                 size=200, drop=()):
    """{group: {bucket: increase}} -- a per-series counter difference.

    One series per terms bucket, differenced WITHIN the series. A tile that writes
    `lagInFrame(...) OVER (PARTITION BY series ORDER BY ts)` is doing exactly this, and
    differencing the ungrouped total instead would silently agree on the shape while being
    wrong everywhere.
    """
    per = e_group_agg(field, base_agg, term, ds, size=size)
    out = {}
    d = BUCKET if per_second else 1
    for g, series in per.items():
        if g in drop:
            continue
        ks = sorted(series)
        cur = {}
        for i in range(1, len(ks)):
            delta = series[ks[i]] - series[ks[i - 1]]
            cur[ks[i]] = (max(delta, 0) if floor_zero else delta) / d
        if cur:
            out[g] = cur
    return out


def e_terms(field, agg, term, ds, extra=None, size=200):
    """{group: value} -- a distribution over the whole window, no bucketing."""
    aggs = {"g": {"terms": {"field": term, "size": size},
                  "aggs": ({} if agg == "count" else {"v": {agg: {"field": field}}})}}
    res = esflat(ds, aggs, extra)
    out = {}
    for gb in res["g"]["buckets"]:
        val = float(gb["doc_count"]) if agg == "count" else gb["v"]["value"]
        if val is not None:
            out[str(gb["key"])] = float(val)
    return out


def e_agg_window_sum(field, ds, extra=None):
    """{bucket: sum(field)} with a document filter -- e.g. only documents that HAVE the field."""
    return {k(b): b["v"]["value"]
            for b in esb(ds, {"v": {"sum": {"field": field}}}, extra)
            if b["v"]["value"] is not None}


def e_scalar_newest(field, agg, ds, extra=None):
    """agg(field) over ONLY the newest scrape in the window.

    Several `number` tiles show a current value, not a window average, and express it as
    `TimeUnix = (SELECT max(TimeUnix) ...)`. Averaging the whole window instead would give a
    plausible-looking number that is wrong in every run -- and it would still be within any
    loose tolerance, which is what makes this worth a dedicated helper.
    """
    newest = esflat(ds, {"mx": {"max": {"field": "@timestamp"}}},
                    extra)["mx"]["value_as_string"]
    if newest is None:
        return None
    at_newest = list(extra or []) + [{"term": {"@timestamp": newest}}]
    return esflat(ds, {"v": {agg: {"field": field}}}, at_newest)["v"]["value"]


def e_scalar(field, agg, ds, extra=None):
    """A single number over the whole window -- for a `number` tile."""
    return esflat(ds, {"v": {agg: {"field": field}}}, extra)["v"]["value"]


# ------------------------------------------------------------------ builder -> SQL
# `clickstack_timeseries` caps the rows it returns and exposes no limit or pagination
# parameter, so a grouped tile silently comes back truncated -- an nginx error-level tile
# returned 107 rows where the data has ~1,100, which reads as "the chart is empty" rather
# than "the transport clipped it". For grouped tiles the builder config is therefore COMPILED
# to SQL instead.
#
# This keeps the property that matters: the query is still derived from the LIVE tile
# (its aggFn, valueExpression, where and groupBy), not from a hand-written "equivalent" that
# would re-encode whatever misunderstanding produced the tile and then agree with it.
AGG = {"count": lambda e: "count()",
       "count_distinct": lambda e: "uniqExact(%s)" % e,
       "sum": lambda e: "sum(%s)" % e,
       "avg": lambda e: "avg(%s)" % e,
       "max": lambda e: "max(%s)" % e,
       "min": lambda e: "min(%s)" % e,
       "last_value": lambda e: "argMax(%s, Timestamp)" % e}

_SOURCES = None


def source_table(source_id):
    """(table, timestamp column) for a source id. Only log sources need compiling today."""
    global _SOURCES
    if _SOURCES is None:
        _SOURCES = {s["id"]: s for s in cs.call("clickstack_list_sources", {})["sources"]}
    src = _SOURCES.get(source_id, {})
    if src.get("kind") != "log":
        raise ValueError("builder->SQL compilation is only wired for log sources, got %r"
                         % src.get("kind"))
    return conf.table("otel_logs"), src.get("timestampColumn", "Timestamp")


def builder_sql(cfg, item, group_expr=None):
    """Compile one select item of a builder tile into the query the tile itself runs."""
    table, tscol = source_table(cfg["sourceId"])
    lang = (item.get("whereLanguage") or "lucene").lower()
    where = (item.get("where") or "").strip()
    if where and lang != "sql":
        raise ValueError("select-item where is %r, not sql; cannot compile faithfully" % lang)
    tile_where = (cfg.get("where") or "").strip()
    if tile_where and (cfg.get("whereLanguage") or "lucene").lower() != "sql":
        raise ValueError("tile-level where is not sql; cannot compile faithfully")

    agg = AGG[item["aggFn"]](item.get("valueExpression") or "")
    preds = ["%s >= toDateTime('%s')" % (tscol, CH_FROM),
             "%s <= toDateTime('%s')" % (tscol, CH_TO)]
    for w in (where, tile_where):
        if w:
            preds.append("(%s)" % w)
    sel = ["toStartOfInterval(%s, INTERVAL %d second) AS ts" % (tscol, BUCKET)]
    grp = ["ts"]
    if group_expr:
        sel.append("%s AS grp" % group_expr)
        grp.append("grp")
    sel.append("%s AS val" % agg)
    return ("SELECT %s FROM %s WHERE %s GROUP BY %s ORDER BY %s"
            % (", ".join(sel), table, " AND ".join(preds), ", ".join(grp), ", ".join(grp)))


def window_edge_slack(where):
    """How many events sit in the two seconds straddling the window edges.

    An UNBUCKETED total over a window can only disagree by this much when the two platforms
    disagree on sub-second placement: Elastic truncates these timestamps to the second, so a
    document at 14:00:00.5 is stored as 14:00:00 and falls inside `lte 14:00:00`, while the
    same event on the target keeps .5 and falls outside. That is the entire story behind the
    postgres `Log Level Count` tile differing by exactly one document out of 118,759.
    """
    q = ("SELECT count() FROM {tbl} WHERE ({where}) AND ("
         "(Timestamp >= toDateTime('{lo}') - INTERVAL 1 SECOND"
         "  AND Timestamp < toDateTime('{lo}'))"
         " OR (Timestamp > toDateTime('{hi}')"
         "  AND Timestamp < toDateTime('{hi}') + INTERVAL 1 SECOND))").format(
             tbl=conf.table("otel_logs"), where=where, lo=CH_FROM, hi=CH_TO)
    res = ch(q)
    return int(res["data"][0][0]) if res.get("data") else 1


def float32_delta_tol(magnitude):
    """Tolerance for differencing a counter that Elasticsearch stores as a 32-bit `float`.

    `_source` keeps full precision but AGGREGATIONS read doc_values, which for a `float`
    field are single precision. A cumulative counter that reaches ~3.2e6 is therefore
    quantised to a 0.25 grid before `max()` ever sees it, and subtracting two quantised
    values doubles that error -- which is why two postgres query-latency series disagreed by
    0.05 on values of ~290 while eleven others matched exactly.

    Derived from the counter's own magnitude rather than fitted to the failure: single
    precision has a 24-bit significand, so the grid spacing at `magnitude` is 2^(e-24).
    """
    if not magnitude or magnitude <= 0:
        return 1e-6
    return 2.0 * math.ldexp(1.0, math.frexp(float(magnitude))[1] - 24)


def max_per_second(where, group_expr=None, value_expr=None):
    """The largest number of events sharing a single second -- the exact bucket-edge bound.

    This is what makes `compare_shifted` a real check rather than a fudge factor: the
    tolerance is not a guessed percentage, it is the most events that a one-second timestamp
    difference could possibly move across a bucket boundary. Returns a scalar, or
    {group: bound} when `group_expr` is given, so a sparse series gets a tight bound (an
    nginx `alert` series with 12 events all window long is allowed to move by 1, not by 66).

    `value_expr` bounds a SUM rather than a count -- for a bytes-per-bucket series the most a
    boundary can move is the largest single second's worth of bytes, not of events.
    """
    pred = ("Timestamp >= toDateTime('%s') AND Timestamp <= toDateTime('%s')"
            % (CH_FROM, CH_TO))
    if where:
        pred += " AND (%s)" % where
    if group_expr:
        agg = "sum(%s)" % value_expr if value_expr else "count()"
        q = ("SELECT grp, max(c) AS b FROM (SELECT {expr} AS grp,"
             " toStartOfSecond(Timestamp) AS s, {agg} AS c FROM {tbl}"
             " WHERE {pred} GROUP BY grp, s) GROUP BY grp").format(
                 expr=group_expr, agg=agg, tbl=conf.table("otel_logs"), pred=pred)
        res = ch(q)
        return {str(r[0]): int(r[1]) for r in res.get("data", [])}
    agg = "sum(%s)" % value_expr if value_expr else "count()"
    q = ("SELECT max(c) FROM (SELECT toStartOfSecond(Timestamp) AS s, {agg} AS c "
         "FROM {tbl} WHERE {pred} GROUP BY s)").format(
             agg=agg, tbl=conf.table("otel_logs"), pred=pred)
    res = ch(q)
    return int(res["data"][0][0]) if res.get("data") else 1


# ------------------------------------------------------------------ comparison
class Results(list):
    def add(self, ok, tile, alias):
        self.append((ok, tile, alias))


RESULTS = Results()
TOL = 1e-6


def compare(tile, alias, expected, actual, tol=TOL):
    """Values must agree on every shared bucket; bucket SETS may differ only at the edges.

    An extra bucket on either side is tolerated when it is the first or last of that series
    and nowhere else -- a stray bucket in the middle is a real disagreement. The edges are
    genuinely ambiguous: a builder `increase` reads the counter from before startTime, so it
    emits a leading bucket where Kibana's differences() has none, and can emit a trailing
    bucket at endTime fed by data past the window.
    """
    ekeys, akeys = set(expected), set(actual)
    common = ekeys & akeys
    # ABSOLUTE tolerance, deliberately. An earlier version of this line used
    # `max(tol, tol * abs(expected))`, which silently turns a tolerance of 1 into a 100%
    # relative tolerance -- a check that passes anything. Every tolerance passed in here is
    # derived from a storage quantum or a window edge, so absolute is the correct reading.
    bad = [kk for kk in sorted(common) if abs(expected[kk] - actual[kk]) > tol]

    edges_a = {min(akeys), max(akeys)} if akeys else set()
    edges_e = {min(ekeys), max(ekeys)} if ekeys else set()
    extra_a = sorted(akeys - ekeys)
    extra_e = sorted(ekeys - akeys)
    stray = [kk for kk in extra_a if kk not in edges_a] + \
            [kk for kk in extra_e if kk not in edges_e]

    ok = bool(common) and not bad and not stray
    RESULTS.add(ok, tile, alias)
    note = "  (+%d/-%d at the edges)" % (len(extra_a), len(extra_e)) if (extra_a or extra_e) else ""
    print("    %-34s e=%-4d t=%-4d bad=%-4d %s%s"
          % (alias[:34], len(expected), len(actual), len(bad),
             "OK" if ok else "** FAIL **", note))
    for kk in bad[:2]:
        print("        %s elastic=%.6f tile=%.6f" % (kk, expected[kk], actual[kk]))
    for kk in stray[:3]:
        print("        bucket %s on one side only, and NOT at an edge" % kk)
    return ok


def compare_shifted(tile, alias, expected, actual, bound, tol_net=None):
    """Compare a LOG-derived series whose two platforms disagree on sub-second placement.

    Demanding exact per-bucket equality here is wrong, and the reason is measurable rather
    than a matter of taste: **all 499,964 Elasticsearch nginx.access documents have
    millisecond == 0**, because the integration parses the combined log's second-resolution
    `time_local`, while the ClickStack side retains the millisecond the event carries. Events
    within one second of a bucket boundary therefore land in different 5-minute buckets, and
    since a single second can hold up to `bound` events, individual buckets differ by up to
    that much while the totals stay exact. 234 of 276 buckets differ that way for nginx
    access logs; the signed deltas sum to 3 out of 493,569.

    So this asserts the shape of that disagreement instead of its absence:

      * no bucket differs by more than one second's worth of events (`bound`);
      * the signed deltas CONSERVE -- they sum to ~0, i.e. events moved between buckets
        rather than appearing or vanishing;
      * the bucket sets still agree except at the window edges.

    Those three together still fail on every real bug: a double-counted stream shifts every
    bucket by ~100%, a wrong field changes the magnitude, a dropped group vanishes entirely,
    and a scale error breaks conservation. What they tolerate is only re-bucketing.
    """
    ekeys, akeys = set(expected), set(actual)
    shared = ekeys & akeys
    if not shared:
        _fail(tile, alias, "no shared buckets")
        return False

    # Absent means ZERO, not "no comparison". A sparse series whose only event in a bucket
    # moves across the boundary leaves that bucket present on one platform and missing on the
    # other -- the same one-second shift, showing up as bucket PRESENCE instead of a value.
    # Treating it as a stray bucket failed three nginx series (5xx, info) for the very effect
    # this function exists to model. Buckets outside the shared range are excluded, because
    # there the difference really is window-edge truncation rather than a shift.
    lo, hi = min(shared), max(shared)
    keys = sorted(kk for kk in (ekeys | akeys) if lo <= kk <= hi)
    deltas = [expected.get(kk, 0.0) - actual.get(kk, 0.0) for kk in keys]
    worst = max(deltas, key=abs) if deltas else 0.0
    net = sum(deltas)
    tol_net = bound if tol_net is None else tol_net

    ok = abs(worst) <= bound and abs(net) <= tol_net
    RESULTS.add(ok, tile, alias)
    ndiff = sum(1 for d in deltas if d)
    onesided = len((ekeys ^ akeys) & set(keys))
    print("    %-34s n=%-4d differ=%-4d worst=%+d net=%+d (bound %d)%s %s"
          % (alias[:34], len(keys), ndiff, int(worst), int(net), bound,
             " 1-sided=%d" % onesided if onesided else "",
             "OK" if ok else "** FAIL **"))
    if not ok:
        if abs(worst) > bound:
            k_worst = keys[deltas.index(worst)]
            print("        bucket %s moved by %+d, more than one second of events (%d)"
                  % (k_worst, int(worst), bound))
        if abs(net) > tol_net:
            print("        deltas do NOT conserve: net %+d -- events appeared or vanished,"
                  % int(net))
            print("        which re-bucketing cannot explain")
    return ok


def _fail(tile, alias, msg):
    print("    %-34s ** %s **" % (alias[:34], msg))
    RESULTS.add(False, tile, alias)


# ------------------------------------------------------------------ the runner
def _cmp(exp, tile, alias, expected, actual):
    """Route to the exact or the precision-aware comparison, per the expectation."""
    b = exp.get("_shifted")
    nt = exp.get("_net_tol")
    # Bounds may be per-series, keyed by group for a grouped tile or by column alias for a
    # wide one. Two series on the SAME tile can need different bounds: a count series is
    # bounded by events-per-second, a sum series by that sum's value-per-second.
    if isinstance(b, dict):
        b = b.get(alias, 1)
    if isinstance(nt, dict):
        nt = nt.get(alias)
    if b:
        return compare_shifted(tile, alias, expected, actual, b, nt)
    return compare(tile, alias, expected, actual, exp.get("_tol", TOL))


def _sql_rows(cfg, tile):
    res = ch(expand(cfg["sqlTemplate"]))
    if "_error" in res:
        _fail(tile, "sql", "SQL ERROR " + res["_error"][:160])
        return None, None
    names = [c[0] if isinstance(c, list) else c["name"] for c in res["meta"]]
    return names, res["data"]


def run_wide_sql(cfg, tile, exp):
    names, rows = _sql_rows(cfg, tile)
    if names is None:
        return
    ti = names.index("ts") if "ts" in names else 0
    for alias, series in exp.items():
        if alias.startswith("_"):
            continue
        if alias not in names:
            _fail(tile, alias, "column missing from tile SQL")
            continue
        ci = names.index(alias)
        # a NULL is "no value in this bucket" -- what Kibana shows for the first bucket of a
        # differences() series. Not a zero, and not an error.
        got = {str(r[ti])[:16]: float(r[ci]) for r in rows if r[ci] is not None}
        _cmp(exp, tile, alias, series, got)


def run_long_sql(cfg, tile, exp):
    names, rows = _sql_rows(cfg, tile)
    if names is None:
        return
    scol, vcol = exp["_series_col"], exp["_value_col"]
    for c in ("ts", scol, vcol):
        if c not in names:
            _fail(tile, c, "column %r missing from tile SQL" % c)
            return
    ti, si, vi = names.index("ts"), names.index(scol), names.index(vcol)
    got = {}
    for r in rows:
        if r[vi] is None:
            continue
        got.setdefault(str(r[si]), {})[str(r[ti])[:16]] = float(r[vi])
    data = exp["_data"]
    for g in sorted(set(data) | set(got)):
        if g not in data:
            _fail(tile, g, "series present on the tile and not in Elastic")
        elif g not in got:
            _fail(tile, g, "series present in Elastic and not on the tile")
        else:
            _cmp(exp, tile, g, data[g], got[g])


def run_scalar_sql(cfg, tile, exp):
    names, rows = _sql_rows(cfg, tile)
    if names is None:
        return
    for alias, want in exp.items():
        if alias.startswith("_"):
            continue
        if alias not in names:
            _fail(tile, alias, "column missing from tile SQL")
            continue
        ci = names.index(alias)
        got = {"(window)": float(rows[0][ci])} if rows and rows[0][ci] is not None else {}
        compare(tile, alias, {"(window)": float(want)}, got, exp.get("_tol", TOL))


def run_terms_sql(cfg, tile, exp):
    names, rows = _sql_rows(cfg, tile)
    if names is None:
        return
    # `_key_col` may name several columns: a table keyed on (command, user) is a different
    # distribution from one keyed on either alone, and collapsing it to the first column
    # silently sums over the second.
    kcols = exp["_key_col"]
    kcols = [kcols] if isinstance(kcols, str) else list(kcols)
    vcol = exp["_value_col"]
    for c in kcols + [vcol]:
        if c not in names:
            _fail(tile, c, "column %r missing from tile SQL" % c)
            return
    kis, vi = [names.index(c) for c in kcols], names.index(vcol)
    sep = exp.get("_key_sep", " | ")
    got = {sep.join(str(r[i]) for i in kis): float(r[vi])
           for r in rows if r[vi] is not None}
    want = exp["_data"]
    # A `terms` tile carries LIMIT N, so Elastic's tail beyond N is not a disagreement --
    # but every key the tile DOES show must match, and the tile must not invent one.
    shared = set(want) & set(got)
    missing = [g for g in got if g not in want]
    if missing:
        _fail(tile, vcol, "tile shows %d key(s) absent from Elastic: %s"
              % (len(missing), ", ".join(sorted(missing)[:3])))
        return
    compare(tile, vcol + " (%d keys)" % len(shared),
            {g: want[g] for g in shared}, {g: got[g] for g in shared},
            exp.get("_tol", TOL))


def run_builder(cfg, tile, exp):
    for alias, series in exp.items():
        if alias.startswith("_"):
            continue
        sels = cfg.get("select")
        item = None
        if isinstance(sels, list):
            item = next((s for s in sels
                         if isinstance(s, dict) and s.get("alias") == alias), None)
        if item is None:
            _fail(tile, alias, "no select item with this alias")
            continue
        args = {"sourceId": cfg["sourceId"], "startTime": START, "endTime": END,
                "granularity": "5 minute", "select": [item]}
        if cfg.get("where"):
            args["where"] = cfg["where"]
        r = cs.call("clickstack_timeseries", args)
        got = {}
        for row in r["result"]["data"]:
            b = row["__hdx_time_bucket"][:16].replace("T", " ")
            vals = [v for kk, v in row.items() if kk != "__hdx_time_bucket"]
            if vals and vals[0] is not None:
                got[b] = float(vals[0])
        _cmp(exp, tile, alias, series, got)


def run_builder_grouped(cfg, tile, exp):
    """A builder tile with a groupBy renders one series per group.

    Compiled to SQL rather than re-issued through `clickstack_timeseries`, because that tool
    caps its result rows with no way to raise or page the limit: the nginx error-level tile
    came back with 107 of ~1,100 rows, so 5 of its 6 series looked almost empty and the sixth
    looked short. The compiled query is still built from the tile's own aggFn / where /
    groupBy -- see builder_sql.
    """
    alias = exp["_alias"]
    sels = cfg.get("select")
    item = next((s for s in sels if isinstance(s, dict) and s.get("alias") == alias), None) \
        if isinstance(sels, list) else None
    if item is None:
        _fail(tile, alias, "no select item with this alias")
        return
    try:
        sql = builder_sql(cfg, item, cfg.get("groupBy"))
    except ValueError as exc:
        _fail(tile, alias, "cannot compile: %s" % exc)
        return
    res = ch(sql)
    if "_error" in res:
        _fail(tile, alias, "compiled SQL failed: " + res["_error"][:160])
        return
    names = [c[0] if isinstance(c, list) else c["name"] for c in res["meta"]]
    ti, gi, vi = names.index("ts"), names.index("grp"), names.index("val")
    got = {}
    for r in res["data"]:
        if r[vi] is None:
            continue
        got.setdefault(str(r[gi]), {})[str(r[ti])[:16]] = float(r[vi])
    data = exp["_data"]
    for g in sorted(set(data) | set(got)):
        if g not in data:
            _fail(tile, g, "series on the tile and not in Elastic")
        elif g not in got:
            _fail(tile, g, "series in Elastic and not on the tile")
        else:
            _cmp(exp, tile, g, data[g], got[g])


def run_builder_terms(cfg, tile, exp):
    """A builder TABLE/bar tile: a distribution with no time axis.

    Compiled from the tile's own config for the same reason grouped time series are -- the
    MCP transport caps rows -- and because `clickstack_table` would need its own result
    shape handling for no benefit.
    """
    alias = exp["_alias"]
    sels = cfg.get("select")
    item = next((s for s in sels if isinstance(s, dict) and s.get("alias") == alias), None) \
        if isinstance(sels, list) else None
    if item is None:
        _fail(tile, alias, "no select item with this alias")
        return
    try:
        table, tscol = source_table(cfg["sourceId"])
        lang = (item.get("whereLanguage") or "lucene").lower()
        where = (item.get("where") or "").strip()
        if where and lang != "sql":
            raise ValueError("select-item where is %r, not sql" % lang)
        agg = AGG[item["aggFn"]](item.get("valueExpression") or "")
        preds = ["%s >= toDateTime('%s')" % (tscol, CH_FROM),
                 "%s <= toDateTime('%s')" % (tscol, CH_TO)]
        if where:
            preds.append("(%s)" % where)
        sql = ("SELECT %s AS grp, %s AS val FROM %s WHERE %s GROUP BY grp"
               % (cfg["groupBy"], agg, table, " AND ".join(preds)))
    except (ValueError, KeyError) as exc:
        _fail(tile, alias, "cannot compile: %s" % exc)
        return
    res = ch(sql)
    if "_error" in res:
        _fail(tile, alias, "compiled SQL failed: " + res["_error"][:160])
        return
    names = [c[0] if isinstance(c, list) else c["name"] for c in res["meta"]]
    gi, vi = names.index("grp"), names.index("val")
    got = {str(r[gi]): float(r[vi]) for r in res["data"] if r[vi] is not None}
    want = exp["_data"]
    extra = sorted(set(got) - set(want))
    missing = sorted(set(want) - set(got))
    if extra or missing:
        _fail(tile, alias, "keys differ -- only-tile=%s only-elastic=%s"
              % (extra[:3], missing[:3]))
        return
    compare(tile, alias + " (%d keys)" % len(want), want, got, exp.get("_tol", TOL))


KINDS = {"wide": run_wide_sql, "long": run_long_sql, "scalar": run_scalar_sql,
         "terms": run_terms_sql, "builder": run_builder, "grouped": run_builder_grouped,
         "builder_terms": run_builder_terms}


def run(dashboards, expect):
    """Walk the live dashboards and check every tile that has an expectation."""
    for dname, did in dashboards.items():
        print("\n== %s ==" % dname)
        dash = cs.call("clickstack_get_dashboard", {"id": did})
        seen = set()
        # An expectation is normally keyed by the tile name. `_tile` overrides that, so one
        # tile can carry TWO expectations under different keys -- needed where a table has
        # two independent value columns (an interface's in and out bytes) and checking only
        # the first would not notice them being swapped.
        by_tile = {}
        for key, exp in expect.items():
            by_tile.setdefault(exp.get("_tile", key), []).append((key, exp))
        for t in dash["tiles"]:
            cfg, name = t["config"], t["name"]
            if cfg.get("displayType") == "markdown" or name not in by_tile:
                continue
            seen.add(name)
            for key, exp in by_tile[name]:
                _run_one(cfg, name, key, exp)


def _run_one(cfg, name, key, exp):
    kind = exp.get("_kind", "wide" if cfg.get("sqlTemplate") else "builder")
    label = name if key == name else "%s  -> %s" % (name, key)
    print("  %s  [%s/%s]" % (label, "sql" if cfg.get("sqlTemplate") else "builder", kind))
    KINDS[kind](cfg, name, exp)


def check_row_caps():
    """Fail any TIME-SERIES tile whose own `LIMIT` drops buckets.

    Found the apache `Scoreboard` tile silently missing its last 48 of 276 buckets: 22 series
    x 276 buckets is 6,072 rows against a `LIMIT 5000`. No value check can see it -- every
    bucket the tile DOES return is correct -- and no structural check can either, because the
    LIMIT is legitimate syntax. Only comparing the capped row count against the uncapped one
    reveals it.

    Scoped to queries that select a `ts` column, because for a top-N table or bar the LIMIT is
    the panel's own size and truncation is the POINT: three `[Logs System]` tiles carry
    `LIMIT 5`, and their Kibana panels specify `size: 5`. Flagging those would have produced
    three false positives and taught everyone to ignore the check.
    """
    print("\n== row caps: does any time-series tile truncate its own buckets? ==")
    flagged = 0
    for d in cs.call("clickstack_get_dashboard", {}):
        for t in cs.call("clickstack_get_dashboard", {"id": d["id"]})["tiles"]:
            sql = (t.get("config") or {}).get("sqlTemplate")
            if not sql:
                continue
            m = re.search(r"LIMIT\s+(\d+)", sql, re.I)
            if not m or " AS ts" not in sql:
                continue
            capped = ch(expand(sql))
            uncapped = ch(expand(re.sub(r"LIMIT\s+\d+", "LIMIT 100000000", sql, flags=re.I)))
            if "_error" in capped or "_error" in uncapped:
                continue
            na, nb = len(capped["data"]), len(uncapped["data"])
            if nb > na:
                flagged += 1
                RESULTS.add(False, t["name"], "row cap")
                print("    %-44s ** TRUNCATED: %d of %d rows (LIMIT %s) **"
                      % (t["name"][:44], na, nb, m.group(1)))
    if not flagged:
        print("    no time-series tile is truncated by its own LIMIT")
    return flagged


def report():
    ok = sum(1 for r in RESULTS if r[0])
    print("\n%d/%d tile series match Elastic bucket-for-bucket" % (ok, len(RESULTS)))
    for good, tile, alias in RESULTS:
        if not good:
            print("  FAILED: %s / %s" % (tile, alias))
    return 0 if ok == len(RESULTS) else 1
