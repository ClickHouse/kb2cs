#!/usr/bin/env python3
"""
Generate the host-metrics corpus behind `[Metrics System] Host overview` and
`[Metrics System] Overview`.

  data/metrics/system-<stream>.jsonl   one file per Elastic data stream, eight in all:
      cpu  memory  load  network  diskio  filesystem  fsstat  process

Emits NEUTRAL records keyed by the Elastic field names. Each loader then maps them to its own
shape -- `stack/elastic/load-metrics.py` to ECS documents, `stack/clickstack/load-metrics.py`
to the OTel hostmetricsreceiver's model -- which is the same division of labour the other four
services use, and the reason one generator can feed two very different targets.

The fleet is the SAME five machines the rest of the corpus describes, so `system` monitors the
hosts nginx, apache and mysql run on rather than being a disconnected sixth dataset.

Invariants the dashboards depend on, all checkable on both platforms:

    cpu.total.norm.pct        == sum of the six component percentages
    fsstat.total_size.used    == sum of per-mount used bytes
    fsstat.total_size.total   == sum of per-mount total bytes
    memory.actual.used.pct    == actual.used.bytes / memory.total
    memory.used.bytes         >= memory.actual.used.bytes   (used includes cache/buffers)

Counters vs gauges, taken from the integration's own mapping rather than guessed:
    network.*, diskio.*   COUNTERS   (monotonic; the panels wrap them in counter_rate())
    everything else       GAUGES

Deterministic: fixed seed, no wall-clock reads.

Usage:  python3 generate-system-metrics.py [--interval 60]
"""
import argparse, json, math, os, random, sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402

SEED = 20260922

# (host, cores, total memory bytes, role) -- the role shapes the load curve and the process
# list, so "Top Hosts by CPU" has a real ranking instead of five identical lines.
HOSTS = [
    ("web-edge-01", 8, 16 * 1024**3, "nginx"),
    ("web-edge-02", 8, 16 * 1024**3, "nginx"),
    ("web-edge-03", 8, 16 * 1024**3, "nginx"),
    ("docs-web-01", 4, 8 * 1024**3, "apache"),
    ("mysql-primary-01", 16, 64 * 1024**3, "mysql"),
]

# per role: (process name, base cpu share of one core, base rss fraction of total memory)
PROCS = {
    "nginx": [("nginx", 0.42, 0.030), ("php-fpm", 0.55, 0.075), ("node", 0.28, 0.055),
              ("filebeat", 0.06, 0.008), ("systemd", 0.01, 0.003), ("sshd", 0.004, 0.002)],
    "apache": [("httpd", 0.38, 0.065), ("php-fpm", 0.22, 0.050), ("node", 0.10, 0.030),
               ("filebeat", 0.06, 0.010), ("systemd", 0.01, 0.004), ("sshd", 0.004, 0.003)],
    "mysql": [("mysqld", 1.65, 0.480), ("filebeat", 0.07, 0.002), ("node", 0.05, 0.004),
              ("systemd", 0.01, 0.001), ("sshd", 0.004, 0.001), ("cron", 0.002, 0.001)],
}
IFACES = ["eth0", "lo"]
MOUNTS = [("/", "/dev/sda1", 64 * 1024**3), ("/var", "/dev/sda2", 256 * 1024**3)]
DISK = "sda"


def diurnal(off):
    """0..1 traffic shape, reusing the corpus's own hourly curve so the CPU graph and the
    access logs peak at the same time of day."""
    h = (off / 3600.0) % 24.0
    i = int(h) % 24
    j = (i + 1) % 24
    frac = h - int(h)
    lo, hi = ng.HOURLY[i], ng.HOURLY[j]
    cur = lo + (hi - lo) * frac
    return cur / float(max(ng.HOURLY))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="seconds between scrapes")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    args = ap.parse_args()
    root = os.path.abspath(args.out)
    os.makedirs(os.path.join(root, "metrics"), exist_ok=True)
    rng = random.Random(SEED)
    nbins = 86400 // args.interval

    files = {k: open(os.path.join(root, "metrics", "system-%s.jsonl" % k), "w",
                     encoding="utf-8", newline="\n")
             for k in ("cpu", "memory", "load", "network", "diskio",
                       "filesystem", "fsstat", "process")}
    counts = {k: 0 for k in files}
    checks = {"cpu_total_err": 0.0, "fsstat_err": 0, "mem_pct_err": 0.0, "mem_order": 0,
              "cpu_over_100": 0, "idle_err": 0.0}

    # cumulative counters carry across scrapes, one per (host, dimension)
    net = {(h[0], i): {"in_b": 0, "out_b": 0, "in_p": 0, "out_p": 0,
                       "in_d": 0, "out_d": 0} for h in HOSTS for i in IFACES}
    dio = {h[0]: {"r": 0, "w": 0} for h in HOSTS}
    pids = {(h[0], p[0]): rng.randint(300, 30000) for h in HOSTS for p in PROCS[h[3]]}

    for b in range(nbins):
        off = b * args.interval
        ts = (ng.DAY + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        d = diurnal(off)
        for host, cores, mem_total, role in HOSTS:
            # a per-host multiplier so the fleet ranks consistently rather than randomly
            weight = {"nginx": 1.0, "apache": 0.55, "mysql": 1.35}[role]
            busy = min(0.97, max(0.02, d * weight * rng.uniform(0.86, 1.14)))

            # ---------------------------------------------------------- cpu
            # The six components are generated FIRST and total is their sum, so
            # `total == sum(parts)` is an invariant rather than an approximation.
            user = round(busy * 0.62, 6)
            system = round(busy * 0.21, 6)
            iowait = round(busy * 0.09 * rng.uniform(0.4, 1.8), 6)
            nice = round(busy * 0.012 * rng.uniform(0.2, 1.6), 6)
            irq = round(busy * 0.008 * rng.uniform(0.3, 1.5), 6)
            softirq = round(busy * 0.031 * rng.uniform(0.5, 1.5), 6)
            total = round(user + system + iowait + nice + irq + softirq, 6)
            # A CPU cannot be more than 100% busy. The six components are drawn
            # independently, so at high `busy` they could sum past 1.0 -- which happened on
            # 111 of 7,200 scrapes and showed up as `total != 1 - idle`. Scale them back
            # proportionally so the identity `total == sum(parts)` AND the physical bound
            # `total <= 1` both hold, then derive idle exactly.
            if total > 1.0:
                k = 1.0 / total
                user = round(user * k, 6); system = round(system * k, 6)
                iowait = round(iowait * k, 6); nice = round(nice * k, 6)
                irq = round(irq * k, 6); softirq = round(softirq * k, 6)
                total = round(user + system + iowait + nice + irq + softirq, 6)
                # Rounding each scaled component back to 6dp can push the sum a hair over
                # 1.0 again (up to ~3e-6), so the residual is taken off the largest
                # component. Without this, 23 of 7,200 scrapes still reported >100%.
                if total > 1.0:
                    user = round(user - (total - 1.0), 6)
                    total = round(user + system + iowait + nice + irq + softirq, 6)
            idle = round(1.0 - total, 6)
            checks["cpu_total_err"] = max(checks["cpu_total_err"],
                abs(total - (user + system + iowait + nice + irq + softirq)))
            if total > 1.0:
                checks["cpu_over_100"] += 1
            checks["idle_err"] = max(checks["idle_err"], abs(total - (1.0 - idle)))
            files["cpu"].write(json.dumps({
                "ts": ts, "host": host, "cores": cores,
                "user": user, "system": system, "nice": nice, "irq": irq,
                "softirq": softirq, "iowait": iowait, "idle": idle, "total": total,
            }, separators=(",", ":")) + "\n"); counts["cpu"] += 1

            # ---------------------------------------------------------- load
            l1 = round(busy * cores * rng.uniform(0.85, 1.15), 3)
            l5 = round(l1 * rng.uniform(0.90, 1.04), 3)
            l15 = round(l1 * rng.uniform(0.82, 1.00), 3)
            files["load"].write(json.dumps({
                "ts": ts, "host": host, "cores": cores,
                "load1": l1, "load5": l5, "load15": l15,
            }, separators=(",", ":")) + "\n"); counts["load"] += 1

            # ---------------------------------------------------------- memory
            # `actual.used` excludes cache/buffers; `used` includes them, so used >= actual.
            actual_used = int(mem_total * (0.34 + 0.30 * busy) * rng.uniform(0.98, 1.02))
            cache = int(mem_total * (0.14 + 0.06 * rng.random()))
            used = min(mem_total, actual_used + cache)
            free = mem_total - used
            actual_pct = round(actual_used / float(mem_total), 6)
            swap_total = mem_total // 4
            swap_used_pct = round(min(0.35, max(0.0, (busy - 0.7) * 0.9)) *
                                  rng.uniform(0.8, 1.2), 6)
            if used < actual_used:
                checks["mem_order"] += 1
            checks["mem_pct_err"] = max(checks["mem_pct_err"],
                abs(actual_pct - actual_used / float(mem_total)))
            files["memory"].write(json.dumps({
                "ts": ts, "host": host, "total": mem_total, "used": used,
                "actual_used": actual_used, "free": free, "actual_used_pct": actual_pct,
                "swap_total": swap_total, "swap_used_pct": swap_used_pct,
            }, separators=(",", ":")) + "\n"); counts["memory"] += 1

            # ---------------------------------------------------------- network (counters)
            for iface in IFACES:
                c = net[(host, iface)]
                if iface == "lo":
                    inc_b = int(busy * 90_000 * rng.uniform(0.7, 1.3))
                    inc_p = int(busy * 700 * rng.uniform(0.7, 1.3))
                    drop = 0
                else:
                    inc_b = int(busy * 2_400_000 * rng.uniform(0.75, 1.25))
                    inc_p = int(busy * 2_600 * rng.uniform(0.75, 1.25))
                    drop = 1 if rng.random() < 0.015 else 0
                c["in_b"] += inc_b; c["out_b"] += int(inc_b * rng.uniform(2.6, 3.4))
                c["in_p"] += inc_p; c["out_p"] += int(inc_p * rng.uniform(0.95, 1.15))
                c["in_d"] += drop;  c["out_d"] += 1 if rng.random() < 0.008 else 0
                files["network"].write(json.dumps({
                    "ts": ts, "host": host, "name": iface,
                    "in_bytes": c["in_b"], "out_bytes": c["out_b"],
                    "in_packets": c["in_p"], "out_packets": c["out_p"],
                    "in_dropped": c["in_d"], "out_dropped": c["out_d"],
                }, separators=(",", ":")) + "\n"); counts["network"] += 1

            # ---------------------------------------------------------- diskio (counters)
            dd = dio[host]
            dd["r"] += int(busy * 180_000 * rng.uniform(0.5, 1.6))
            dd["w"] += int(busy * 640_000 * rng.uniform(0.6, 1.5))
            files["diskio"].write(json.dumps({
                "ts": ts, "host": host, "name": DISK,
                "read_bytes": dd["r"], "write_bytes": dd["w"],
            }, separators=(",", ":")) + "\n"); counts["diskio"] += 1

            # ---------------------------------------------------------- filesystem + fsstat
            fs_used_total = 0
            fs_total_total = 0
            for mount, device, size in MOUNTS:
                base = 0.46 if mount == "/" else 0.63
                # a slow upward drift across the day, so "Top mountpoints" is not flat
                pct = round(min(0.97, base + 0.05 * (b / float(nbins))
                                + rng.uniform(-0.004, 0.004)), 6)
                used_b = int(size * pct)
                fs_used_total += used_b
                fs_total_total += size
                files["filesystem"].write(json.dumps({
                    "ts": ts, "host": host, "mount_point": mount, "device_name": device,
                    "total": size, "used_bytes": used_b, "used_pct": pct,
                    "free": size - used_b,
                }, separators=(",", ":")) + "\n"); counts["filesystem"] += 1
            files["fsstat"].write(json.dumps({
                "ts": ts, "host": host, "count": len(MOUNTS),
                "total_size_total": fs_total_total, "total_size_used": fs_used_total,
                "total_size_free": fs_total_total - fs_used_total,
            }, separators=(",", ":")) + "\n"); counts["fsstat"] += 1

            # ---------------------------------------------------------- process
            for pname, cpu_share, rss_frac in PROCS[role]:
                pcpu = round(min(1.0, cpu_share * busy * rng.uniform(0.7, 1.35) / cores), 6)
                pnorm = pcpu
                prss = round(min(0.95, rss_frac * rng.uniform(0.92, 1.10)), 6)
                files["process"].write(json.dumps({
                    "ts": ts, "host": host, "name": pname, "pid": pids[(host, pname)],
                    "cpu_pct": round(min(1.0, pcpu * cores), 6),
                    "cpu_norm_pct": pnorm, "memory_rss_pct": prss,
                }, separators=(",", ":")) + "\n"); counts["process"] += 1

    for f in files.values():
        f.close()

    # ------------------------------------------------------------------ summary
    total_docs = sum(counts.values())
    print("%d hosts, %d scrapes each at %ds" % (len(HOSTS), nbins, args.interval))
    for k in ("cpu", "memory", "load", "network", "diskio", "filesystem", "fsstat", "process"):
        path = os.path.join(root, "metrics", "system-%s.jsonl" % k)
        print("  system-%-11s %8s records  %5.1f MB" % (
            k + ".jsonl", "{:,}".format(counts[k]), os.path.getsize(path) / 1e6))
    print("  %-19s %8s records total" % ("", "{:,}".format(total_docs)))
    print("\ninvariants both platforms must reproduce:")
    print("  cpu.total == sum(6 components)      max error %.1e   %s" % (
        checks["cpu_total_err"], "match" if checks["cpu_total_err"] < 1e-9 else "MISMATCH"))
    print("  cpu.total == 1 - cpu.idle           max error %.1e   %s" % (
        checks["idle_err"], "match" if checks["idle_err"] < 1e-9 else "MISMATCH"))
    print("  cpu.total <= 1 (a CPU has a ceiling) %d violations   %s" % (
        checks["cpu_over_100"], "match" if checks["cpu_over_100"] == 0 else "MISMATCH"))
    print("  memory.used >= memory.actual.used    %d violations   %s" % (
        checks["mem_order"], "match" if checks["mem_order"] == 0 else "MISMATCH"))
    # 1e-6, not 1e-9: the percentage is stored rounded to 6 decimal places, which is already
    # finer than the integration's `scaled_float` mapping preserves. Asserting exact equality
    # against the unrounded ratio would be asserting that the rounding did not happen.
    print("  memory.actual.used.pct == used/total max error %.1e   %s" % (
        checks["mem_pct_err"], "match" if checks["mem_pct_err"] < 1e-6 else "MISMATCH"))
    print("  fsstat.total_size.total              %s bytes/scrape/host" % "{:,}".format(
        sum(s for _m, _d, s in MOUNTS)))
    print("  distinct processes                   %d" % len(
        {p[0] for r in PROCS.values() for p in r}))
    print("  network interfaces / mounts / disks  %d / %d / 1" % (len(IFACES), len(MOUNTS)))


if __name__ == "__main__":
    main()
