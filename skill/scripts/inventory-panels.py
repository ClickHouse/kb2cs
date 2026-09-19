#!/usr/bin/env python3
"""Inventory the panels in a Kibana saved-objects export.

    python3 inventory-panels.py dashboards.ndjson            # markdown tables
    python3 inventory-panels.py dashboards.ndjson --json     # machine-readable
    python3 inventory-panels.py dashboards.ndjson --fields   # just the distinct source fields
    python3 inventory-panels.py dashboards.ndjson --sources  # data views -> panels that read them
    python3 inventory-panels.py dashboards.ndjson --triage   # a ranked migration plan

Walks every dashboard's panelsJSON, resolves both by-value and by-reference panels, and
reports for each: chart type, the aggregations it performs, the fields those aggregations
read, the data views it reads, its filters, and a suggested ClickStack displayType (or
NOT MIGRATABLE).

Handles Lens (formBased and textBased), legacy visState aggs, TSVB, timelion, saved
searches, maps and markdown. Anything else reports type `unknown` with its raw keys, so an
unrecognized panel is visible rather than silently dropped.

The --fields list is the input to the field-mapping step: it is the complete set of source
fields the dashboards depend on, which is what you diff against the target schema.

--sources answers the question the tile schema forces on you: every tile needs a `sourceId`,
so each data view a dashboard reads has to be mapped to a ClickStack source before any tile
can be written. Pair it with `introspect-clickstack.py --sources` for the target side.

--triage turns the inventory into a plan: which panels are a direct translation, which need a
decision (and exactly which decision), and which cannot be migrated. It also names the
dashboard to migrate first -- the one that exercises the procedure with the fewest unknowns.
"""
import json
import re
import sys
from collections import OrderedDict

# Lens visualizationType / legacy visState type -> ClickStack displayType.
# None means there is no equivalent on the target (see SKILL.md step 3).
CHART_MAP = {
    # Lens
    "lnsXY": "line",            # refine to stacked_bar/bar from state.visualization
    "lnsPie": "pie",
    "lnsDatatable": "table",
    "lnsMetric": "number",
    "lnsLegacyMetric": "number",
    "lnsHeatmap": "heatmap",
    "lnsGauge": "number",       # no gauge on the target; degrades to a number tile
    # legacy visState
    "pie": "pie",
    "histogram": "stacked_bar",
    "horizontal_bar": "bar",
    "line": "line",
    "area": "line",             # no area type; renders as a line
    "table": "table",
    "metric": "number",
    "goal": "number",
    "gauge": "number",
    "heatmap": "heatmap",
    "tagcloud": "table",        # no tag cloud; a terms table carries the same data
    "metrics": "line",          # TSVB
    "timelion": "line",
    "markdown": "markdown",
    "lnsTagcloud": "table",     # no tag cloud on the target; a terms table carries the data
    # no equivalent
    "region_map": None,
    "coordinate_map": None,
    "maps": None,
    "map": None,
    "vega": None,               # arbitrary Vega spec; re-author by hand
    "input_control_vis": None,  # dashboard controls, not a data panel
}

# Panel types that are navigation/chrome, not data. Excluded from the panel count so a
# finished migration does not look incomplete.
NON_DATA_TYPES = {"links", "input_control_vis", "navigation"}

# TSVB (`type: "metrics"`) is a family, not a chart. Its inner `params.type` decides what
# it actually renders -- including `markdown`, which is a text panel with no data at all.
# All 11 TSVB panels in the stock system/kubernetes dashboards are markdown-mode.
TSVB_MAP = {
    "timeseries": "line",
    "metric": "number",
    "gauge": "number",
    "top_n": "bar",
    "table": "table",
    "markdown": "markdown",
}

# Lens's synthetic "count of records" placeholder. Not a real field, so it must not reach
# the field-mapping list -- there is nothing on the target to map it to; it is just count().
SYNTHETIC_FIELDS = {"___records___"}

# ---------------------------------------------------------------------------------------
# Triage inputs. These decide whether a panel is a direct translation or needs a decision,
# so each entry is a claim about the TARGET's capabilities -- re-check them with
# introspect-clickstack.py when the ClickStack version changes.
# ---------------------------------------------------------------------------------------

# Operations with no ClickStack `aggFn`. They are window functions over the grouped result,
# which in practice means a `sql` tile rather than a builder tile.
PIPELINE_OPS = {
    # Lens operationType
    "differences", "moving_average", "cumulative_sum", "counter_rate",
    "overall_sum", "overall_average", "overall_max", "overall_min", "normalize_by_unit",
    # legacy visState sibling/parent pipeline aggs
    "derivative", "serial_diff", "moving_avg", "cumulative_sum_bucket",
    "avg_bucket", "sum_bucket", "min_bucket", "max_bucket", "bucket_script",
}

# `quantile` on a builder tile accepts only these levels; anything else needs a sql tile.
QUANTILE_LEVELS = {"50", "90", "95", "99"}

# Panel types that DO have a target chart type but lose something on the way. The data
# survives; the form does not. Worth surfacing per panel so the loss list is not a surprise.
DEGRADING_CHARTS = {
    "lnsGauge": "no gauge tile -> number",
    "gauge": "no gauge tile -> number",
    "goal": "no goal tile -> number",
    "tagcloud": "no tag cloud -> table",
    "lnsTagcloud": "no tag cloud -> table",
    "area": "no area fill -> line",
}

TERMS_OPS = {"terms", "multi_terms", "significant_terms"}

# A Lens `formula` column has no `sourceField`: the fields it reads are written inside the
# formula STRING, e.g.
#   pick_max(normalize_by_unit(differences(max(postgresql.database.rows.fetched)), unit='s'), 0)
# A sourceField-only reader therefore reports a formula-heavy dashboard as depending on
# almost no fields -- measured on the stock PostgreSQL metrics dashboard: 9 fields found
# where the real surface is 25. Since --fields is the input to the field-mapping step, that
# under-report is the difference between mapping a dashboard and thinking you already have.
#
# Dotted identifiers are unambiguous here: every Lens formula function name is a bare word
# (pick_max, differences, normalize_by_unit, ...), so anything containing a dot is a field.
FORMULA_FIELD = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\b")


def formula_fields(text):
    """Field names referenced inside a Lens formula string."""
    if not text:
        return []
    return [f for f in dict.fromkeys(FORMULA_FIELD.findall(str(text)))
            if f not in SYNTHETIC_FIELDS]


def load(path):
    """NDJSON -> (objects by id, ordered dashboards). Drops the export summary line."""
    objs, dashboards = {}, []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                print(f"warning: line {lineno} is not JSON, skipped", file=sys.stderr)
                continue
            if "type" not in o or "id" not in o:
                continue  # export summary
            objs[o["id"]] = o
            if o["type"] == "dashboard":
                dashboards.append(o)
    return objs, dashboards


def jloads(value, default):
    """Kibana nests JSON inside JSON strings; tolerate both string and parsed forms."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def search_source(attrs):
    meta = attrs.get("kibanaSavedObjectMeta") or {}
    return jloads(meta.get("searchSourceJSON"), {})


def saved_vis(panel):
    """By-value legacy visualizations do NOT use `attributes.visState`.

    They store the whole definition under `embeddableConfig.savedVis`, already parsed
    (not a JSON string), in a differently-shaped object:

        savedVis = {type, title, params, data: {aggs, searchSource}}

    Reading only `visState` misses every one of them. Across the stock system, apache,
    mysql, kubernetes and nginx integrations that is 51 of 334 panels -- 40 markdown notes
    and 11 TSVB charts -- all reported as an unparsed `vis?`.

    Returns a visState-shaped dict so the normal visState path can consume it.
    """
    cfg = panel.get("embeddableConfig") or {}
    sv = cfg.get("savedVis") or ((cfg.get("attributes") or {}).get("savedVis"))
    if not isinstance(sv, dict):
        return None, {}
    data = sv.get("data") or {}
    return {
        "type": sv.get("type"),
        "title": sv.get("title"),
        "aggs": data.get("aggs") or [],
        "params": sv.get("params") or {},
    }, (data.get("searchSource") or {})


def dashboard_controls(attrs):
    """The dashboard's CONTROL BAR -- `controlGroupInput`, a row of dropdowns bound to fields
    that filter every panel.

    These are not panels, so a panel inventory skips them, and that is exactly how an entire
    migratable feature went missing: **21 of 37 dashboards in the reference estate carry one**
    and none was migrated until someone noticed a missing `database` dropdown. ClickStack's
    dashboard-level `filters` array is a direct equivalent, so this is a translation, not a
    degradation -- which makes silently dropping it worse, not better.

    Returns [(title, fieldName, singleSelect)].
    """
    out = []
    cg = attrs.get("controlGroupInput") or {}
    try:
        panels = json.loads(cg.get("panelsJSON") or "{}")
    except (json.JSONDecodeError, TypeError):
        return out
    for _cid, c in sorted(panels.items(), key=lambda kv: (kv[1] or {}).get("order", 0)):
        ei = (c or {}).get("explicitInput") or {}
        if ei.get("fieldName"):
            out.append((ei.get("title") or ei["fieldName"], ei["fieldName"],
                        bool(ei.get("singleSelect"))))
    return out


def render_filter(f):
    """Render one filter clause, recursing into Kibana's `combined` filters.

    A **combined** filter (`meta.type == "combined"`) holds a LIST of sub-filters in
    `meta.params`, joined by `meta.relation` -- OR or AND. Flattening it to its members
    loses the relation, and that is not cosmetic: reading an OR as an AND inverts the
    panel's meaning. On the stock `[Metrics System] Overview`, `Top Hosts by CPU` combines
    two `exists` filters with **OR**; ANDed they select nothing at all, because the two
    fields live in different data streams and never co-occur in one document. Flattened
    output made that panel look structurally broken when it works fine.

    Returns None for a disabled filter or one with nothing to say.
    """
    if not isinstance(f, dict):
        return None
    meta = f.get("meta") or {}
    if meta.get("disabled"):
        return None
    neg = "NOT " if meta.get("negate") else ""

    if meta.get("type") == "combined":
        rel = str(meta.get("relation") or "OR").upper()
        members = [render_filter(sub) for sub in (meta.get("params") or [])]
        members = [m for m in members if m]
        if not members:
            return None
        return "%s(%s)" % (neg, (" %s " % rel).join(members))

    key = meta.get("key") or ""
    # `exists` filters carry value == "exists", which reads as a literal term otherwise
    if meta.get("type") == "exists" and key:
        return "%s%s:*" % (neg, key)

    val = meta.get("value")
    if val is None:
        params = meta.get("params")
        if isinstance(params, dict):
            val = params.get("query")
        elif isinstance(params, list):
            # a `phrases` filter: one key, several accepted values
            vals = [str(p.get("query") if isinstance(p, dict) else p) for p in params]
            val = " or ".join(v for v in vals if v)
    if not val and isinstance(f.get("query"), dict):
        mp = f["query"].get("match_phrase") or {}
        if isinstance(mp, dict) and mp:
            k, v = next(iter(mp.items()))
            key = key or k
            val = v.get("query") if isinstance(v, dict) else v
    if key or val:
        return "%s%s:%s" % (neg, key, val) if key else "%s%s" % (neg, val)
    return None


def describe_filters(*sources):
    """Render KQL queries and filter clauses as readable strings.

    Every panel-scoping predicate must be carried onto the target tile, so these are
    reported even when they look like noise -- a dropped dataset filter double-counts.
    """
    out = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        q = src.get("query")
        if isinstance(q, dict) and q.get("query"):
            out.append(str(q["query"]).strip())
        elif isinstance(q, str) and q.strip():
            out.append(q.strip())
        for f in src.get("filter") or src.get("filters") or []:
            rendered = render_filter(f)
            if rendered:
                out.append(rendered)
    # de-dupe, keep order
    return list(OrderedDict.fromkeys(x for x in out if x))


def lens_aggs(state):
    """Lens -> [{op, field, label, detail}]. Reads formBased columns, then textBased."""
    aggs = []
    ds = state.get("datasourceStates") or {}
    form = ds.get("formBased") or ds.get("indexpattern") or {}
    extra_fields = []
    for layer in (form.get("layers") or {}).values():
        cols = layer.get("columns") or {}
        # columnOrder keeps the panel's own ordering; fall back to dict order
        order = [c for c in (layer.get("columnOrder") or []) if c in cols] or list(cols)

        # Referenced ("pipeline") operations -- differences, moving_average, counter_rate,
        # cumulative_sum -- have no sourceField. They carry `references: [columnId]`
        # pointing at the column they transform, and BOTH appear in columnOrder. Listing
        # them as two unrelated rows loses the one thing that matters for translation:
        # which metric is being differenced. Fold the base column into its parent.
        referenced = set()
        for cid in order:
            for ref in (cols[cid] or {}).get("references") or []:
                if ref in cols:
                    referenced.add(ref)

        for cid in order:
            if cid in referenced:
                continue
            col = cols[cid] or {}
            op = col.get("operationType") or "?"
            params = col.get("params") or {}
            refs = [r for r in (col.get("references") or []) if r in cols]
            base_of = ""
            if refs:
                base = cols[refs[0]] or {}
                base_of = f"of {base.get('operationType') or '?'}"
                # inherit the base column's field so it reaches the field-mapping list
                col = dict(col, sourceField=base.get("sourceField") or "")
            detail = []
            if base_of:
                detail.append(base_of)
            if op == "formula" or params.get("formula"):
                detail.append(f"formula={params.get('formula')}")
                # the formula's own field references, which have no sourceField
                extra_fields = formula_fields(params.get("formula"))
            if params.get("size"):
                detail.append(f"size={params['size']}")
            if params.get("interval"):
                detail.append(f"interval={params['interval']}")
            if params.get("percentile"):
                detail.append(f"p{params['percentile']}")
            for f in params.get("filters") or []:
                inp = (f or {}).get("input") or {}
                if inp.get("query"):
                    detail.append(f"filter[{inp['query']}]")
            field = col.get("sourceField") or ""
            aggs.append({
                "op": op,
                "field": "" if field in SYNTHETIC_FIELDS else field,
                "label": col.get("label") or "",
                "detail": "; ".join(str(d) for d in detail),
                "formula_fields": extra_fields,
            })
            extra_fields = []
    text = ds.get("textBased") or {}
    for layer in (text.get("layers") or {}).values():
        q = layer.get("query") or {}
        expr = q.get("esql") or q.get("sql") or ""
        if expr:
            aggs.append({"op": "esql/sql", "field": "", "label": "", "detail": expr.strip()})
    return aggs


def visstate_aggs(vis):
    """Legacy visState -> aggs. Handles TSVB (params.series) and timelion separately."""
    aggs = []
    for agg in vis.get("aggs") or []:
        if not isinstance(agg, dict):
            continue
        params = agg.get("params") or {}
        detail = []
        for k in ("size", "interval", "percents", "order", "orderBy"):
            if params.get(k):
                detail.append(f"{k}={params[k]}")
        for f in params.get("filters") or []:
            inp = (f or {}).get("input") or {}
            if inp.get("query"):
                detail.append(f"filter[{inp['query']}]")
        aggs.append({
            "op": agg.get("type") or "?",
            "field": params.get("field") or "",
            "label": params.get("customLabel") or agg.get("schema") or "",
            "detail": "; ".join(str(d) for d in detail),
        })
    params = vis.get("params") or {}
    for series in params.get("series") or []:                    # TSVB
        if not isinstance(series, dict):
            continue
        for m in series.get("metrics") or []:
            if not isinstance(m, dict):
                continue
            aggs.append({
                "op": m.get("type") or "?",
                "field": m.get("field") or "",
                "label": series.get("label") or "",
                "detail": "TSVB",
            })
    if params.get("expression"):                                  # timelion
        aggs.append({"op": "timelion", "field": "", "label": "",
                     "detail": str(params["expression"]).strip()})
    return aggs


def map_geo_fields(attrs):
    fields = []
    for layer in jloads(attrs.get("layerListJSON"), []) or []:
        if not isinstance(layer, dict):
            continue
        sd = layer.get("sourceDescriptor") or {}
        for key in ("geoField", "geo_field", "leftField"):
            if sd.get(key):
                fields.append(sd[key])
    return list(OrderedDict.fromkeys(fields))


def resolve(panel, dash_refs, objs):
    """A panel is stored by value or by reference.

    Returns (type, attributes, title, own_refs) -- `own_refs` being the REFERENCED object's
    own `references` array, which is where a by-reference panel records the data view it
    reads. Dropping it would make every by-reference panel look like it reads nothing.
    """
    ptype = panel.get("type") or ""
    cfg = panel.get("embeddableConfig") or {}
    attrs = cfg.get("attributes")
    own_refs = []

    if attrs is None:
        ref_name = panel.get("panelRefName")
        target = None
        if ref_name:
            for r in dash_refs:
                if r.get("name") == ref_name or r.get("name", "").endswith(ref_name):
                    target = objs.get(r.get("id"))
                    break
        if target is None and panel.get("id"):          # pre-7.x inline id
            target = objs.get(panel["id"])
        if target is not None:
            ptype = target.get("type") or ptype
            attrs = target.get("attributes") or {}
            own_refs = target.get("references") or []
    attrs = attrs or {}
    title = panel.get("title") or cfg.get("title") or attrs.get("title") or ""
    return ptype, attrs, title, own_refs


def data_view_info(objs, dv_id):
    """An index-pattern id -> {id, title, time_field, ...}. The id is used as the title when
    the index-pattern object itself was not included in the export, so a partial export still
    reports something usable rather than dropping the reference."""
    o = objs.get(dv_id) or {}
    a = o.get("attributes") or {}
    return {"id": dv_id,
            "title": a.get("title") or dv_id,
            "time_field": a.get("timeFieldName") or "",
            "adhoc": False,
            "runtime_fields": sorted(jloads(a.get("runtimeFieldMap"), {}) or {}),
            "resolved": bool(o)}


def lens_state(panel, attrs):
    """A Lens panel's `state`, whether the panel is stored by value or by reference."""
    at = (panel.get("embeddableConfig") or {}).get("attributes") or {}
    return at.get("state") or attrs.get("state") or {}


def adhoc_data_views(panel, attrs):
    """Data views defined INSIDE the panel rather than as saved objects.

    Lens calls these ad-hoc data views (`state.adHocDataViews`) and they are not a corner
    case: across the stock system/kubernetes/mysql/nginx/apache dashboards, **43 panels**
    use one, and those were every Lens panel whose data view looked unresolvable. A
    reference-only reader reports them as reading nothing at all.

    They matter for two reasons beyond resolution:

      * `runtimeFieldMap` -- Painless scripts evaluated at query time. 12 of those 43 carry
        one, and there is nothing on the target that runs Painless, so each has to be
        re-expressed as a ClickHouse expression. Query-time enrichment, not index-time.
      * the title can span clusters (`metrics-*,*:metrics-*`). The `<cluster>:` prefix is
        cross-cluster search, so the panel reads indices that are not even in this
        Elasticsearch -- a scope question to settle before choosing a target source.
    """
    out = []
    for dv_id, dv in (lens_state(panel, attrs).get("adHocDataViews") or {}).items():
        if not isinstance(dv, dict):
            continue
        out.append({
            "id": dv.get("id") or dv_id,
            "title": dv.get("title") or dv.get("name") or dv_id,
            "time_field": dv.get("timeFieldName") or "",
            "adhoc": True,
            "runtime_fields": sorted(dv.get("runtimeFieldMap") or {}),
            "resolved": True,
        })
    return out


def data_views(panel, attrs, own_refs, dash_refs, objs):
    """Every data view (index-pattern) a panel reads.

    Kibana records this in five different places depending on how the panel was saved, and
    a migration needs all of them -- each tile carries exactly one `sourceId`, so an
    unresolved data view is a tile that cannot be written. Measured against real exports:

      1. by-value Lens        embeddableConfig.attributes.references[] (type index-pattern)
      2. by-reference panels  the referenced object's own references[]
      3. legacy vis / search  kibanaSavedObjectMeta.searchSourceJSON.index (an id)
      4. by-value legacy vis  embeddableConfig.savedVis.data.searchSource.index
      5. maps                 layerListJSON[].sourceDescriptor.indexPatternId
      6. ad-hoc data views    state.adHocDataViews -- defined in the panel, no saved object

    Note that `indexPatternId` inside a Lens formBased layer is commonly null even when the
    panel plainly reads an index -- the reference array is the authoritative source, not the
    datasource state.
    """
    ids = []

    def add(v):
        if isinstance(v, str) and v and v not in ids:
            ids.append(v)

    cfg = panel.get("embeddableConfig") or {}
    at = cfg.get("attributes") or {}
    for r in list(at.get("references") or []) + list(own_refs or []):
        if isinstance(r, dict) and r.get("type") == "index-pattern":
            add(r.get("id"))

    for src in (search_source(attrs),
                ((cfg.get("savedVis") or {}).get("data") or {}).get("searchSource") or {}):
        if isinstance(src, dict):
            add(src.get("index"))

    # Dashboard-level references name panel-scoped ones "<panelIndex>:<ref-name>".
    pid = panel.get("panelIndex")
    if pid:
        for r in dash_refs or []:
            if (isinstance(r, dict) and r.get("type") == "index-pattern"
                    and str(r.get("name") or "").startswith(str(pid) + ":")):
                add(r.get("id"))

    for layer in jloads(attrs.get("layerListJSON"), []) or []:
        desc = (layer or {}).get("sourceDescriptor") or {}
        add(desc.get("indexPatternId"))

    adhoc = adhoc_data_views(panel, attrs)
    adhoc_ids = {d["id"] for d in adhoc}
    saved = [data_view_info(objs, i) for i in ids if i not in adhoc_ids]
    return saved + adhoc


def inspect(panel, dash_refs, objs):
    ptype, attrs, title, own_refs = resolve(panel, dash_refs, objs)
    row = {
        "panel_id": panel.get("panelIndex") or panel.get("panelRefName") or "",
        "title": title,
        "panel_type": ptype,
        "chart": "",
        "aggs": [],
        "filters": [],
        "columns": [],
        "data_views": data_views(panel, attrs, own_refs, dash_refs, objs),
        "grid": panel.get("gridData") or {},
        "note": "",
    }

    if ptype == "lens":
        row["chart"] = attrs.get("visualizationType") or "lens?"
        state = attrs.get("state") or {}
        row["aggs"] = lens_aggs(state)
        row["filters"] = describe_filters(state, search_source(attrs))
        vis = state.get("visualization") or {}
        # XY covers line/bar/area; the actual shape decides the target displayType
        seriestype = vis.get("preferredSeriesType") or ""
        if not seriestype:
            for l in vis.get("layers") or []:
                if isinstance(l, dict) and l.get("seriesType"):
                    seriestype = l["seriesType"]
                    break
        if seriestype:
            row["note"] = f"seriesType={seriestype}"
            # ClickStack's `bar` is CATEGORICAL (groupBy + limit, no time axis); its
            # time-bucketed bar chart is `stacked_bar`. An lnsXY is time-series unless the
            # series are horizontal, so a vertical `bar` seriesType maps to stacked_bar --
            # mapping it to `bar` produces a tile that fails schema validation for want of a
            # groupBy, or renders a categorical chart where the source had a time axis.
            if "bar_horizontal" in seriestype:
                row["chart_hint"] = "bar"
            elif "bar" in seriestype:
                row["chart_hint"] = "stacked_bar"

    elif ptype == "visualization":
        # by-reference -> attributes.visState (a JSON string)
        # by-value     -> embeddableConfig.savedVis (already parsed, different shape)
        vis = jloads(attrs.get("visState"), {})
        extra_src = {}
        if not vis:
            vis, extra_src = saved_vis(panel)
            vis = vis or {}
        row["chart"] = vis.get("type") or "vis?"
        row["title"] = row["title"] or vis.get("title") or ""
        row["aggs"] = visstate_aggs(vis)
        row["filters"] = describe_filters(search_source(attrs), extra_src)
        params = vis.get("params") or {}

        if row["chart"] == "metrics":
            # TSVB: resolve what it really renders, and say so in the chart column.
            # `params.type` is absent in older exports, where Kibana defaults to timeseries.
            inner = params.get("type") or "timeseries"
            row["chart"] = f"metrics/{inner}"
            row["chart_hint"] = TSVB_MAP.get(inner)
            if inner == "markdown":
                # a markdown-mode TSVB carries prose; its default [{type: count}] series
                # is vestigial, and reporting it as a count chart is simply wrong
                row["aggs"] = []

        text = params.get("markdown") or ""
        if text:
            # true for both a legacy markdown vis and a markdown-mode TSVB
            row["note"] = f"{len(text)} chars of markdown"

    elif ptype == "search":
        row["chart"] = "search"
        row["columns"] = attrs.get("columns") or []
        row["filters"] = describe_filters(search_source(attrs))
        sort = attrs.get("sort")
        if sort:
            row["note"] = f"sort={sort}"

    elif ptype == "map":
        row["chart"] = "map"
        geo = map_geo_fields(attrs)
        row["aggs"] = [{"op": "geo", "field": f, "label": "", "detail": ""} for f in geo]
        row["filters"] = describe_filters(search_source(attrs))

    elif ptype in NON_DATA_TYPES:
        row["chart"] = ptype
        row["note"] = "navigation/control panel, not data"

    elif ptype in CHART_MAP:
        # A known panel type that is simply not a Lens/visState/search/map object --
        # e.g. a top-level `vega` panel. Classify it rather than calling it unknown.
        row["chart"] = ptype
        row["filters"] = describe_filters(search_source(attrs))

    else:
        row["chart"] = "unknown"
        keys = sorted(set(list(attrs.keys()) + list(panel.keys())))
        row["note"] = "raw keys: " + ", ".join(keys[:12])

    # verdict
    if ptype in NON_DATA_TYPES:
        row["target"] = "n/a"
    elif row["chart"] == "search":
        row["target"] = "search"
    elif row["chart"] == "unknown":
        row["target"] = "UNKNOWN - inspect by hand"
    else:
        hint = row.get("chart_hint")
        mapped = CHART_MAP.get(row["chart"], "?")
        row["target"] = hint or (mapped if mapped else "NOT MIGRATABLE")
    return row


def collect(objs, dashboards):
    out = []
    for d in dashboards:
        attrs = d.get("attributes") or {}
        panels = jloads(attrs.get("panelsJSON"), [])
        dash = {
            "id": d.get("id"),
            "title": attrs.get("title") or "",
            "dashboard_filters": describe_filters(search_source(attrs)),
            "controls": dashboard_controls(attrs),
            "panels": [inspect(p, d.get("references") or [], objs)
                       for p in panels if isinstance(p, dict)],
        }
        out.append(dash)
    return out


def fmt_aggs(row):
    if row["columns"]:
        return "columns: " + ", ".join(row["columns"])
    parts = []
    for a in row["aggs"]:
        s = a["op"]
        if a["field"]:
            s += f"({a['field']})"
        if a["detail"]:
            s += f" [{a['detail']}]"
        parts.append(s)
    return "<br>".join(parts) if parts else "—"


def md_escape(s):
    return str(s).replace("|", "\\|")


def render_markdown(data):
    total = migratable = 0
    for dash in data:
        print(f"\n## {dash['title']}  \n`{dash['id']}`")
        if dash["dashboard_filters"]:
            print(f"\nDashboard-level filters: `{'` `'.join(dash['dashboard_filters'])}`")
        if dash.get("controls"):
            bits = ", ".join("%s -> `%s`%s" % (t, f, " (single-select)" if sgl else "")
                             for t, f, sgl in dash["controls"])
            print(f"\nDashboard controls (migrate to ClickStack dashboard `filters`): {bits}")
        print("\n| # | Panel | Source type | Aggregations / columns | Data view | Filters "
              "| Target displayType |")
        print("|---|---|---|---|---|---|---|")
        n = 0
        for row in dash["panels"]:
            if row["target"] == "n/a":
                continue
            n += 1
            total += 1
            if row["target"] not in ("NOT MIGRATABLE", "UNKNOWN - inspect by hand", "?"):
                migratable += 1
            filt = "`" + "` `".join(row["filters"]) + "`" if row["filters"] else "—"
            note = f" <br>_{row['note']}_" if row["note"] else ""
            dv = ", ".join("`%s`" % d["title"] for d in row["data_views"]) or "—"
            print("| {} | {} | {} | {} | {} | {} | **{}** |".format(
                n, md_escape(row["title"] or row["panel_id"]),
                md_escape(row["chart"]), md_escape(fmt_aggs(row)) + note,
                md_escape(dv), md_escape(filt), md_escape(row["target"])))
        skipped = [r for r in dash["panels"] if r["target"] == "n/a"]
        if skipped:
            print(f"\n{len(skipped)} non-data panel(s) excluded from the count: "
                  + ", ".join(f"`{r['chart']}`" for r in skipped))

    fields = distinct_fields(data)
    print(f"\n## Totals\n\n{total} data panel(s), {migratable} with a direct target chart type, "
          f"{total - migratable} needing a decision.")
    print(f"\n{len(fields)} distinct source field(s) to map:\n")
    for f in fields:
        print(f"- `{f}`")
    print("\nDiff this list against `clickstack_describe_source` on the target before "
          "translating any panel.")


def triage(row):
    """Classify one panel as ready | decide | blocked, with the reasons.

    `ready` means a direct translation exists -- not that it will be correct. Verification
    is still step 6; this only says nothing is known to stand in the way.
    """
    target = row["target"]
    if target == "n/a":
        return None, []
    if target == "NOT MIGRATABLE":
        return "blocked", ["no target chart type for `%s`" % row["chart"]]
    if target.startswith("UNKNOWN"):
        return "blocked", ["unrecognized panel type; inspect by hand"]

    why = []
    if row["chart"] in DEGRADING_CHARTS:
        why.append(DEGRADING_CHARTS[row["chart"]])
    if "seriesType=area" in (row.get("note") or ""):
        why.append(DEGRADING_CHARTS["area"])

    ops = [a["op"] for a in row["aggs"]]
    hit = sorted({o for o in ops if o in PIPELINE_OPS})
    dv_titles = " ".join(d["title"] for d in row.get("data_views") or []).lower()
    on_metrics = "metric" in dv_titles
    if hit:
        # `differences` is the one pipeline op with a builder answer -- but only on a metric
        # source, and only approximately. ClickStack de-cumulates a Sum before aggregating,
        # so differences(of max(counter)) becomes `increase` (fleet total) or `max` (biggest
        # single series); neither equals Kibana's Δ(max over series) once there are several.
        # Calling it a flat "sql tile" overstates the work; calling it `increase` understates
        # the disagreement. Say both.
        if on_metrics and hit == ["differences"]:
            why.append("differences on a metric source -> `increase`/`max`, but neither "
                       "equals Kibana's Δ(max over series) on multi-series data")
        else:
            why.append("pipeline op (%s) -> sql tile" % ", ".join(hit))
    # Two DIFFERENT metric-source traps, both measured on ClickStack 2.35.0-beta, and the
    # reason nearly every metric panel needs a decision:
    #
    #   Sums   -- de-cumulated before aggFn, so a panel charting a raw counter with
    #             max()/min() gets the per-bucket increase instead of the value.
    #   Gauges -- collapsed to ONE sample per bucket (the last) before aggFn, so
    #             avg/max/min/sum/last_value all return the same number, and none of them is
    #             Kibana's average()/max() over the samples in that bucket.
    #
    # `last_value()` is the one that survives: it is what a gauge tile computes anyway.
    # Everything else reading a metric's value needs a sql tile, whatever the field's type --
    # which is why this no longer tries to guess counter vs gauge from the aggregation.
    if on_metrics and not hit:
        intra = sorted({a["op"] for a in row["aggs"]
                        if a["op"] in ("max", "min", "avg", "average", "sum") and a["field"]})
        if intra:
            why.append("%s of a metric's value -> neither builder path reproduces it: a Sum "
                       "is de-cumulated first, and a Gauge is collapsed to one sample per "
                       "bucket before aggFn. Needs a sql tile"
                       % "/".join(intra))
        elif any(a["op"] == "last_value" and a["field"] for a in row["aggs"]):
            # Faithful as a builder tile IF the field ships as a Gauge. On a Sum,
            # last_value returns the bucket's increase rather than the value.
            why.append("last_value on a metric source -> builder-faithful only if the field "
                       "ships as a Gauge; on a Sum it returns the bucket increase")
    if "esql/sql" in ops:
        why.append("ES|QL/SQL query -> re-author as a sql tile")
    for a in row["aggs"]:
        if a["op"] == "formula" or "formula=" in (a["detail"] or ""):
            why.append("Lens formula -> re-express by hand")
            break
    for a in row["aggs"]:
        if a["op"] in ("percentile", "percentiles"):
            levels = re.findall(r"p([\d.]+)", a["detail"] or "")
            bad = [l for l in levels if l.rstrip("0").rstrip(".") not in
                   {x.rstrip("0").rstrip(".") for x in QUANTILE_LEVELS}]
            if bad or not levels:
                why.append("percentile %s outside quantile(0.5/0.9/0.95/0.99) -> sql tile"
                           % (", ".join("p" + b for b in bad) or "level unread"))
            break

    # Nested terms buckets do not survive as nesting -- see clickstack-tiles.md.
    nterms = sum(1 for a in row["aggs"] if a["op"] in TERMS_OPS)

    # ClickStack's `heatmap` is a VALUE-DISTRIBUTION heatmap: exactly one series, a numeric
    # `valueExpression` bucketed against time, and **no groupBy at all** (read off the live
    # save_dashboard schema). It does what a trace-latency heatmap does. It cannot do a
    # CATEGORICAL y-axis, so a Kibana heatmap broken down by `terms(host.name)` has no target
    # -- it degrades to a line chart or table with that field as the series.
    if target == "heatmap" and nterms >= 1:
        why.append("heatmap with a terms() breakdown -> ClickStack heatmaps take no groupBy "
                   "(value-distribution only); degrade to line/table grouped by that field")
    if nterms >= 2 and target == "pie":
        why.append("%d nested terms -> rings flatten to one slice per combination" % nterms)
    elif nterms >= 2 and target == "table":
        why.append("%d nested terms -> global top-N, not per-group top-N" % nterms)

    # A builder `table` tile has NO row limit: only line/stacked_bar take `seriesLimit` and
    # only pie/bar take `limit`. So a datatable with `terms(f) size=N` cannot be capped by any
    # builder -- it needs a sql tile with ORDER BY ... LIMIT N.
    if target == "table" and nterms >= 1 and any(
            re.search(r"size=\d+", a["detail"] or "") for a in row["aggs"]):
        why.append("terms size=N on a table tile -> `table` accepts no row limit "
                   "(only line/stacked_bar have seriesLimit, pie/bar have limit) -> sql tile")

    dvs = row.get("data_views") or []
    if len(dvs) > 1:
        why.append("reads %d data views (%s) -> one sourceId per tile, so split or pick"
                   % (len(dvs), ", ".join(d["title"] for d in dvs)))
    elif not dvs and target not in ("markdown",):
        why.append("no data view resolved -> confirm which source this should read")

    # Painless runtime fields are computed per query on the source and have no target
    # equivalent; each one becomes a ClickHouse expression written by hand.
    rt = sorted({f for d in dvs for f in d.get("runtime_fields") or []})
    if rt:
        why.append("%d runtime field(s) in Painless (%s) -> re-express as ClickHouse"
                   % (len(rt), ", ".join(rt[:4]) + (", …" if len(rt) > 4 else "")))

    # "<cluster>:<index>" is cross-cluster search: indices that are not in this
    # Elasticsearch at all, so the target may have no equivalent source.
    ccs = [d["title"] for d in dvs if re.search(r"(^|,)\s*[^,:\s]+:", d["title"] or "")]
    if ccs:
        why.append("cross-cluster pattern (%s) -> confirm the target holds that data"
                   % ", ".join(sorted(set(ccs))))

    return ("decide" if why else "ready"), why


def dashboard_triage(dash):
    buckets = OrderedDict((k, []) for k in ("ready", "decide", "blocked"))
    for row in dash["panels"]:
        b, why = triage(row)
        if b:
            buckets[b].append((row, why))
    return buckets


def render_triage(data):
    print("# Migration plan\n")
    plans = [(dash, dashboard_triage(dash)) for dash in data]

    print("| dashboard | panels | ready | decide | blocked |")
    print("|---|---:|---:|---:|---:|")
    tot = [0, 0, 0]
    for dash, b in plans:
        n = sum(len(v) for v in b.values())
        tot = [tot[0] + len(b["ready"]), tot[1] + len(b["decide"]), tot[2] + len(b["blocked"])]
        print("| %s | %d | %d | %d | %d |" % (md_escape(dash["title"]), n,
              len(b["ready"]), len(b["decide"]), len(b["blocked"])))
    print("| **total** | **%d** | **%d** | **%d** | **%d** |" % (sum(tot), *tot))

    # Recommend a first dashboard. "Smallest decision-free" is the obvious pick and a bad
    # one: a 2-panel dashboard of identical tiles exercises almost none of the procedure.
    # Rank instead by how many DISTINCT tile types it forces you to build, and prefer a logs
    # dashboard, because that is the signal the procedure has actually been executed on --
    # the metrics path is documented but unproven (see SKILL.md, Validation status).
    def signal(dash):
        titles = " ".join(d["title"] for row in dash["panels"]
                          for d in row["data_views"] or []).lower()
        if "log" in titles and "metric" not in titles:
            return "logs"
        if "metric" in titles:
            return "metrics"
        return "unknown"

    cands = []
    for dash, b in plans:
        n = sum(len(v) for v in b.values())
        if not n or b["decide"] or b["blocked"]:
            continue
        kinds = {row["target"] for row, _ in b["ready"]}
        cands.append((0 if signal(dash) == "logs" else 1, -len(kinds), n,
                      dash["title"], signal(dash), sorted(kinds)))
    if cands:
        cands.sort()
        _, negk, n, title, sig, kinds = cands[0]
        print("\n**Migrate first:** `%s` — %d panels, nothing needing a decision, and it "
              "exercises %d distinct tile type(s): %s."
              % (title, n, -negk, ", ".join("`%s`" % k for k in kinds)))
        if sig == "metrics":
            print("\n> Note: that is a **metrics** dashboard, and the metrics translation "
                  "path is documented but has never been executed (see SKILL.md, Validation "
                  "status). Expect to be the first to find its gaps. If a logs dashboard is "
                  "available, it is the safer first migration even if it needs a decision.")
    else:
        ranked = sorted(((len(b["decide"]) + len(b["blocked"]),
                          sum(len(v) for v in b.values()), dash["title"])
                         for dash, b in plans if sum(len(v) for v in b.values())))
        if ranked:
            print("\n**Migrate first:** `%s` — no dashboard is decision-free, and this one "
                  "has the fewest open questions (%d)." % (ranked[0][2], ranked[0][0]))
    print("\nThen verify it against the source before translating the rest: a rendered tile "
          "is not a verified tile, and `query_tiles` cannot see a chart that draws nothing.")
    print("\n`ready` means a direct translation exists — not that it will be correct. "
          "Every wrong tile in the reference migrations rendered perfectly.")

    for label, head in (("decide", "Needs a decision"), ("blocked", "Cannot be migrated")):
        rows = [(dash["title"], row, why) for dash, b in plans for row, why in b[label]]
        if not rows:
            continue
        print("\n## %s — %d panel(s)\n" % (head, len(rows)))
        print("| dashboard | panel | target | why |")
        print("|---|---|---|---|")
        for title, row, why in rows:
            print("| %s | %s | %s | %s |" % (
                md_escape(title), md_escape(row["title"] or row["panel_id"]),
                md_escape(row["target"]), md_escape("; ".join(why))))


def render_sources(data, objs):
    print("# Data views -> ClickStack sources\n")
    print("Every tile carries exactly one `sourceId`, so each data view below needs a target "
          "source before any tile can be written. Get the target side from "
          "`introspect-clickstack.py --sources`.\n")

    seen = OrderedDict()
    for dash in data:
        for row in dash["panels"]:
            if row["target"] == "n/a":
                continue
            dvs = row["data_views"]
            if not dvs:
                # A markdown/prose panel reads nothing, which is not the same as a data view
                # we failed to resolve. Conflating them invents a problem.
                placeholder = ("(none — prose panel)" if row["target"] == "markdown"
                               else "(unresolved)")
                dvs = [{"id": "", "title": placeholder, "time_field": "",
                        "resolved": row["target"] == "markdown", "adhoc": False}]
            for dv in dvs:
                e = seen.setdefault(dv["title"], {"time": dv["time_field"], "panels": 0,
                                                  "dashboards": set(), "fields": set(),
                                                  "resolved": dv.get("resolved", False),
                                                  "saved_n": 0, "adhoc_n": 0,
                                                  "runtime": set(), "id": dv["id"]})
                e["panels"] += 1
                e["runtime"].update(dv.get("runtime_fields") or [])
                if dv.get("adhoc"):
                    e["adhoc_n"] += 1
                elif dv.get("id"):
                    e["saved_n"] += 1
                e["dashboards"].add(dash["title"])
                for a in row["aggs"]:
                    if a["field"] and a["field"] not in SYNTHETIC_FIELDS:
                        e["fields"].add(a["field"])
                e["fields"].update(c for c in row["columns"] if c not in SYNTHETIC_FIELDS)

    print("| data view | kind | time field | panels | dashboards | fields | runtime fields |")
    print("|---|---|---|---:|---:|---:|---:|")
    for title, e in sorted(seen.items(), key=lambda kv: -kv[1]["panels"]):
        # A title can be BOTH: a saved data view and an ad-hoc one that happens to target
        # the same index pattern. Say so rather than picking one.
        bits = []
        if e["saved_n"]:
            bits.append("saved×%d" % e["saved_n"])
        if e["adhoc_n"]:
            bits.append("ad-hoc×%d" % e["adhoc_n"])
        kind = ", ".join(bits) or ("none" if e["resolved"] else "unresolved ⚠️")
        if re.search(r"(^|,)\s*[^,:\s]+:", title or ""):
            kind += ", cross-cluster"
        print("| `%s` | %s | `%s` | %d | %d | %d | %d |" % (
            md_escape(title), kind, e["time"] or "—",
            e["panels"], len(e["dashboards"]), len(e["fields"]), len(e["runtime"])))

    if any(not e["resolved"] and not e["saved_n"] and not e["adhoc_n"]
           for e in seen.values()):
        print("\n⚠️ = no data view could be resolved for these panels, and they are not prose "
              "panels. Re-export with `includeReferencesDeep`, or open one in Kibana and see "
              "what it reads.")
    if any(e["adhoc_n"] for e in seen.values()):
        print("\n**ad-hoc** = defined inside the panel (`state.adHocDataViews`), not a saved "
              "object. Just as real as a saved one, and just as much in need of a target "
              "source.")
    if any(e["runtime"] for e in seen.values()):
        print("\n**runtime fields** are Painless, evaluated per query. Nothing on the target "
              "runs Painless, so each becomes a ClickHouse expression written by hand — "
              "query-time enrichment, listed per panel by `--triage`.")

    print("\n## Per data view\n")
    for title, e in sorted(seen.items(), key=lambda kv: -kv[1]["panels"]):
        print("### `%s`\n" % title)
        print("- id: `%s`%s" % (e["id"] or "n/a",
              "  (ad-hoc in %d panel(s))" % e["adhoc_n"] if e["adhoc_n"] else ""))
        print("- time field: `%s`" % (e["time"] or "none — not a time-based view"))
        dl = sorted(e["dashboards"])
        print("- read by %d panel(s) across %d dashboard(s): %s%s" % (
            e["panels"], len(dl), ", ".join("`%s`" % d for d in dl[:6]),
            ", …" if len(dl) > 6 else ""))
        if e["runtime"]:
            print("- runtime fields to re-express: %s"
                  % ", ".join("`%s`" % f for f in sorted(e["runtime"])))
        if e["fields"]:
            fl = sorted(e["fields"])
            # Truncated on purpose: a metrics view can need 200 fields, and a 200-item
            # inline list is not something anyone reads. --fields prints them all.
            print("- %d field(s) the panels need, e.g. %s%s" % (
                len(fl), ", ".join("`%s`" % f for f in fl[:12]),
                " … (`--fields` prints the full list)" if len(fl) > 12 else ""))
        print()

    print("Pick the target source by matching on all three: **signal kind** (a `logs-*` view "
          "belongs on a log source, `metrics-*` on a metric source — the tile keys differ), "
          "**the fields above** (they must exist on the source; check `describe_source`), and "
          "**the time field** (it becomes the source's `timestampValueExpression`).\n")
    print("Two dashboards' worth of panels reading one data view can still land on different "
          "sources if the target split that data across tables — the mapping is per data "
          "view *per target table*, not per name.")


def distinct_fields(data):
    fields = set()
    for dash in data:
        for row in dash["panels"]:
            for a in row["aggs"]:
                if a["field"] and a["field"] not in SYNTHETIC_FIELDS:
                    fields.add(a["field"])
                fields.update(a.get("formula_fields") or [])
            fields.update(c for c in row["columns"] if c not in SYNTHETIC_FIELDS)
    return sorted(fields)


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 64
    path = argv[1]
    flags = set(argv[2:])
    objs, dashboards = load(path)
    if not dashboards:
        print(f"no dashboard objects in {path} — is this a saved-objects export?",
              file=sys.stderr)
        return 1
    data = collect(objs, dashboards)
    if "--json" in flags:
        json.dump(data, sys.stdout, indent=2)
        print()
    elif "--fields" in flags:
        for f in distinct_fields(data):
            print(f)
    elif "--sources" in flags:
        render_sources(data, objs)
    elif "--triage" in flags:
        render_triage(data)
    else:
        render_markdown(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
