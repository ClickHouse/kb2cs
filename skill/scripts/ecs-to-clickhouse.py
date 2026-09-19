#!/usr/bin/env python3
"""Land ECS documents in ClickHouse with their own schema, and print the source to register.

For the case where the customer is KEEPING Filebeat/Metricbeat or Elastic Agent -- typically
for a proof of concept -- so ECS documents arrive rather than OTel ones. It reads an index's
`_mapping` and its documents, and emits everything needed to query them from ClickStack:

    export ES_URL=https://es.example.com:9200 ES_USER=svc_migration ES_PASSWORD=...

    # what would the table look like? (mapping only, no documents read)
    python3 ecs-to-clickhouse.py --schema-only 'metrics-system.*'

    # the whole thing, into ./out
    python3 ecs-to-clickhouse.py --out ./out 'metrics-system.*' 'logs-nginx.*'
    clickhouse-client --queries-file ./out/schema.sql
    clickhouse-client --query 'INSERT INTO default.ecs_events FORMAT JSONEachRow' < ./out/rows.jsonl
    clickhouse-client --queries-file ./out/views.sql          # one view per dataset
    #   ./out/sources.json  -- arguments for clickstack_save_source, one per view

This is a PROOF-OF-CONCEPT loader, not an ingestion pipeline: it pulls documents through
`_search` and writes files. For a real migration the routes are Vector or EDOT -- see
https://clickhouse.com/docs/use-cases/observability/clickstack/migration/elastic/migrating-agents

WHY IT EMITS VIEWS, AND WHY THAT IS THE POINT OF THE SCRIPT
-----------------------------------------------------------
Beats writes ONE DOCUMENT PER METRICSET, so a single wide table holding several metricsets is
mostly NULL -- and the target's builder aggregations turn a NULL into a zero that counts
(`AVG(toFloat64OrDefault(toString(col)))`). On a wide table `avg`, `min` and `last_value` are
then silently wrong, by a factor of however many rows share the group, while `max` and `sum`
look fine. Every one of them renders. `verify/verify-ecs-source.py` in the repo asserts this.

So the default output is one VIEW per dataset, each dense, each registered as its own source.
Tiles over those need no predicate discipline and every aggregation is faithful. Pass
`--no-views` to get only the wide table, and then put the dataset predicate on every single
tile instead.

`--partition-field` chooses what to split on; `event.dataset` is the Beats convention and
also what a data stream is named after, so a Kibana panel scoped to one data stream maps
one-to-one onto one view.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------- type mapping
# Elasticsearch field type -> ClickHouse column type. Two deliberate choices:
#
#   * numerics are Nullable, because a metric field is ABSENT from every document belonging to
#     another metricset, and absent is not zero. Making them non-Nullable would bake the
#     target's NULL-reads-as-zero behaviour into the data itself, where no predicate can
#     undo it.
#   * `scaled_float` becomes Float64, not Decimal. ES stores round(v*factor) and divides on
#     the way out, so what you can read back is a float either way; Decimal would imply a
#     precision the source does not have.
TYPES = {
    "long": "Nullable(Int64)",
    "integer": "Nullable(Int32)",
    "short": "Nullable(Int16)",
    "byte": "Nullable(Int8)",
    "unsigned_long": "Nullable(UInt64)",
    "double": "Nullable(Float64)",
    "float": "Nullable(Float64)",
    "half_float": "Nullable(Float64)",
    "scaled_float": "Nullable(Float64)",
    "boolean": "Nullable(Bool)",
    "date": "Nullable(DateTime64(3))",
    "date_nanos": "Nullable(DateTime64(9))",
    "ip": "Nullable(String)",
    "keyword": "LowCardinality(String)",
    "constant_keyword": "LowCardinality(String)",
    "wildcard": "String",
    "text": "String",
    "match_only_text": "String",
    "flattened": "String",
    "geo_point": "String",
    "version": "LowCardinality(String)",
}

# Columns that must never be Nullable, whatever the mapping says: the timestamp because it is
# the source's ordering key, and the grouping/partition keys because a NULL group renders as
# an unlabelled series. Beats always populates these.
def not_nullable(name, timestamp_field, partition_field):
    return name in (timestamp_field, partition_field)


def ch_type(es_type, name, timestamp_field, partition_field):
    """ClickHouse type for one mapped field, or None if it cannot be represented flatly."""
    if name == timestamp_field:
        return "DateTime64(3)"
    t = TYPES.get(es_type)
    if t is None:
        return None
    if not_nullable(name, timestamp_field, partition_field):
        return t.replace("Nullable(", "", 1).rstrip(")") if t.startswith("Nullable(") else t
    return t


def flatten_mapping(props, prefix=""):
    """Every leaf of an ES mapping as {dotted name: es type}.

    Multi-fields (`fields`) are deliberately skipped: `x` and `x.keyword` hold the same value
    with two analysers, which is an Elasticsearch query concern with no meaning on the target.
    Keeping both would double the column count for nothing.

    `object` and `nested` are skipped too. They carry a `type` like a real field does, so a
    reader that only looks for `type` emits them as leaves -- and they then show up in the
    unrepresentable-field warning, which should list fields the loader gave up on, not
    structural nodes that were never fields.
    """
    out = {}
    for key, val in (props or {}).items():
        name = prefix + key
        if "properties" in val:
            out.update(flatten_mapping(val["properties"], name + "."))
        elif val.get("type") in ("object", "nested"):
            continue
        elif "type" in val:
            out[name] = val["type"]
    return out


def flatten_doc(doc, prefix=""):
    """One `_source` as {dotted name: scalar}. Lists become JSON text, which is honest about
    them not being scalars rather than silently keeping the first element."""
    out = {}
    for key, val in doc.items():
        name = prefix + key
        if isinstance(val, dict):
            out.update(flatten_doc(val, name + "."))
        elif isinstance(val, list):
            out[name] = json.dumps(val)
        else:
            out[name] = val
    return out


def view_name(table, dataset):
    """A ClickHouse-safe view name for a dataset value like `system.cpu`."""
    return "%s_%s" % (table, re.sub(r"[^0-9a-zA-Z]+", "_", dataset).strip("_").lower())


def schema_sql(columns, db, table, order_by):
    cols = ",\n  ".join("`%s` %s" % (n, t) for n, t in columns.items())
    return ("CREATE TABLE IF NOT EXISTS %s.%s (\n  %s\n) ENGINE = MergeTree ORDER BY (%s);\n"
            % (db, table, cols, ", ".join("`%s`" % c for c in order_by)))


def views_sql(columns, db, table, datasets, timestamp_field, partition_field):
    """One dense view per dataset, selecting only the columns that dataset actually fills.

    Selecting every column would put the NULLs back and defeat the point."""
    out = []
    for dataset, present in sorted(datasets.items()):
        keep = [c for c in columns if c in present or c in (timestamp_field, partition_field)]
        out.append("CREATE OR REPLACE VIEW %s.%s AS\n  SELECT %s\n  FROM %s.%s"
                   "\n  WHERE `%s` = '%s';\n"
                   % (db, view_name(table, dataset),
                      ", ".join("`%s`" % c for c in keep), db, table,
                      partition_field, dataset))
    return "\n".join(out)


def sources_json(db, table, datasets, timestamp_field, partition_field, with_views):
    """`clickstack_save_source` arguments, one per view (or one for the wide table).

    `connection` is left as a placeholder because it is deployment-specific -- read it from
    `clickstack_list_sources`. Everything else is derivable, including the fact that these are
    `kind: log` sources: a `metric` source requires the narrow OTel metric tables and rejects
    a wide ECS document outright.
    """
    out = []
    targets = ([(view_name(table, d), d) for d in sorted(datasets)] if with_views
               else [(table, None)])
    for name, dataset in targets:
        out.append({
            "kind": "log",
            "name": "ECS %s" % (dataset or table),
            "connection": "<from clickstack_list_sources>",
            "databaseName": db,
            "tableName": name,
            "timestampValueExpression": "`%s`" % timestamp_field,
            "defaultTableSelectExpression": "`%s`, `%s`" % (timestamp_field, partition_field),
            "bodyExpression": "`%s`" % partition_field,
        })
    return out


# ---------------------------------------------------------------------------- Elasticsearch
def es_request(url, path, auth, body=None):
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": auth})
    return json.load(urllib.request.urlopen(req, timeout=300))


def auth_header(args):
    if args.api_key:
        return "ApiKey " + args.api_key
    if args.user:
        return "Basic " + base64.b64encode(
            ("%s:%s" % (args.user, args.password or "")).encode()).decode()
    sys.exit("no credentials: set ES_API_KEY, or ES_USER and ES_PASSWORD")


def scan(url, auth, index, limit, timestamp_field):
    """Documents from one index pattern, oldest first, via `search_after`.

    `search_after` rather than a scroll so nothing is left open server-side if this is
    interrupted, and so the order is stable across calls.
    """
    after, seen = None, 0
    while limit is None or seen < limit:
        size = 5000 if limit is None else min(5000, limit - seen)
        body = {"size": size, "query": {"match_all": {}},
                "sort": [{timestamp_field: "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es_request(url, "/%s/_search" % index, auth, body)["hits"]["hits"]
        if not hits:
            return
        for hit in hits:
            yield hit["_source"]
        seen += len(hits)
        after = hits[-1]["sort"]


def main():
    ap = argparse.ArgumentParser(
        description="Land ECS documents in ClickHouse with their own schema.",
        epilog="Credentials come from ES_URL, ES_API_KEY, or ES_USER + ES_PASSWORD.")
    ap.add_argument("index", nargs="+", help="index or data-stream patterns to read")
    ap.add_argument("--url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    ap.add_argument("--api-key", default=os.environ.get("ES_API_KEY"))
    ap.add_argument("--user", default=os.environ.get("ES_USER"))
    ap.add_argument("--password", default=os.environ.get("ES_PASSWORD"))
    ap.add_argument("--database", default=os.environ.get("CLICKHOUSE_DATABASE", "default"))
    ap.add_argument("--table", default="ecs_events")
    ap.add_argument("--timestamp-field", default="@timestamp")
    ap.add_argument("--partition-field", default="event.dataset",
                    help="what the per-dataset views split on (default event.dataset)")
    ap.add_argument("--limit", type=int, help="stop after N documents per index pattern")
    ap.add_argument("--no-views", action="store_true",
                    help="emit only the wide table -- then EVERY tile needs the predicate")
    ap.add_argument("--schema-only", action="store_true",
                    help="read the mapping, print the DDL, read no documents")
    ap.add_argument("--out", default=".", help="directory for the emitted files")
    args = ap.parse_args()

    auth = auth_header(args)
    ts, part = args.timestamp_field, args.partition_field

    # The MAPPING decides the columns, not the sampled documents: a field absent from the
    # first 5,000 documents is still a field, and a column discovered late would mean
    # rewriting the DDL after the data was already emitted.
    mapped = {}
    for pattern in args.index:
        for idx in es_request(args.url, "/%s/_mapping" % pattern, auth).values():
            mapped.update(flatten_mapping(idx.get("mappings", {}).get("properties", {})))

    columns, skipped = {}, {}
    for name, es_t in sorted(mapped.items()):
        t = ch_type(es_t, name, ts, part)
        if t is None:
            skipped[name] = es_t
        else:
            columns[name] = t
    if ts not in columns:
        sys.exit("no `%s` in the mapping -- pass --timestamp-field" % ts)
    if part not in columns:
        print("warning: no `%s` in the mapping; views will be skipped" % part, file=sys.stderr)

    order_by = [c for c in (part, ts) if c in columns]
    ddl = schema_sql(columns, args.database, args.table, order_by)
    print("%d columns from %d mapped fields" % (len(columns), len(mapped)), file=sys.stderr)
    if skipped:
        print("skipped %d field(s) with no flat ClickHouse equivalent: %s"
              % (len(skipped), ", ".join(sorted(skipped))), file=sys.stderr)
    if args.schema_only:
        print(ddl)
        return

    os.makedirs(args.out, exist_ok=True)
    datasets, total = {}, 0
    with open(os.path.join(args.out, "rows.jsonl"), "w") as fh:
        for pattern in args.index:
            for src in scan(args.url, auth, pattern, args.limit, ts):
                flat = flatten_doc(src)
                row = {k: v for k, v in flat.items() if k in columns}
                if ts in row and isinstance(row[ts], str):
                    # ClickHouse's DateTime64 parser wants a space, not the ISO 'T', and no
                    # trailing Z. Keeping the sub-second part matters: Elastic truncating
                    # these to the second is what makes some log series compare only as
                    # conservation checks rather than equality.
                    row[ts] = row[ts].replace("T", " ").rstrip("Z")
                fh.write(json.dumps(row) + "\n")
                total += 1
                if part in flat:
                    datasets.setdefault(flat[part], set()).update(
                        k for k, v in row.items() if v is not None)
    print("%d documents -> %s/rows.jsonl" % (total, args.out), file=sys.stderr)

    with open(os.path.join(args.out, "schema.sql"), "w") as fh:
        fh.write(ddl)
    with_views = bool(datasets) and not args.no_views
    if with_views:
        with open(os.path.join(args.out, "views.sql"), "w") as fh:
            fh.write(views_sql(columns, args.database, args.table, datasets, ts, part))
        print("%d dataset(s) -> %s/views.sql: %s"
              % (len(datasets), args.out, ", ".join(sorted(datasets))), file=sys.stderr)
    with open(os.path.join(args.out, "sources.json"), "w") as fh:
        json.dump(sources_json(args.database, args.table, datasets, ts, part, with_views),
                  fh, indent=2)
    if not with_views:
        print("--no-views: every tile must carry `%s` = '<dataset>' or its averages will be "
              "diluted" % part, file=sys.stderr)


if __name__ == "__main__":
    main()
