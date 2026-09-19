#!/usr/bin/env python3
"""Assert ecs-to-clickhouse.py's schema decisions, which are where it can be quietly wrong.

No Elasticsearch and no ClickHouse: the parts worth testing are pure functions over a mapping
and a document. A wrong column TYPE is the failure mode that survives every later check --
the load succeeds, the tiles render, and a metric is silently truncated or a NULL is silently
a zero.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "ecs_loader", os.path.join(HERE, "..", "ecs-to-clickhouse.py"))
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

G, R, X = "\033[32m", "\033[31m", "\033[0m"
fails = []


def check(label, got, want):
    if got == want:
        print("  %sok%s   %s" % (G, X, label))
    else:
        print("  %sbad%s  %s\n         got  %r\n         want %r" % (R, X, label, got, want))
        fails.append(label)


# A mapping in the shape Elasticsearch returns one: nested `properties`, a multi-field, a
# scaled_float, and a type with no flat ClickHouse equivalent.
MAPPING = {
    "@timestamp": {"type": "date"},
    "host": {"properties": {"name": {"type": "keyword"}}},
    "event": {"properties": {"dataset": {"type": "constant_keyword"}}},
    "message": {"type": "match_only_text", "fields": {"keyword": {"type": "keyword"}}},
    "system": {"properties": {"cpu": {"properties": {
        "total": {"properties": {"norm": {"properties": {
            "pct": {"type": "scaled_float", "scaling_factor": 1000.0}}}}},
        "cores": {"type": "long"}}}}},
    "labels": {"type": "object", "dynamic": True},
    "geo": {"properties": {"location": {"type": "geo_point"}}},
}

flat = L.flatten_mapping(MAPPING)
check("flatten_mapping reaches every nested leaf with its dotted name",
      sorted(flat),
      ["@timestamp", "event.dataset", "geo.location", "host.name", "message",
       "system.cpu.cores", "system.cpu.total.norm.pct"])
check("a multi-field is NOT emitted as a second column", "message.keyword" in flat, False)
check("an `object` with no leaf type contributes nothing", "labels" in flat, False)

TS, PART = "@timestamp", "event.dataset"
types = {n: L.ch_type(t, n, TS, PART) for n, t in flat.items()}
check("the timestamp is a non-Nullable DateTime64(3), not Nullable(date)",
      types["@timestamp"], "DateTime64(3)")
check("the partition key is not Nullable -- a NULL group renders unlabelled",
      types["event.dataset"], "LowCardinality(String)")
check("a metric field IS Nullable -- absent is not zero",
      types["system.cpu.total.norm.pct"], "Nullable(Float64)")
check("scaled_float becomes Float64, not Decimal",
      types["system.cpu.total.norm.pct"], "Nullable(Float64)")
check("a long keeps its width rather than widening to Float64",
      types["system.cpu.cores"], "Nullable(Int64)")
check("a free-text field is String, not LowCardinality", types["message"], "String")
check("an unrepresentable type is reported rather than guessed",
      L.ch_type("dense_vector", "embedding", TS, PART), None)

# The document side. A Beats document fills its own metricset's fields and nothing else,
# which is the whole reason the views exist.
DOC = {"@timestamp": "2026-01-05T00:00:03.000Z",
       "host": {"name": "web-01"},
       "event": {"dataset": "system.cpu"},
       "system": {"cpu": {"total": {"norm": {"pct": 0.193}}, "cores": 8}},
       "tags": ["beats", "metrics"]}
fd = L.flatten_doc(DOC)
check("flatten_doc produces the same dotted names as the mapping",
      fd["system.cpu.total.norm.pct"], 0.193)
check("a list becomes JSON text rather than losing its other elements",
      fd["tags"], '["beats", "metrics"]')

check("a dataset value becomes a safe view name",
      L.view_name("ecs_events", "system.cpu"), "ecs_events_system_cpu")
check("and so does one with awkward characters",
      L.view_name("ecs_events", "aws.s3_daily-storage"), "ecs_events_aws_s3_daily_storage")

cols = {n: t for n, t in types.items() if t}
ddl = L.schema_sql(cols, "default", "ecs_events", [PART, TS])
check("the DDL orders by the partition key then time, so a per-dataset view seeks",
      "ORDER BY (`event.dataset`, `@timestamp`)" in ddl, True)
check("every column name is backticked -- a dotted identifier is not bare-safe",
      "`system.cpu.total.norm.pct` Nullable(Float64)" in ddl, True)

# A view must select only the columns its dataset fills. Selecting all of them would put the
# NULLs back, which is precisely what the view exists to remove.
datasets = {"system.cpu": {"system.cpu.total.norm.pct", "system.cpu.cores", "host.name"},
            "system.memory": {"system.memory.used.pct", "host.name"}}
cols2 = dict(cols, **{"system.memory.used.pct": "Nullable(Float64)"})
views = L.views_sql(cols2, "default", "ecs_events", datasets, TS, PART)
cpu_view = [b for b in views.split("CREATE OR REPLACE VIEW") if "system_cpu " in b][0]
check("a dataset's view omits another dataset's columns",
      "system.memory.used.pct" in cpu_view, False)
check("a dataset's view keeps its own", "system.cpu.total.norm.pct" in cpu_view, True)
check("a dataset's view is scoped by the partition predicate",
      "WHERE `event.dataset` = 'system.cpu'" in cpu_view, True)

srcs = L.sources_json("default", "ecs_events", datasets, TS, PART, True)
check("one source per view, not one for the wide table", len(srcs), 2)
check("they are log sources -- a metric source rejects a wide ECS document",
      {s["kind"] for s in srcs}, {"log"})
check("the timestamp expression is backticked into the source definition",
      srcs[0]["timestampValueExpression"], "`@timestamp`")
srcs = L.sources_json("default", "ecs_events", datasets, TS, PART, False)
check("--no-views gives exactly one source, the wide table",
      [s["tableName"] for s in srcs], ["ecs_events"])

print()
if fails:
    print("%s%d assertion(s) failed.%s" % (R, len(fails), X))
    sys.exit(1)
print("%sAll ecs-to-clickhouse assertions passed.%s" % (G, X))
