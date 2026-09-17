#!/usr/bin/env python3
"""Diff every migrated tile against Elasticsearch BUCKET FOR BUCKET.

    python3 verify/verify-tiles-vs-elastic.py             # all four integrations
    python3 verify/verify-tiles-vs-elastic.py nginx postgres   # a subset
    python3 verify/verify-tiles-vs-elastic.py --list           # what is covered

This is a different and stricter thing from the `verify-*.sh` verifiers, which assert totals
and structure. It exists because totals, whole-window averages and even a careful visual pass
all miss one class of error: a series that is the right shape and quietly wrong in every
bucket.

WHAT IT HAS FOUND
-----------------
  * **mysql** -- 42/42 matched, so nothing to fix. The harness was written here.
  * **nginx / apache metrics** -- 8 wrong series in two migrations that had already passed
    22/22 and been eyeballed. Builder GAUGE tiles cannot reproduce a Kibana panel that
    averages a gauge's samples inside a bucket, because HyperDX collapses a gauge to one
    sample per bucket (the last) before `aggFn` runs. `Active connections` disagreed in
    141/144 buckets, `Average server load` in 144/144 -- by ~1% (3.63 against 3.67), which is
    exactly why the charts looked right.
  * **nginx `Heartbeat / Up`** -- a builder `count_distinct` on a metric source returned 2 in
    2 of 277 buckets where all three hosts had reported ten points each, verified directly in
    ClickHouse. Same family as the gauge collapse: an aggFn on a metric source does not see
    the raw rows. Converted to SQL.
  * **apache `Scoreboard`** -- silently truncated by its own `LIMIT 5000`. 22 series x 276
    buckets is 6,072 rows, so the last 48 buckets of the chart were simply missing. Nothing
    numeric or structural can see that; only counting buckets can.

HOW IT VERIFIES, WHICH MATTERS AS MUCH AS WHAT
----------------------------------------------
  * **sql tiles** -- the stored `sqlTemplate` is read back from the live dashboard, its
    macros expanded, and executed. A hand-written "equivalent" re-encodes whatever
    misunderstanding produced the tile, and then agrees with it.
  * **builder tiles** -- re-issued through `clickstack_timeseries`, or compiled from the
    tile's own aggFn/where/groupBy when the tile is grouped, because that tool caps its
    result rows with no way to page (an nginx tile came back with 107 of ~1,100 rows).

Machinery lives in `tilediff.py`; each integration's expectations in `expect_<name>.py`.
Two comparison modes, and which one a series uses is a statement about the DATA, not a
convenience:

  * exact, to an ABSOLUTE tolerance derived from a storage quantum where one applies
    (`scaled_float` scaling_factor, single-precision doc_values);
  * conservation-checked, for log-derived series, because Elastic stores these events at
    second resolution -- all 499,964 nginx access documents have millisecond == 0 -- while
    the target keeps the millisecond, so events within a second of a bucket edge land in
    different buckets. That mode asserts no bucket moves by more than one second's worth of
    events and that the deltas sum to ~0, both bounds queried from the data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tilediff as td  # noqa: E402

INTEGRATIONS = ("mysql", "nginx", "apache", "postgres", "system")


def main(argv):
    if "--list" in argv:
        for name in INTEGRATIONS:
            mod = __import__("expect_" + name)
            print("\n%s -- %d tile(s) with expectations" % (name, len(mod.build())))
            for dash in mod.DASHBOARDS:
                print("    %s" % dash)
            for d in getattr(mod, "DIVERGENCES", []):
                print("    declared divergence: %s" % d)
        return 0

    want = [a for a in argv[1:] if not a.startswith("-")] or list(INTEGRATIONS)
    unknown = [w for w in want if w not in INTEGRATIONS]
    if unknown:
        print("unknown integration(s): %s\nknown: %s"
              % (", ".join(unknown), ", ".join(INTEGRATIONS)))
        return 2

    print("window %s -> %s  (derived from the data; override with "
          "TILEDIFF_WIN_START/END)" % (td.START, td.END))
    # Estate-wide preflight, not per-integration: a tile's row cap is a property of the tile,
    # and this found a real truncation that every other check in the repo passed over.
    td.check_row_caps()
    per = {}
    for name in want:
        mod = __import__("expect_" + name)
        before = len(td.RESULTS)
        print("\n" + "#" * 78)
        print("# %s" % name)
        print("#" * 78)
        td.run(mod.DASHBOARDS, mod.build())
        after = td.RESULTS[before:]
        per[name] = (sum(1 for r in after if r[0]), len(after))

    print("\n" + "=" * 78)
    for name in want:
        ok, tot = per[name]
        print("  %-10s %3d/%-3d series" % (name, ok, tot))
    rc = td.report()

    declared = [(n, d) for n in want
                for d in getattr(__import__("expect_" + n), "DIVERGENCES", [])]
    if declared:
        print("\nDeclared divergences, checked elsewhere rather than here:")
        for n, d in declared:
            print("  [%s] %s" % (n, d))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
