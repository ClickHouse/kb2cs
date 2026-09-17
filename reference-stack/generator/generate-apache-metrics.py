#!/usr/bin/env python3
"""
Generate the apache `mod_status` metric series behind the Elastic `apache` integration's
[Metrics Apache] Overview dashboard.

    data/metrics/apache-status.jsonl   one scrape per line, per node

Derived from data/apache/access.log wherever it can be, like generate-nginx-metrics.py:

  total_accesses  cumulative request count       (one log line == one request)
  total_bytes     cumulative bytes sent          (the %b field; `-` counts as 0)

so the metric series and the apache access log describe the same traffic, and the per-bucket
increase of total_accesses sums to the log's own line count.

mod_status semantics that are easy to get wrong
-----------------------------------------------
`ReqPerSec` and `BytesPerSec` are **averages since server start**, not instantaneous rates —
mod_status divides the cumulative counter by uptime. So they are smooth curves that converge,
not spiky per-interval rates, and the dashboard's "Requests per sec" panel is charting a
long-run average. Modelling them as interval rates would look plausible and be wrong.

`scoreboard.total` is not a state: it is MaxRequestWorkers, i.e. the sum of the eleven real
slot states. The 12th field exists so a panel can show capacity next to usage.

Modelled, because an access log cannot report them: worker/scoreboard slot distribution, the
async connection states, CPU accounting and the three load averages.

The node split is also modelled — apache's `combined` format records no server name, so
requests are assigned to the two nodes by a seeded RNG rather than read from the log. The
totals still reconcile exactly.

Deterministic: fixed seed, no wall-clock reads. Timestamps stay on the corpus day
(2026-08-17); both loaders shift them the same way the apache log loaders do.

Usage:  python3 generate-apache-metrics.py [--interval 30] [--out ../data/metrics]
"""

import argparse
import json
import math
import os
import random
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402  -- DAY, cumweights/pick/lognorm

SEED = 20260918
NODES = ["docs-web-01", "docs-web-02"]

# Slots per node: MaxRequestWorkers. mod_status reports one character per slot, so the
# eleven states below always sum to this.
MAX_WORKERS = 150

# Elastic's scoreboard field names -> the OTel apachereceiver's `state` attribute values.
# Eleven real states; Elastic's twelfth field, `total`, is their sum.
SCOREBOARD = [
    ("open_slot", "open"),
    ("waiting_for_connection", "waiting"),
    ("starting_up", "starting"),
    ("reading_request", "reading"),
    ("sending_reply", "sending"),
    ("keepalive", "keepalive"),
    ("dns_lookup", "dnslookup"),
    ("closing_connection", "closing"),
    ("logging", "logging"),
    ("gracefully_finishing", "finishing"),
    ("idle_cleanup", "idle_cleanup"),
]

# 17/Aug/2026:00:00:00 +0000  then  " 200 1234 "
LINE = re.compile(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) [+-]\d{4}\]'
                  r'.*?" \d{3} (\d+|-) ')
MONTHS = {m: i + 1 for i, m in enumerate(ng.MONTHS)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--src", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "apache", "access.log"))
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "metrics"))
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    if not os.path.exists(src):
        sys.exit("missing %s -- run generate-apache.py first" % src)

    iv = args.interval
    nbins = 86400 // iv
    rng = random.Random(SEED)
    day0 = int(ng.DAY.timestamp())

    # bins[node][i] = [requests, bytes]
    bins = {n: [[0, 0] for _ in range(nbins)] for n in NODES}
    lines = skipped = 0
    print("reading %s ..." % os.path.basename(src))
    with open(src, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = LINE.search(line)
            if not m:
                skipped += 1
                continue
            d, mon, y, hh, mm, ss, nbytes = m.groups()
            import calendar
            epoch = calendar.timegm((int(y), MONTHS[mon], int(d),
                                     int(hh), int(mm), int(ss), 0, 0, 0))
            i = (epoch - day0) // iv
            if not 0 <= i < nbins:
                skipped += 1
                continue
            lines += 1
            # apache's combined format carries no server name, so the node is assigned here
            node = NODES[rng.randrange(len(NODES))]
            slot = bins[node][i]
            slot[0] += 1
            slot[1] += 0 if nbytes == "-" else int(nbytes)
    print("  %s requests parsed, %d lines skipped" % ("{:,}".format(lines), skipped))

    # Each node started a little before the window, so uptime is never zero.
    boot_offset = {n: 3600 + i * 897 for i, n in enumerate(NODES)}
    cum = {n: {"accesses": 0, "bytes": 0} for n in NODES}
    # cpu.* are percentages of a CPU-second accumulated since start, as mod_status reports
    cpu = {n: {"user": 0.0, "system": 0.0, "cu": 0.0, "cs": 0.0} for n in NODES}

    path = os.path.join(outdir, "apache-status.jsonl")
    written = 0
    check = {n: 0 for n in NODES}

    with open(path, "w", encoding="utf-8", newline="\n") as out:
        for i in range(nbins):
            ts = ng.DAY + timedelta(seconds=(i + 1) * iv)
            for node in NODES:
                reqs, nbytes = bins[node][i]
                c = cum[node]
                c["accesses"] += reqs
                c["bytes"] += nbytes
                check[node] += reqs
                uptime = boot_offset[node] + (i + 1) * iv

                # busy workers: concurrency implied by the interval's request count, damped
                busy = min(MAX_WORKERS, max(0, int(round(reqs / float(iv) * 1.8))))
                idle = MAX_WORKERS - busy

                # Distribute the slots. `waiting` absorbs the idle pool; the active states
                # share `busy` with sending dominant, which is what a static site looks like.
                sb = {k: 0 for _, k in SCOREBOARD}
                sb["waiting"] = max(0, idle - 2)
                sb["open"] = MAX_WORKERS - MAX_WORKERS  # filled at the end
                left = busy
                for state, share in (("sending", 0.62), ("keepalive", 0.18),
                                     ("reading", 0.08), ("logging", 0.05),
                                     ("closing", 0.04), ("dnslookup", 0.01)):
                    v = int(round(busy * share))
                    v = min(v, left)
                    sb[state] = v
                    left -= v
                sb["sending"] += left                       # any rounding remainder
                sb["starting"] = 1 if rng.random() < 0.02 else 0
                sb["finishing"] = 1 if rng.random() < 0.03 else 0
                sb["idle_cleanup"] = 1 if rng.random() < 0.01 else 0
                used = sum(sb[k] for _, k in SCOREBOARD if k != "open")
                sb["open"] = max(0, MAX_WORKERS - used)

                # CPU accumulates with work done; keep it monotonic like mod_status does.
                cpu[node]["user"] += reqs * 0.00042 + rng.uniform(0, 0.002)
                cpu[node]["system"] += reqs * 0.00018 + rng.uniform(0, 0.001)
                cpu[node]["cu"] += reqs * 0.00006
                cpu[node]["cs"] += reqs * 0.00003
                cpu_load = min(99.9, (cpu[node]["user"] + cpu[node]["system"]) / uptime * 100)

                base_load = 0.35 + busy / 40.0
                out.write(json.dumps({
                    "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "node": node,
                    # counters
                    "total_accesses": c["accesses"],
                    "total_bytes": c["bytes"],
                    "uptime": uptime,
                    "server_uptime": uptime,
                    # ConnsTotal in mod_status is the CURRENT connection count, not a
                    # cumulative one -- Elastic's mapping types it `counter`, which looks
                    # like a mapping slip. Emitting current connections keeps it faithful to
                    # apache and maps straight onto OTel's apache.current_connections gauge;
                    # emitting a cumulative value would also make panel 6 a duplicate of
                    # panel 2 (total accesses).
                    "connections_total": (max(0, int(round(busy * 0.55)))
                                          + max(0, int(round(busy * 0.30)))
                                          + max(0, int(round(busy * 0.10)))),
                    # mod_status averages-since-start, NOT interval rates
                    "requests_per_sec": round(c["accesses"] / uptime, 6),
                    "bytes_per_sec": round(c["bytes"] / uptime, 6),
                    "bytes_per_request": round(c["bytes"] / c["accesses"], 6)
                    if c["accesses"] else 0.0,
                    # gauges
                    "workers_busy": busy,
                    "workers_idle": idle,
                    "scoreboard": {ek: sb[ok] for ek, ok in SCOREBOARD},
                    "scoreboard_total": MAX_WORKERS,
                    "conn_async_writing": max(0, int(round(busy * 0.55))),
                    "conn_async_keep_alive": max(0, int(round(busy * 0.30))),
                    "conn_async_closing": max(0, int(round(busy * 0.10))),
                    "cpu_user": round(cpu[node]["user"], 4),
                    "cpu_system": round(cpu[node]["system"], 4),
                    "cpu_children_user": round(cpu[node]["cu"], 4),
                    "cpu_children_system": round(cpu[node]["cs"], 4),
                    "cpu_load": round(cpu_load, 4),
                    "load_1": round(base_load * rng.uniform(0.9, 1.1), 3),
                    "load_5": round(base_load * rng.uniform(0.95, 1.05), 3),
                    "load_15": round(base_load * rng.uniform(0.97, 1.03), 3),
                }, separators=(",", ":")) + "\n")
                written += 1

    print("\napache-status.jsonl  %s scrapes  %.1f MB" % (
        "{:,}".format(written), os.path.getsize(path) / 1e6))
    print("  interval: %ds -> %d scrapes/node/day x %d nodes" % (iv, nbins, len(NODES)))
    print("\nthe invariant both platforms must reproduce:")
    for node in NODES:
        print("  %-13s total_accesses delta = %9s  (final %s)" % (
            node, "{:,}".format(check[node]), "{:,}".format(cum[node]["accesses"])))
    grand = sum(check.values())
    print("  %-13s total_accesses delta = %9s" % ("TOTAL", "{:,}".format(grand)))
    print("  %s access.log lines read" % ("matches" if grand == lines else "DOES NOT MATCH"))
    print("  total_bytes final: %s" % "{:,}".format(sum(c["bytes"] for c in cum.values())))
    print("  scoreboard slots per node: %d (11 states + `total` as their sum)" % MAX_WORKERS)


if __name__ == "__main__":
    main()
