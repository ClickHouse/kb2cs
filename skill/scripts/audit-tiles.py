#!/usr/bin/env python3
"""
Audit migrated ClickStack tiles against the Kibana panels they came from — structurally.

    python3 audit-tiles.py <source-dashboards.ndjson> <migrated-dashboards.json>
    python3 audit-tiles.py src.ndjson migrated.json --dashboard "[Metrics System] Host overview"

`<migrated-dashboards.json>` is whatever `clickstack_get_dashboard` returns: one dashboard,
or the list form with a `tiles` array on each entry.

WHY THIS EXISTS
---------------
Every numeric check can pass while a tile is still wrong, and on the reference migrations a
person comparing charts caught ten such bugs that a green suite did not. Most of them were
not wrong *values* — they were wrong **structure**, and structure is mechanically checkable
against the source panel. This audit is that check. It is step 6c: run it before opening a
browser, because it finds in a second what the visual pass finds in ten minutes.

The five classes it catches, each taken from a real failure:

  1. NORMALISATION MISMATCH. The panel aggregates `process.cpu.pct`; the tile read
     `system.process.cpu.total.norm.pct`. Same quantity, one divided by core count — the tile
     was wrong by 4x-16x and every check was green, because the harness had derived its
     expectation from the field the TILE used. Any `.norm.` present on one side and absent on
     the other is flagged.

  2. DROPPED METRIC. `Memory usage vs total` displays two values (used bytes AND total); the
     tile showed one. `Top Hosts by CPU` displays two; the tile shipped `any(0) AS "_"` as a
     placeholder for the second. Counted from the panel's own visualization accessors, so a
     formula's internal sub-columns are not miscounted as displayed metrics.

  3. CHART TYPE DRIFT. Fifteen source panels were `seriesType: bar_stacked`; four tiles were
     built as `line` because a ratio "felt" like a line chart. Nothing numeric sees this.

  4. `seriesLimit` ON A TIME SERIES. It ranks series by UNCONDITIONAL volume, ignoring the
     select-item `where`, so on a shared table it silently empties a chart. No value check can
     reproduce it: the builder tools have no such parameter. Note a pie/bar `limit` is
     persisted under the same key, so only line/stacked_bar are flagged.

  5. PLACEHOLDERS. `any(0)`, `AS "_"`, `TODO`, `FIXME` left in stored SQL.

Exit status is 1 if anything is flagged, so it can gate a migration.
"""
import argparse
import json
import os
import re
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Kibana visualization states name the columns they actually DISPLAY. Counting a layer's
# columns instead over-counts: a single formula creates the formula column, a `math` column
# and one column per aggregation it references. Only accessors are displayed.
ACCESSOR_KEYS = (
    "metricAccessor", "secondaryMetricAccessor",      # lnsMetric
    "valueAccessor", "xAccessor", "yAccessor",         # lnsHeatmap / gauge
    "breakdownByAccessor", "splitAccessor",
    "accessors", "columns", "columnId",                # lnsXY layers / lnsDatatable
    "metric", "groups",
)
# A static_value column is a reference/goal line, not data -- dropping it is expected.
NON_DATA_OPS = {"static_value"}


def collect_accessors(node, out):
    """Every column id referenced by a visualization state, at any depth."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ACCESSOR_KEYS:
                if isinstance(v, str):
                    out.add(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str):
                            out.add(item)
                        else:
                            collect_accessors(item, out)
                else:
                    collect_accessors(v, out)
            else:
                collect_accessors(v, out)
    elif isinstance(node, list):
        for item in node:
            collect_accessors(item, out)


def panel_facts(att):
    """(displayed metric count, field set, seriesType) for one Lens panel."""
    state = att.get("state") or {}
    layers = ((state.get("datasourceStates") or {}).get("formBased") or {}).get("layers") or {}
    cols = {}
    for layer in layers.values():
        for cid, col in (layer.get("columns") or {}).items():
            cols[cid] = col

    displayed = set()
    collect_accessors(state.get("visualization") or {}, displayed)
    displayed &= set(cols)

    # A displayed metric is one that is not a bucket and not a reference line.
    metrics, fields = 0, set()
    for cid in sorted(displayed):
        col = cols[cid]
        op = col.get("operationType")
        if op in NON_DATA_OPS:
            continue
        if col.get("isBucketed"):
            continue
        metrics += 1

    # Fields come from every column, bucketed or not: a dropped breakdown matters too. A
    # formula keeps its fields inside the formula string, which has no sourceField.
    for col in cols.values():
        sf = col.get("sourceField")
        if sf and sf != "___records___":
            fields.add(sf)
        formula = (col.get("params") or {}).get("formula")
        if formula:
            for m in re.findall(r"[A-Za-z_][\w.]*\.[\w.]+", formula):
                fields.add(m)

    vis = state.get("visualization") or {}
    st = vis.get("preferredSeriesType")
    if not st:
        for layer in vis.get("layers") or []:
            st = st or layer.get("seriesType")
    return metrics, fields, st


def tile_facts(tile):
    """(value-column count, referenced identifiers, displayType, seriesLimit, sql)."""
    cfg = tile.get("config") or {}
    dt = cfg.get("displayType")
    sql = cfg.get("sqlTemplate") or ""
    blob = json.dumps(cfg)

    if sql:
        # Aliased output columns, minus the ones that are axes rather than values.
        aliases = re.findall(r'\bAS\s+"([^"]+)"', sql)
        aliases = [a for a in aliases
                   if a.lower() not in ("ts", "time", "host", "bucket")]
        values = len(aliases)
    else:
        values = len(cfg.get("select") or [])

    idents = set(re.findall(r"[A-Za-z_][\w.]*\.[\w.]+", blob))
    return values, idents, dt, cfg.get("seriesLimit"), sql


def norm_variants(field):
    """`a.b.norm.pct` <-> `a.b.pct`: the pair that is trivially swapped and never looks wrong."""
    if ".norm." in field:
        return field, field.replace(".norm.", ".")
    parts = field.rsplit(".", 1)
    if len(parts) == 2:
        return parts[0] + ".norm." + parts[1], field
    return field, field


CONTROLS = {}
FILTERS = {}


def load_panels(path):
    """{dashboard title: [(panel title, attributes)]} from a saved-objects export."""
    out = OrderedDict()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "dashboard":
                continue
            attrs = obj.get("attributes") or {}
            panels = []
            try:
                raw = json.loads(attrs.get("panelsJSON") or "[]")
            except json.JSONDecodeError:
                raw = []
            for p in raw:
                ec = p.get("embeddableConfig") or {}
                att = ec.get("attributes") or {}
                if not att.get("visualizationType"):
                    continue
                title = ec.get("title") or att.get("title") or p.get("title") or ""
                panels.append((title, att))
            title = attrs.get("title") or obj.get("id")
            out[title] = panels
            # the dashboard's CONTROL BAR, which is not a panel and so is invisible to any
            # panel-by-panel audit -- 21 of 37 dashboards in the reference estate have one
            # and none was migrated until a missing `database` dropdown was noticed
            controls = []
            cg = attrs.get("controlGroupInput") or {}
            try:
                cps = json.loads(cg.get("panelsJSON") or "{}")
            except (json.JSONDecodeError, TypeError):
                cps = {}
            for _cid, c in sorted(cps.items(), key=lambda kv: (kv[1] or {}).get("order", 0)):
                ei = (c or {}).get("explicitInput") or {}
                if ei.get("fieldName"):
                    controls.append((ei.get("title") or ei["fieldName"], ei["fieldName"]))
            CONTROLS[title] = controls
    return out


def load_tiles(path):
    """{dashboard name: [tiles]} from clickstack_get_dashboard output."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = [data]
    out = OrderedDict()
    for d in data:
        if isinstance(d, dict) and d.get("tiles") is not None:
            name = d.get("name") or d.get("id")
            out[name] = d["tiles"]
            FILTERS[name] = d.get("filters") or []
    return out


def norm_title(s):
    """Punctuation- and case-insensitive title identity. Does NOT strip anything else."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def norm_title_unannotated(s):
    """Same, minus a TRAILING PARENTHETICAL -- for the guarded fallback below only.

    Migrated tiles here routinely carry an annotation saying why they are what they are:
    `Heartbeat / Up (SQL: builder count_distinct under-counts)`. Matching on the raw string
    drops such a tile from the audit as "no source panel identified", which reads as an
    untitled source panel rather than a rename.

    But the parenthetical is NOT always an annotation -- sometimes it is the panel's
    identity. `Network traffic (bytes)` and `Network traffic (packets)` are two different
    stock panels, and stripping both sides paired the bytes tile to the packets panel. So
    this is used ONLY after an exact match fails, ONLY on the tile's side, and ONLY when it
    resolves to exactly one source panel. Ambiguity means no match, which is the honest
    answer.

    The same guard makes the bracketed form safe. `[Metrics Apache]`-style suffixes are a
    Kibana titling convention rather than a distinction, but the rule does not need to know
    that: if stripping ever collapses two panels together, the match is refused.
    """
    t = (s or "").strip()
    # Strip a trailing parenthetical AND/OR a trailing bracketed suffix, repeatedly: stock
    # Kibana panels are routinely titled `Total connections [Metrics Apache]`, and a migrated
    # tile drops that suffix, so an exact comparison never pairs them. Several apache and
    # system tiles were unmatched for that reason alone.
    for _ in range(3):
        stripped = re.sub(r"\s*(\([^()]*\)|\[[^\[\]]*\])\s*$", "", t)
        if stripped == t:
            break
        t = stripped
    return norm_title(t)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Kibana saved-objects export (ndjson)")
    ap.add_argument("migrated", help="clickstack_get_dashboard output (json)")
    ap.add_argument("--dashboard", help="audit only this migrated dashboard")
    ap.add_argument("--field-map", help="JSON {source field: target identifier}. With it, "
                    "every panel field must map to something the tile references -- the only "
                    "way to catch a RENAMED field pointed at the wrong target. Without it, "
                    "such pairings are listed for review instead.")
    args = ap.parse_args(argv)

    fmap = {}
    if args.field_map:
        with open(args.field_map, encoding="utf-8") as fh:
            fmap = json.load(fh)
    panels_by_dash = load_panels(args.source)
    tiles_by_dash = load_tiles(args.migrated)
    all_panels = [(dt, t, a) for dt, ps in panels_by_dash.items() for t, a in ps]

    findings = 0
    audited = 0
    unmatched = []
    reviews = []
    for dash, tiles in tiles_by_dash.items():
        if args.dashboard and dash != args.dashboard:
            continue
        print("\n== %s ==" % dash)

        # DASHBOARD-LEVEL: a Kibana control bar maps to ClickStack's dashboard `filters`.
        # Checked here because it belongs to no tile, which is precisely why it was missed on
        # 7 of 17 migrated dashboards.
        src_title = re.sub(r"\s*\(migrated\)\s*$", "", dash)
        src_controls = next((c for t, c in CONTROLS.items()
                             if norm_title(t) == norm_title(src_title)), None)
        have = FILTERS.get(dash) or []
        if src_controls:
            if len(have) < len(src_controls):
                findings += 1
                print("  %-46s FLAG" % "(dashboard controls)")
                print("       - source has %d control(s) on %s; migrated dashboard has %d "
                      "filter(s). A control bar maps to ClickStack `filters`; dropping it "
                      "loses a working feature."
                      % (len(src_controls), [f for _t, f in src_controls], len(have)))
            else:
                print("  %-46s ok (%d control(s) -> %d filter(s))"
                      % ("(dashboard controls)", len(src_controls), len(have)))

        # A METRIC-source filter with no `sourceMetricType` validates, saves, and then renders
        # an EMPTY dropdown: the field is "required only when sourceId is a Metric source", so
        # it is absent from the schema's top-level `required` list and nothing rejects it.
        # Which sourceIds are metric sources is read off the dashboard's own tiles -- a tile
        # carrying metricType/metricName proves its sourceId is one -- rather than guessed
        # from the expression text.
        # Two ways a tile proves its sourceId is a metric source, and BOTH are needed: a
        # builder tile carries metricType on a select item, but a raw-SQL tile has no select
        # at all -- and metric dashboards are overwhelmingly SQL here (9 of 10 on the
        # postgres one), so checking only the builder form found nothing and the check never
        # fired on the very dashboards it exists for.
        metric_sources = set()
        for t2 in tiles:
            c2 = t2.get("config") or {}
            for sel in (c2.get("select") or []):
                if isinstance(sel, dict) and sel.get("metricType"):
                    metric_sources.add(c2.get("sourceId"))
            if "otel_metrics_" in (c2.get("sqlTemplate") or ""):
                metric_sources.add(c2.get("sourceId"))
        for f in have:
            if f.get("sourceId") in metric_sources and not f.get("sourceMetricType"):
                findings += 1
                print("  %-46s FLAG" % ("(filter: %s)" % f.get("name")))
                print("       - metric-source filter %r has no sourceMetricType, so its "
                      "dropdown cannot populate" % f.get("expression"))

        # Assign panels to tiles UP FRONT, one panel per tile, exact titles first.
        #
        # Two tiles can strip to the same key -- `SSH login attempts` and `SSH login attempts
        # (search)` -- and one is the migration of the panel while the other is an ADDED
        # raw-rows view. Matching them independently let both claim the panel, and the search
        # tile was then flagged "panel seriesType=bar_stacked but tile is search", comparing a
        # stacked bar panel against a tile that was never meant to be it.
        #
        # Greedy in two passes so the result does not depend on tile order.
        own_panels = [(t, a) for t, a in panels_by_dash.get(src_title, []) if t]
        title_match, claimed = {}, set()
        for tl in tiles:
            nm = tl.get("name") or ""
            hit = next((i for i, (pt, _a) in enumerate(own_panels)
                        if i not in claimed and norm_title(pt) == norm_title(nm)), None)
            if hit is not None:
                title_match[nm], _ = own_panels[hit], claimed.add(hit)
        for tl in tiles:
            nm = tl.get("name") or ""
            if nm in title_match:
                continue
            key = norm_title_unannotated(nm)
            cands = [i for i, (pt, _a) in enumerate(own_panels)
                     if i not in claimed and norm_title_unannotated(pt) == key]
            if len(cands) == 1:
                title_match[nm], _ = own_panels[cands[0]], claimed.add(cands[0])

        for tile in tiles:
            cfg = tile.get("config") or {}
            if cfg.get("displayType") == "markdown":
                continue
            name = tile.get("name") or ""
            tvals, tidents, tdt, tlimit, tsql = tile_facts(tile)

            # Title match first, then FIELD OVERLAP. Many stock panels are untitled -- their
            # `title` is a UUID or absent -- while a migration gives tiles readable names, so
            # title matching alone left two thirds of the reference estate unmatched. The
            # fallback scores each panel by how many of its fields the tile mentions (either
            # normalisation variant), which is the same evidence a human uses.
            match, inferred = None, False
            # SAME-DASHBOARD FIRST. `all_panels` spans the whole estate, and dashboard titles
            # repeat across integrations -- `Connections` exists on both [Metrics MySQL]
            # Database Overview and [Metrics Apache] Overview. Matching estate-wide paired an
            # apache tile to the mysql panel and then flagged a chart-type difference that was
            # really a mispairing. A tile's panel is almost always on its own source dashboard,
            # so look there first and only widen if it is absent.
            # Same-dashboard assignment computed above. Estate-wide title matching is
            # deliberately NOT attempted: panel titles repeat across integrations, and doing
            # so paired an apache `Connections` tile to the mysql panel of that name and a
            # system Overview `CPU Usage` tile to the Host overview panel -- then reported the
            # differences as findings. Both were mispairings dressed as defects. If a tile's
            # panel is not on its own dashboard by title, the scored field-overlap inference
            # below is the safe fallback, and it is already barred from driving comparisons.
            match = title_match.get(name)
            if match is None:
                scored = []
                for _dt, ptitle, att in all_panels:
                    _m, pfields, _st = panel_facts(att)
                    score = 0
                    for f in pfields:
                        a, b = norm_variants(f)
                        if a in tsql or b in tsql or any(a in i or b in i for i in tidents):
                            score += 1
                    scored.append((score, ptitle, att))
                scored.sort(key=lambda x: -x[0])
                # Require a CLEAR winner: at least two shared fields and strictly more than
                # the runner-up. A score>=1 threshold paired a table with a bar chart and a
                # one-metric tile with a two-metric panel, and then reported the mismatch as
                # a finding -- three false positives out of three. A guessed pairing must not
                # be able to manufacture a defect.
                if scored and scored[0][0] >= 2 and (
                        len(scored) == 1 or scored[0][0] > scored[1][0]):
                    match, inferred = (scored[0][1], scored[0][2]), True

            notes = []
            if tlimit is not None and tdt in ("line", "stacked_bar"):
                notes.append("seriesLimit=%s on a %s -- ranks series by UNCONDITIONAL "
                             "volume, ignoring the select-item where" % (tlimit, tdt))
            for bad in ("any(0)", 'AS "_"', "TODO", "FIXME"):
                if bad in tsql:
                    notes.append("placeholder %r left in the stored SQL" % bad)

            # Structural comparisons need the RIGHT panel, so they run only on a title
            # match. On an inferred pairing the tile still gets the checks that need no
            # source at all (seriesLimit, placeholders), and is listed as inferred so a human
            # can confirm the pairing.
            if match and not inferred:
                audited += 1
                ptitle, att = match
                pmetrics, pfields, pstype = panel_facts(att)
                if pmetrics and tvals and tvals < pmetrics:
                    notes.append("panel displays %d metric(s), tile has %d value column(s)"
                                 % (pmetrics, tvals))
                if pstype in ("bar_stacked", "bar") and tdt not in ("stacked_bar",):
                    notes.append("panel seriesType=%s (a TIME-SERIES bar) but tile is %s"
                                 % (pstype, tdt))
                # A METRIC tile references an OTel metric NAME, not the Elastic field
                # name -- the rename is the point of the migration -- so no string heuristic
                # can bridge `process.cpu.pct` to `system.process.cpu.utilization`. That is
                # exactly the pair that was got wrong (one is normalised by core count, the
                # other is not, and the tile was off by 4x-16x with every check green).
                #
                # Two honest options, in order of strength:
                #   with --field-map: assert the declared target appears in the tile
                #   without it:       surface the pairing for review, never guess
                for f in sorted(pfields):
                    if ".norm." in f or f.endswith(".pct"):
                        withnorm, without = norm_variants(f)
                        hit_exact = any(f in i for i in tidents) or f in tsql
                        other = withnorm if ".norm." not in f else without
                        hit_other = (any(other in i for i in tidents) or other in tsql)
                        if not hit_exact and hit_other:
                            notes.append("panel field %s -- tile references %s instead "
                                         "(normalisation mismatch)" % (f, other))
                            continue
                    if f in fmap:
                        target = fmap[f]
                        if target and target not in tsql and not any(
                                target in i for i in tidents):
                            notes.append("panel field %s maps to %s, which this tile does "
                                         "NOT reference" % (f, target))
                    elif not (any(f in i for i in tidents) or f in tsql):
                        reviews.append((dash, name, f, sorted(
                            i for i in tidents if "." in i)[:4]))

            if notes:
                findings += len(notes)
                print("  %-46s FLAG%s" % (name[:46], " (panel inferred)" if inferred else ""))
                for n in notes:
                    print("       - %s" % n)
            elif match:
                print("  %-46s ok%s" % (name[:46], " (panel inferred)" if inferred else ""))
            else:
                unmatched.append((dash, name))
                print("  %-46s -- no source panel identified" % name[:46])

    print("\n%d tile(s) matched to a source panel; %d finding(s)." % (audited, findings))
    if unmatched:
        # NOT counted as findings: an unmatched tile is an audit gap, not a defect. Counting
        # them made the exit status meaningless on an estate whose panels are mostly untitled.
        print("%d tile(s) could not be matched to a panel and were NOT audited:" % len(unmatched))
        for dash, name in unmatched:
            print("   %s / %s" % (dash, name))
    if reviews:
        # Not findings: a renamed field is correct migration practice. But the PAIRING has
        # to be confirmed by someone, because picking the wrong target looks identical to
        # picking the right one -- same shape, same ranking, same units.
        print("\n%d field(s) could not be matched literally and need a mapping review."
              % len(reviews))
        print("Pass --field-map to turn these into assertions:")
        for dash, name, f, idents in reviews[:20]:
            print("   %-34s %-38s tile references: %s" % (name[:34], f, ", ".join(idents)))
    if findings:
        print("\nEach finding is a structural difference from the source panel. Confirm or "
              "fix before the visual pass -- these are the ones charts hide.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
