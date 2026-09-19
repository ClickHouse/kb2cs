#!/usr/bin/env python3
"""
Generate the nginx `stub_status` metric series that the Elastic `nginx` integration's
[Metrics Nginx] Overview dashboard is built on.

    data/metrics/nginx-stubstatus.jsonl   one scrape per line, per node

Unlike the two log datasets this is NOT an independent simulation: every counter is
**derived from data/access.json.log**, so the metrics and the logs describe the same traffic.
That is the whole point. It buys a verification invariant no synthetic series can offer:

    sum of per-bucket increases in nginx.stubstatus.requests  ==  499,964 access log lines

which is exactly what `differences(of max(requests))` on the source dashboard sums to, and
exactly what `aggFn: "increase"` must reproduce on the target. A migrated tile that is wrong
by a scrape or a bucket edge cannot hide behind "well, it is synthetic".

What is derived vs modelled
---------------------------
Derived exactly, per node per scrape interval, by reading the JSON access log:

  requests  cumulative count of log lines            (one line == one request served)
  accepts   cumulative count of NEW connections      (lines where connection_requests == 1)
  handled   accepts minus the few that were dropped
  dropped   accepts - handled; nginx only drops on resource limits, so this stays ~0

Modelled, because a log records completed requests and says nothing direct about what was
in flight at scrape time:

  writing   concurrency: sum(request_time) over the interval / interval seconds
  reading   a small fraction of new connections (headers arrive fast)
  waiting   idle keep-alive connections still open
  active    reading + writing + waiting, which is how stub_status defines it
  current   == requests, matching what metricbeat reports for this field

The integration's own mapping is what fixed the counter/gauge split above:
`accepts`/`dropped`/`handled`/`requests` are `time_series_metric: counter`, and
`active`/`current`/`reading`/`waiting`/`writing` are `gauge`. On ClickStack that is the
difference between an OTel Sum and an OTel Gauge -- a different table and a different
`metricType` on every tile -- so it is not a detail to guess at.

Deterministic: derived from a fixed corpus, single seed for the modelled part, no wall-clock
reads. Timestamps stay on the corpus's own day (2026-08-17); both loaders shift them the same
way the log loaders do, so metrics and logs land on one clock.

Usage:  python3 generate-nginx-metrics.py [--interval 30] [--out ../data/metrics]
"""

import argparse
import json
import os
import random
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402  -- DAY, NGINX_NODES

SEED = 20260917

# nginx counters do not start at zero: the worker has been up for a while. Give each node its
# own base so a tile that accidentally sums across nodes is obviously wrong rather than
# plausibly wrong.
COUNTER_BASE = {node: (i + 1) * 4_000_000 for i, node in enumerate(ng.NGINX_NODES)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30,
                    help="scrape interval in seconds (default 30)")
    ap.add_argument("--src", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "access.json.log"))
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "metrics"))
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    if not os.path.exists(src):
        sys.exit("missing %s -- run generate.py first" % src)

    iv = args.interval
    nbins = 86400 // iv
    rng = random.Random(SEED)

    # ---------------------------------------------------------------- read the access log
    # bins[node][i] = [requests, new_connections, summed request_time]
    bins = {n: [[0, 0, 0.0] for _ in range(nbins)] for n in ng.NGINX_NODES}
    day0 = int(ng.DAY.timestamp())
    total_lines = 0
    print("reading %s ..." % os.path.basename(src))
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            node = e.get("hostname")
            b = bins.get(node)
            if b is None:
                continue
            total_lines += 1
            i = int((float(e["msec"]) - day0) // iv)
            if not 0 <= i < nbins:
                continue
            slot = b[i]
            slot[0] += 1
            if str(e.get("connection_requests")) == "1":
                slot[1] += 1
            rt = e.get("request_time")
            try:
                slot[2] += float(rt)
            except (TypeError, ValueError):
                pass
    print("  %s requests across %d nodes" % ("{:,}".format(total_lines), len(bins)))

    # ---------------------------------------------------------------- emit the scrapes
    path = os.path.join(outdir, "nginx-stubstatus.jsonl")
    counters = {n: dict(requests=COUNTER_BASE[n], accepts=COUNTER_BASE[n],
                        handled=COUNTER_BASE[n], dropped=0) for n in ng.NGINX_NODES}
    written = 0
    checksum = {n: 0 for n in ng.NGINX_NODES}

    with open(path, "w", encoding="utf-8", newline="\n") as out:
        for i in range(nbins):
            # scrape at the END of the interval: the counters it reports include everything
            # that completed during it, which is what makes a bucket delta comparable to a
            # line count over the same window
            ts = ng.DAY + timedelta(seconds=(i + 1) * iv)
            for node in ng.NGINX_NODES:
                reqs, newconn, rtsum = bins[node][i]
                c = counters[node]

                # nginx drops a connection only when it cannot allocate one; rare, bursty.
                dropped_now = 1 if (newconn and rng.random() < 0.0008) else 0

                c["requests"] += reqs
                c["accepts"] += newconn
                c["handled"] += newconn - dropped_now
                c["dropped"] += dropped_now
                checksum[node] += reqs

                writing = int(round(rtsum / iv))
                reading = int(round(newconn * 0.02))
                # Idle keep-alive connections: proportional to new connections in the
                # interval, damped, plus a floor so a quiet node still shows an open pool.
                waiting = int(round(newconn * 0.55 + 2)) if newconn else rng.randint(0, 2)
                active = reading + writing + waiting

                out.write(json.dumps({
                    "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "node": node,
                    # counters (OTel Sum, monotonic cumulative)
                    "requests": c["requests"],
                    "accepts": c["accepts"],
                    "handled": c["handled"],
                    "dropped": c["dropped"],
                    # gauges (OTel Gauge)
                    "active": active,
                    "reading": reading,
                    "writing": writing,
                    "waiting": waiting,
                    "current": c["requests"],
                }, separators=(",", ":")) + "\n")
                written += 1

    # ---------------------------------------------------------------- summary
    print("\nnginx-stubstatus.jsonl  %s scrapes  %.1f MB" % (
        "{:,}".format(written), os.path.getsize(path) / 1e6))
    print("  interval: %ds  ->  %d scrapes/node/day x %d nodes"
          % (iv, nbins, len(ng.NGINX_NODES)))
    print("\nthe invariant both platforms must reproduce:")
    grand = sum(checksum.values())
    for node in ng.NGINX_NODES:
        print("  %-12s requests delta = %9s   (base %s -> %s)" % (
            node, "{:,}".format(checksum[node]),
            "{:,}".format(COUNTER_BASE[node]),
            "{:,}".format(counters[node]["requests"])))
    print("  %-12s requests delta = %9s" % ("TOTAL", "{:,}".format(grand)))
    if grand != total_lines:
        print("  WARNING: does not match the %s access log lines read"
              % "{:,}".format(total_lines))
    else:
        print("  matches access.json.log exactly (%s lines)" % "{:,}".format(total_lines))
    print("  dropped total: %d" % sum(c["dropped"] for c in counters.values()))


if __name__ == "__main__":
    main()
