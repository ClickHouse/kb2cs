#!/usr/bin/env python3
"""Self-asserting test for plan-collector.py.

    python3 scripts/tests/test-plan-collector.py

The other script in this directory (`make-fixture.py`) produces a fixture you then eyeball.
That is fine for a parser whose output is a table a human reads, but plan-collector's output
is a CLASSIFICATION, and a misclassification looks exactly like a correct one unless you
already know the answer. So this asserts instead.

It exercises, deliberately, one field per verdict plus the shapes that have caused trouble:

  * a **reshaped** family, to check several fields collapse onto one metric;
  * a field whose state is RENAMED (`iowait` -> `wait`), the kind of difference that silently
    returns nothing when a tile filters on the source's spelling;
  * both `process.cpu.utilization` variants, because choosing the wrong one is a 4-16x error;
  * a log field, which must be declined rather than guessed at;
  * the invariant that no field is left on an unresolved `check` verdict;
  * the input tolerances -- backticks, bullets, blanks, comments, duplicates -- because the
    real input is whatever `inventory-panels.py --fields` last printed.

Exits non-zero on failure.
"""
import importlib.util
import io
import json
import os
import re
import sys
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pc", os.path.join(HERE, "..", "plan-collector.py"))
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)
MAP = json.load(open(os.path.join(HERE, "..", "receiver-map.json")))

G, R, X = "\033[32m", "\033[31m", "\033[0m"
fails, ran = [], []


def check(desc, cond, detail=""):
    ran.append(desc)
    print("  %s%s%s  %s" % (G + "ok " + X if cond else R + "bad" + X, "", "", desc))
    if not cond:
        fails.append(desc)
        if detail:
            print("       %s" % detail)


FIXTURE = """
# a comment, and a blank line follow

- `nginx.stubstatus.requests`
nginx.stubstatus.active
nginx.stubstatus.reading
nginx.stubstatus.writing
nginx.stubstatus.waiting
nginx.stubstatus.dropped
nginx.stubstatus.requests
mysql.status.command.select
mysql.status.cache.ssl.hits
system.cpu.iowait.norm.pct
system.memory.used.bytes
system.network.name
process.cpu.pct
system.process.cpu.total.norm.pct
url.original
not a field at all
"""

path = os.path.join("/tmp", "pc-fixture.txt")
open(path, "w").write(FIXTURE)

fields = pc.read_fields(path)
check("input tolerances: backticks, bullets, blanks, comments and prose dropped",
      "nginx.stubstatus.requests" in fields and "not a field at all" not in fields,
      "got %r" % fields[:4])
check("duplicates removed, order preserved",
      fields.count("nginx.stubstatus.requests") == 1 and fields[0] == "nginx.stubstatus.requests")

M = MAP["fields"]
by = {}
for f in fields:
    by.setdefault(M[f]["verdict"] if f in M else "unmapped", []).append(f)

check("a default-config field is classified `default`",
      "nginx.stubstatus.requests" in by.get("default", []))
check("an off-by-default field is classified `optional`",
      "mysql.status.command.select" in by.get("optional", []))
check("a field with no receiver metric is `absent`",
      "mysql.status.cache.ssl.hits" in by.get("absent", []))
check("a computable field is `derive`",
      "nginx.stubstatus.dropped" in by.get("derive", []))
check("an attribute is `dimension`",
      "system.network.name" in by.get("dimension", []))
# `check` started at seven fields and is now empty: each was resolved by reading a
# metadata.yaml or, for the last two, by running hostmetricsreceiver against /proc/meminfo.
# The verdict stays in the vocabulary because the next addition may need it, but no field
# should silently acquire it -- an unresolved guess sitting in a customer-facing map is the
# thing this asserts against.
check("no field is left on an unconfirmed `check` verdict",
      not by.get("check"), "still unconfirmed: %r" % by.get("check"))
check("the `check` verdict remains available for future additions",
      "check" in MAP["_meta"]["verdicts"])
e_used = M["system.memory.used.bytes"]
how = e_used.get("how", "")
check("the measured memory finding is locked in, FORMULA included",
      e_used["verdict"] == "derive"
      and "MEASURED" in e_used.get("note", "")
      and "limit" in how and "free" in how
      # the wrong reconstruction is the whole point of the measurement: the receiver's
      # `cached` includes SReclaimable, so used+buffered+cached overshoots MemTotal-MemFree.
      and "buffered" not in how
      and M["system.memory.actual.used.bytes"]["attribute"]["state"] == "used",
      "used.bytes must derive from limit-free (not used+buffered+cached); "
      "actual.used maps to state=used. got how=%r" % how)
check("a log field is declined, not guessed",
      "url.original" in by.get("unmapped", []))

# reshaping: four nginx connection states must collapse onto ONE metric
states = {M[f]["metric"] for f in fields if f.startswith("nginx.stubstatus.")
          and f.endswith(("active", "reading", "writing", "waiting"))}
check("four connection-state fields collapse onto one metric",
      states == {"nginx.connections_current"}, "got %r" % states)

# the renamed state value must be recorded, not silently dropped
e = M["system.cpu.iowait.norm.pct"]
check("a RENAMED state value is recorded (iowait -> wait)",
      e.get("attribute", {}).get("state") == "wait" and "RENAMED" in (e.get("note") or ""),
      "got %r" % e)

# the normalisation trap must distinguish the two variants
v0, v1 = M["process.cpu.pct"], M["system.process.cpu.total.norm.pct"]
check("the two process.cpu.utilization variants are distinguished",
      v0["metric"] == "process.cpu.utilization"
      and v1["metric"] == "process.cpu.utilization@v1"
      and "normalis" in (v1.get("note") or "").lower(),
      "got %r / %r" % (v0.get("metric"), v1.get("metric")))

# the generated YAML must enable exactly the optional metrics, and nest hostmetrics
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pc.main([path, "--yaml"])
y = buf.getvalue()
check("--yaml enables the optional metric it found",
      "mysql.commands:" in y and "enabled: true" in y)
check("--yaml nests hostmetrics under scrapers:",
      re.search(r"hostmetrics:.*\n\s+scrapers:", y) is not None)
check("--yaml emits a sqlqueryreceiver stub when a field is absent",
      "sqlquery:" in y and "attribute_columns" in y)
check("--yaml does NOT enable a metric that is on by default",
      "nginx.requests:\n" not in y, "default metrics must not appear as enable stanzas")

# the full report must run clean on the same input
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pc.main([path])
check("the full report runs and exits 0", rc == 0)
check("the report names every verdict bucket it populated",
      all(w in buf.getvalue() for w in ("DEFAULT", "OFF by default", "No metric", "Derive")))


print()
if fails:
    print("%s%d of %d checks failed.%s" % (R, len(fails), len(ran), X))
    sys.exit(1)
print("%sAll %d checks passed.%s plan-collector classifies every verdict correctly."
      % (G, len(ran), X))
