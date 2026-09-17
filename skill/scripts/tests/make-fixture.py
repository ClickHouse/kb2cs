#!/usr/bin/env python3
"""Generate a synthetic Kibana saved-objects export exercising every panel shape
inventory-panels.py claims to handle, then print it as NDJSON.

    python3 make-fixture.py > fixture.ndjson
    python3 ../inventory-panels.py fixture.ndjson

Built programmatically because the interesting part of a real export is JSON nested
inside JSON strings, which is unreadable and error-prone to hand-write.

Covered: by-value Lens (terms+count, filters column, formula, referenced/pipeline op),
by-reference legacy visState pie, TSVB, saved search, map, links panel, an unrecognized
type, and a dashboard-level filter inherited by an unfiltered panel.

The by-value/by-reference split matters: real stock Kibana dashboards are entirely
by-value, so only this fixture exercises the by-reference resolution path.
"""
import json
import sys

S = json.dumps  # nested-JSON-as-string


def lens_panel(panel_index, title, viz_type, columns, column_order,
               series_type=None, query=None, filters=None):
    state = {
        "datasourceStates": {"formBased": {"layers": {
            "layer1": {"columns": columns, "columnOrder": column_order}}}},
        "visualization": ({"preferredSeriesType": series_type} if series_type else {}),
        "query": query or {"language": "kuery", "query": ""},
        "filters": filters or [],
    }
    return {
        "panelIndex": panel_index, "type": "lens",
        "gridData": {"x": 0, "y": 0, "w": 24, "h": 15, "i": panel_index},
        "embeddableConfig": {"attributes": {
            "title": title, "visualizationType": viz_type, "state": state}},
    }


def dataset_filter(dataset):
    return [{"meta": {"key": "data_stream.dataset", "negate": False,
                      "params": {"query": dataset}, "type": "phrase"},
             "query": {"match_phrase": {"data_stream.dataset": dataset}}}]


panels = []

# 1. Lens XY: date_histogram + count, scoped by a dataset filter.
panels.append(lens_panel(
    "p1", "Access logs over time", "lnsXY",
    {"c_date": {"operationType": "date_histogram", "sourceField": "@timestamp",
                "label": "@timestamp", "params": {"interval": "auto"}},
     "c_cnt": {"operationType": "count", "sourceField": "___records___",
               "label": "Count of records", "params": {}}},
    ["c_date", "c_cnt"], series_type="bar_stacked",
    filters=dataset_filter("nginx.access")))

# 2. Lens XY with a `filters` column -- Kibana's "status ranges as series".
panels.append(lens_panel(
    "p2", "Response codes over time", "lnsXY",
    {"c_f": {"operationType": "filters", "label": "Status", "params": {"filters": [
        {"input": {"language": "kuery", "query": "http.response.status_code >= 200 and http.response.status_code < 300"}},
        {"input": {"language": "kuery", "query": "http.response.status_code >= 500"}}]}},
     "c_cnt": {"operationType": "count", "sourceField": "___records___", "params": {}}},
    ["c_f", "c_cnt"], series_type="bar_stacked"))

# 3. Lens pie: terms on a UA field (index-time enrichment -- will not exist on target).
panels.append(lens_panel(
    "p3", "Browsers", "lnsPie",
    {"c_t": {"operationType": "terms", "sourceField": "user_agent.name",
             "label": "Top browsers", "params": {"size": 20, "orderBy": {"type": "column"}}},
     "c_cnt": {"operationType": "count", "sourceField": "___records___", "params": {}}},
    ["c_t", "c_cnt"]))

# 4. Lens metric with a formula (no sourceField at all).
panels.append(lens_panel(
    "p4", "Error rate", "lnsMetric",
    {"c_form": {"operationType": "formula", "label": "Error rate",
                "params": {"formula": "count(kql='http.response.status_code >= 500') / count()"}}},
    ["c_form"]))

# 5. Lens datatable: sum of bytes by path.
panels.append(lens_panel(
    "p5", "Data volume by page", "lnsDatatable",
    {"c_t": {"operationType": "terms", "sourceField": "url.path", "params": {"size": 10}},
     "c_sum": {"operationType": "sum", "sourceField": "http.response.body.bytes",
               "label": "Bytes"}},
    ["c_t", "c_sum"]))

# 5b. Lens with a referenced (pipeline) op: differences(of max(counter)). The base column
# has the sourceField; the differences column only has references -> both must be folded
# into one row without losing the field.
panels.append(lens_panel(
    "p5b", "Request rate", "lnsXY",
    {"c_date": {"operationType": "date_histogram", "sourceField": "@timestamp",
                "params": {"interval": "1h"}},
     "c_max": {"operationType": "max", "sourceField": "nginx.stubstatus.requests",
               "label": "Max requests", "params": {}},
     "c_diff": {"operationType": "differences", "label": "Request rate",
                "references": ["c_max"], "params": {}}},
    ["c_date", "c_max", "c_diff"], series_type="line"))

# 5c. Lens tag cloud -- degrades to a table on the target.
panels.append(lens_panel(
    "p5c", "Top clients", "lnsTagcloud",
    {"c_t": {"operationType": "terms", "sourceField": "source.address",
             "params": {"size": 25}},
     "c_cnt": {"operationType": "count", "sourceField": "___records___", "params": {}}},
    ["c_t", "c_cnt"]))

# 5d. BY-VALUE legacy visualization: definition lives in embeddableConfig.savedVis, NOT
# attributes.visState, and is already parsed rather than a JSON string.
panels.append({
    "panelIndex": "p5d", "type": "visualization",
    "gridData": {"x": 0, "y": 60, "w": 12, "h": 10, "i": "p5d"},
    "embeddableConfig": {"savedVis": {
        "type": "pie", "title": "Requests by method",
        "params": {},
        "data": {"aggs": [
            {"id": "1", "type": "count", "schema": "metric", "params": {}},
            {"id": "2", "type": "terms", "schema": "segment",
             "params": {"field": "http.request.method", "size": 8}}],
            "searchSource": {"query": {"language": "kuery", "query": "url.original:*"},
                             "filter": []}}}}})

# 5e. BY-VALUE markdown note, same savedVis shape. Prose, not data -- but still a tile.
panels.append({
    "panelIndex": "p5e", "type": "visualization",
    "gridData": {"x": 12, "y": 60, "w": 12, "h": 10, "i": "p5e"},
    "embeddableConfig": {"savedVis": {
        "type": "markdown", "title": "",
        "params": {"markdown": "## Host overview\n\nPick a host in the filter bar."},
        "data": {"aggs": [], "searchSource": {}}}}})

# 5f. TSVB in MARKDOWN MODE. savedVis.type is "metrics" but params.type is "markdown", so
# it renders text, and its [{type: count}] series is a vestigial default. Classifying it
# by savedVis.type alone migrates a text panel as a line chart of a meaningless count.
panels.append({
    "panelIndex": "p5f", "type": "visualization", "title": "Proxy",
    "gridData": {"x": 24, "y": 60, "w": 12, "h": 10, "i": "p5f"},
    "embeddableConfig": {"savedVis": {
        "type": "metrics", "title": "",
        "params": {"type": "markdown", "markdown": "### Proxy\nSee the [overview](#/dash).",
                   "series": [{"id": "s1", "metrics": [{"id": "m1", "type": "count"}]}]},
        "data": {"aggs": [], "searchSource": {}}}}})

# 6. By-reference legacy visualization (pie on log.level).
panels.append({"panelIndex": "p6", "type": "visualization", "panelRefName": "panel_6",
               "gridData": {"x": 0, "y": 15, "w": 12, "h": 15, "i": "p6"}})

# 7. By-reference TSVB.
panels.append({"panelIndex": "p7", "type": "visualization", "panelRefName": "panel_7",
               "gridData": {"x": 12, "y": 15, "w": 12, "h": 15, "i": "p7"}})

# 8. By-reference saved search.
panels.append({"panelIndex": "p8", "type": "search", "panelRefName": "panel_8",
               "gridData": {"x": 0, "y": 30, "w": 48, "h": 15, "i": "p8"}})

# 9. By-reference map -- the unmigratable one.
panels.append({"panelIndex": "p9", "type": "map", "panelRefName": "panel_9",
               "gridData": {"x": 0, "y": 45, "w": 24, "h": 15, "i": "p9"}})

# 10. Navigation panel -- must not be counted as a data panel.
panels.append({"panelIndex": "p10", "type": "links",
               "embeddableConfig": {"attributes": {"title": "Dashboards"}},
               "gridData": {"x": 24, "y": 45, "w": 8, "h": 8, "i": "p10"}})

# 11. Something the script does not know -- must surface, not vanish.
panels.append({"panelIndex": "p11", "type": "vega",
               "embeddableConfig": {"attributes": {"title": "Custom Vega",
                                                   "visState": S({"type": "vega"})}},
               "gridData": {"x": 32, "y": 45, "w": 16, "h": 8, "i": "p11"}})

objects = [
    {"id": "dash-overview", "type": "dashboard",
     "attributes": {
         "title": "[Logs Nginx] Overview",
         "panelsJSON": S(panels),
         # Dashboard-level filter, inherited by the panels that carry none of their own
         # (p3 Browsers, p4 Error rate, ...). A phrases filter over TWO datasets, which is
         # what the stock nginx dashboards use -- it must not be pasted onto a tile as-is.
         "kibanaSavedObjectMeta": {"searchSourceJSON": S(
             {"query": {"language": "kuery", "query": ""},
              "filter": [{"meta": {"key": "data_stream.dataset", "type": "phrases",
                                   "negate": False,
                                   "params": ["nginx.access", "nginx.error"]}}]})}},
     "references": [
         {"name": "panel_6", "type": "visualization", "id": "vis-levels"},
         {"name": "panel_7", "type": "visualization", "id": "vis-tsvb"},
         {"name": "panel_8", "type": "search", "id": "search-access"},
         {"name": "panel_9", "type": "map", "id": "map-geo"}]},

    {"id": "vis-levels", "type": "visualization",
     "attributes": {
         "title": "Errors by level",
         "visState": S({"title": "Errors by level", "type": "pie", "aggs": [
             {"id": "1", "type": "count", "schema": "metric", "params": {}},
             {"id": "2", "type": "terms", "schema": "segment",
              "params": {"field": "log.level", "size": 10, "orderBy": "1"}}]}),
         "kibanaSavedObjectMeta": {"searchSourceJSON": S(
             {"query": {"language": "kuery", "query": ""},
              "filter": dataset_filter("nginx.error")})}}},

    {"id": "vis-tsvb", "type": "visualization",
     "attributes": {
         "title": "p95 response size",
         "visState": S({"title": "p95 response size", "type": "metrics", "aggs": [],
                        "params": {"series": [{"label": "p95", "metrics": [
                            {"type": "percentile",
                             "field": "http.response.body.bytes"}]}]}}),
         "kibanaSavedObjectMeta": {"searchSourceJSON": S({})}}},

    {"id": "search-access", "type": "search",
     "attributes": {
         "title": "Nginx access logs",
         "columns": ["url.original", "http.request.method",
                     "http.response.status_code", "http.response.body.bytes"],
         "sort": [["@timestamp", "desc"]],
         "kibanaSavedObjectMeta": {"searchSourceJSON": S(
             {"query": {"language": "kuery", "query": "http.response.status_code >= 400"},
              "filter": dataset_filter("nginx.access")})}}},

    {"id": "map-geo", "type": "map",
     "attributes": {
         "title": "Nginx logs by location",
         "layerListJSON": S([{"sourceDescriptor": {"geoField": "source.geo.location",
                                                   "type": "ES_GEO_GRID"}}]),
         "kibanaSavedObjectMeta": {"searchSourceJSON": S({})}}},
]

for o in objects:
    print(json.dumps(o))
# real exports end with a summary line that is not a saved object
print(json.dumps({"excludedObjects": [], "excludedObjectsCount": 0,
                  "exportedCount": len(objects), "missingRefCount": 0,
                  "missingReferences": []}))
