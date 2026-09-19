#!/usr/bin/env python3
"""Derive the OTel collector configuration from the panels you intend to keep.

    python3 inventory-panels.py dashboards.ndjson --fields > fields.txt
    python3 plan-collector.py fields.txt
    python3 plan-collector.py fields.txt --yaml      # just the receivers: block

WHY THIS STEP EXISTS
--------------------
A dashboard migration is usually downstream of an ingestion migration: the customer moves
collection to the OTel collector, and only then does the target have anything to query. So the
procedure used to start one step too late. "Inventory the target" presumes someone has already
written a collector config -- and what that config must emit is decided by the panels you have
chosen to keep.

This inverts the usual direction. Instead of discovering mid-migration that a panel has no
data, you take the field list the panel inventory produced and answer, per field:

  * the receiver emits it with a DEFAULT config          -> nothing to do
  * the receiver has it but it is OFF by default         -> enable it (this is the common case)
  * several source fields collapse into one metric       -> the panel is a rewrite, not a rename
  * no receiver emits it                                 -> another receiver, or derive it
  * it is not a metric at all                            -> it becomes an attribute

The second bullet is the one that surprises people: **every hostmetrics `*.utilization` metric
is optional and the default is the absolute counter.** Elastic hands you percentages; OTel
makes percentages opt-in. A dashboard built on Elastic's `*.pct` fields therefore needs a
collector change or a ratio computed in the tile -- decided here, not discovered later.

The mapping lives in `receiver-map.json` beside this script, and its human-readable twin is
`../references/integration-to-receiver.md`. Both were read off each receiver's own
`documentation.md` / `metadata.yaml`; re-read rather than trust them, because receivers move.

Exit status is always 0. This plans, it does not gate.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
B, D, G, Y, R, X = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def read_fields(path):
    """One field per line. Tolerates `backticks`, '- ' bullets, blanks and comments."""
    text = sys.stdin.read() if path == "-" else open(path).read()
    out = []
    for line in text.splitlines():
        s = line.strip().lstrip("-*").strip().strip("`").strip()
        if not s or s.startswith("#") or " " in s or "." not in s:
            continue
        out.append(s)
    # de-duplicate, keep order
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fields", help="file of Elastic field names, or - for stdin")
    ap.add_argument("--map", default=os.path.join(HERE, "receiver-map.json"))
    ap.add_argument("--yaml", action="store_true",
                    help="print only the receivers: block, for pasting into a config")
    args = ap.parse_args(argv)

    doc = json.load(open(args.map))
    M, RCV = doc["fields"], doc["receivers"]
    fields = read_fields(args.fields)

    buckets = defaultdict(list)
    for f in fields:
        e = M.get(f)
        buckets[e["verdict"] if e else "unmapped"].append((f, e))

    # ---- the YAML the plan produces -------------------------------------------------
    # Optional metrics have to be switched on explicitly, and hostmetrics nests them per
    # scraper, so the two shapes are built separately.
    enable = defaultdict(set)          # receiver -> {metric}
    scraper_enable = defaultdict(lambda: defaultdict(set))   # hostmetrics -> scraper -> metrics
    for f, e in buckets["optional"]:
        if e.get("scraper"):
            scraper_enable[e["receiver"]][e["scraper"]].add(e["metric"])
        else:
            enable[e["receiver"]].add(e["metric"])
    scrapers_used = defaultdict(set)
    for verdict in ("default", "optional", "check"):
        for f, e in buckets[verdict]:
            if e.get("scraper"):
                scrapers_used[e["receiver"]].add(e["scraper"])

    yaml_lines = ["receivers:"]
    for rname in sorted({e["receiver"] for v in ("default", "optional", "check")
                         for _f, e in buckets[v] if e.get("receiver")}):
        comp = RCV.get(rname, {}).get("component", rname)
        yaml_lines.append("  %-14s# %s" % (rname + ":", comp))
        if rname in scrapers_used:
            yaml_lines.append("    scrapers:")
            for sc in sorted(scrapers_used[rname]):
                yaml_lines.append("      %s:" % sc)
                mets = sorted(scraper_enable.get(rname, {}).get(sc, ()))
                if mets:
                    yaml_lines.append("        metrics:")
                    for m in mets:
                        yaml_lines.append("          %s:" % m)
                        yaml_lines.append("            enabled: true")
        elif enable.get(rname):
            yaml_lines.append("    metrics:")
            for m in sorted(enable[rname]):
                yaml_lines.append("      %s:" % m)
                yaml_lines.append("        enabled: true")
    if buckets["absent"]:
        yaml_lines += [
            "  # Fields with no metric on the standard receivers need one more receiver.",
            "  # sqlqueryreceiver turns a SELECT into metrics -- one metric per row, with",
            "  # attribute_columns carrying the dimensions. Metrics support is ALPHA.",
            "  sqlquery:",
            "    driver: postgres            # or mysql, sqlserver, oracle, ...",
            "    datasource: \"...\"",
            "    queries:",
            "      - sql: \"SELECT ... FROM ...\"",
            "        metrics:",
            "          - metric_name: your.chosen.name",
            "            value_column: \"...\"",
            "            attribute_columns: [\"...\"]",
            "            data_type: sum      # or gauge",
            "            monotonic: true",
            "            aggregation: cumulative",
        ]

    if args.yaml:
        print("\n".join(yaml_lines))
        return 0

    # ---- the report ------------------------------------------------------------------
    print("%s%d field(s) from the panel inventory%s" % (B, len(fields), X))

    def section(title, key, colour=""):
        rows = buckets[key]
        if not rows:
            return
        print("\n%s%s%s  (%d)" % (colour or B, title, X, len(rows)))
        return rows

    if section("Emitted by a DEFAULT config — nothing to configure", "default", G):
        by_metric = defaultdict(list)
        for f, e in buckets["default"]:
            by_metric[(e["receiver"], e["metric"])].append((f, e))
        for (rcv, metric), items in sorted(by_metric.items()):
            if len(items) > 1:
                pairs = sorted({"%s=%s" % (k, v) for _f, e in items
                                for k, v in (e.get("attribute") or {}).items()})
                keys = sorted({k for _f, e in items
                               for k in (e.get("attribute") or {})})
                attrs = (", ".join(pairs) if len(pairs) <= 6
                         else "%s: %d values" % ("+".join(keys), len(pairs)))
                print("  %-34s <- %d fields  [%s]" % (metric, len(items), rcv))
                print("       %sreshaped: the dimension becomes an attribute (%s)%s"
                      % (D, attrs[:96], X))
            else:
                print("  %-34s <- %s  [%s]" % (metric, items[0][0], rcv))

    if section("OFF by default — enable these explicitly", "optional", Y):
        for f, e in sorted(buckets["optional"]):
            note = e.get("note")
            print("  %-46s -> %s" % (f, e["metric"]))
            if note:
                print("       %s%s%s" % (D, note, X))

    if section("No metric on the standard receiver", "absent", R):
        by_alt = defaultdict(list)
        for f, e in buckets["absent"]:
            by_alt[e.get("alternative", "no known alternative")].append((f, e))
        for alt, items in sorted(by_alt.items()):
            print("  %s%s%s" % (B, alt, X))
            for f, e in sorted(items):
                deg = e.get("degrades_to")
                print("    %s" % f)
                if deg:
                    print("       %sdegrades to %s%s" % (D, deg, X))

    if section("Derive in the tile — no metric needed", "derive"):
        for f, e in sorted(buckets["derive"]):
            print("  %-46s = %s" % (f, e.get("how", "")))

    if section("Semantics to confirm before trusting the mapping", "check", Y):
        for f, e in sorted(buckets["check"]):
            print("  %-46s -> %s" % (f, e.get("metric", "?")))
            print("       %s%s%s" % (D, e.get("note", ""), X))

    if section("Becomes an attribute, not a metric", "dimension"):
        for f, e in sorted(buckets["dimension"]):
            print("  %-46s %s" % (f, e.get("how", "")))

    if section("Not in the map — probably log fields, not collector metrics", "unmapped"):
        for f, _e in sorted(buckets["unmapped"])[:20]:
            print("  %s" % f)
        if len(buckets["unmapped"]) > 20:
            print("  ... and %d more" % (len(buckets["unmapped"]) - 20))
        print("  %sFor log fields the question is ECS -> LogAttributes, not which receiver:%s"
              % (D, X))
        print("  %ssee ../references/field-mapping.md%s" % (D, X))

    for rname, meta in sorted(RCV.items()):
        if meta.get("warning") and any(
                e.get("receiver") == rname for v in buckets.values() for _f, e in v if e):
            print("\n%swarning%s %s: %s" % (Y, X, rname, meta["warning"]))

    print("\n%sThe collector config this implies%s" % (B, X))
    print("%s(also available on its own with --yaml)%s" % (D, X))
    print("\n".join("  " + l for l in yaml_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
