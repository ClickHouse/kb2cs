#!/usr/bin/env python3
"""Assert that receiver-map.json and integration-to-receiver.md still agree.

    python3 scripts/tests/check-map-doc-sync.py

The same mapping is written twice on purpose: the JSON is what `plan-collector.py` reads, the
markdown is what a person reads, and neither can do the other's job. That is a drift risk with
nothing watching it -- rename a metric in one and the other quietly lies. This is the watcher.

It checks four things, chosen because each is a way the two have already almost diverged:

  1. every receiver metric named in the JSON appears somewhere in the markdown;
  2. every receiver component (`nginxreceiver`, ...) is mentioned in the markdown;
  3. the verdict vocabulary in the JSON's `_meta` matches the markdown's legend;
  4. the JSON's `verified` date appears in the markdown's status section, so a re-verification
     of one cannot silently leave the other claiming an older date.

It deliberately does NOT require the markdown to mention every FIELD. The markdown groups
fields into rows (`scoreboard.*` is one row covering eleven), and demanding a line per field
would force it to stop being readable -- which is the only reason it exists.

Exits non-zero on drift, so it can gate.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "..", "receiver-map.json")
DOC = os.path.join(HERE, "..", "..", "references", "integration-to-receiver.md")

G, R, X = "\033[32m", "\033[31m", "\033[0m"


def main():
    doc = json.load(open(MAP))
    md = open(DOC).read()
    fails = []

    def ok(msg):
        print("  %sok%s   %s" % (G, X, msg))

    def bad(msg, detail=""):
        print("  %sbad%s  %s" % (R, X, msg))
        if detail:
            print("       %s" % detail)
        fails.append(msg)

    # 1. metrics.
    #
    # The markdown legitimately writes families in shorthand -- `apache.load.{1,5,15}`,
    # `postgresql.tup_*` -- because a line per metric would stop it being readable, which is
    # the only reason it exists. So expand that shorthand rather than demanding verbosity.
    covered = set(re.findall(r"`([a-z][a-z0-9_.@]*)`", md))
    for tok in re.findall(r"`([a-z][a-z0-9_.@]*)\{([^}]*)\}`", md):
        stem, alts = tok
        covered.update(stem + a.strip() for a in alts.split(","))
    globs = [t[:-1] for t in re.findall(r"`([a-z][a-z0-9_.@]*\*)`", md)]

    def is_covered(m):
        return m in covered or m in md or any(m.startswith(g) for g in globs)

    metrics = sorted({e["metric"] for e in doc["fields"].values() if e.get("metric")})
    missing = [m for m in metrics if not is_covered(m)]
    if missing:
        bad("%d of %d receiver metrics are absent from the markdown" % (len(missing), len(metrics)),
            ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else ""))
    else:
        ok("all %d receiver metrics named in the JSON appear in the markdown" % len(metrics))

    # 2. receiver components
    comps = sorted({r["component"] for r in doc["receivers"].values() if r.get("component")})
    missing = [c for c in comps if c not in md]
    if missing:
        bad("receiver components missing from the markdown", ", ".join(missing))
    else:
        ok("all %d receiver components are mentioned" % len(comps))

    # 3. verdict vocabulary
    verdicts = set(doc["_meta"]["verdicts"])
    used = {e["verdict"] for e in doc["fields"].values()}
    undocumented = used - verdicts
    if undocumented:
        bad("verdicts used by fields but not documented in _meta", ", ".join(sorted(undocumented)))
    else:
        ok("every verdict in use is documented in _meta (%s)" % ", ".join(sorted(used)))
    # the markdown's own legend must name them too
    absent_from_md = sorted(v for v in verdicts if not re.search(r"\b%s\b" % re.escape(v), md))
    if absent_from_md:
        bad("verdicts documented in the JSON but never explained in the markdown",
            ", ".join(absent_from_md))
    else:
        ok("the markdown explains all %d verdicts" % len(verdicts))

    # 4. the verification date
    when = doc["_meta"].get("verified", "")
    if when and when in md:
        ok("both claim the same verification date (%s)" % when)
    else:
        bad("the JSON says verified %r but the markdown does not mention it" % when,
            "re-verifying one without the other is exactly the drift this guards")

    print()
    if fails:
        print("%s%d check(s) failed.%s receiver-map.json and integration-to-receiver.md "
              "have drifted." % (R, len(fails), X))
        return 1
    print("%sAll checks passed.%s The map and its documentation agree." % (G, X))
    return 0


if __name__ == "__main__":
    sys.exit(main())
