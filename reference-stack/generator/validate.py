#!/usr/bin/env python3
"""
Validate the generated dataset. Checks the invariants a trainee (or an ingest pipeline)
will actually rely on. Exits non-zero on any failure.

Usage:  python3 validate.py [--data ../data]
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

from generate import DAY, SERVER_NAME, esc_default

# The de-facto standard combined-log regex (equivalent to grok %{COMBINEDAPACHELOG}).
COMBINED_RE = re.compile(
    r'^(?P<remote_addr>\S+) (?P<remote_user>\S+) (?P<auth>\S+) '
    r'\[(?P<time_local>[^\]]+)\] '
    r'"(?P<request>(?:[^"\\]|\\.)*)" '
    r'(?P<status>\d{3}) (?P<body_bytes>\d+) '
    r'"(?P<referer>(?:[^"\\]|\\.)*)" '
    r'"(?P<ua>(?:[^"\\]|\\.)*)"$'
)
ERR_RE = re.compile(
    r'^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>\w+)\] '
    r'(?P<pid>\d+)#(?P<tid>\d+): (?:\*(?P<conn>\d+) )?(?P<msg>.*)$'
)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

failures = []
notes = []


def check(cond, label, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def parse_time_local(s):
    d, mon, rest = s.split("/", 2)
    y, hh, mm, ss_tz = rest.split(":", 3)
    ss = ss_tz.split(" ")[0]
    return datetime(int(y), MONTHS[mon], int(d), int(hh), int(mm), int(ss), tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    args = ap.parse_args()
    d = os.path.abspath(args.data)

    p_comb = os.path.join(d, "access.log")
    p_json = os.path.join(d, "access.json.log")
    p_err = os.path.join(d, "error.log")

    print("\n== structural ==")
    unparsed_comb = 0
    unparsed_json = 0
    mismatch = []
    n = 0
    prev_t = None
    out_of_order = 0
    outside_window = 0
    hourly = Counter()
    status_from_comb = Counter()
    conns = set()
    conn_keys = set()
    seq_by_conn = {}
    gzip_bad = 0
    gzip_n = 0
    upstream_incoherent = 0
    day_start, day_end = DAY.timestamp(), DAY.timestamp() + 86400

    with open(p_comb, encoding="utf-8") as fc, open(p_json, encoding="utf-8") as fj:
        for lc, lj in zip(fc, fj):
            n += 1
            mc = COMBINED_RE.match(lc.rstrip("\n"))
            if not mc:
                unparsed_comb += 1
                if unparsed_comb <= 3:
                    notes.append("unparsed combined line %d: %s" % (n, lc[:160]))
                continue
            try:
                dj = json.loads(lj)
            except Exception as ex:
                unparsed_json += 1
                if unparsed_json <= 3:
                    notes.append("unparsed json line %d: %s" % (n, ex))
                continue

            # --- the two files must describe the same request ---------------------------
            req = "%s %s %s" % (dj["request_method"], esc_default(dj["request_uri"]),
                               dj["server_protocol"])
            same = (
                mc["remote_addr"] == dj["remote_addr"]
                and int(mc["status"]) == dj["status"]
                and int(mc["body_bytes"]) == dj["body_bytes_sent"]
                and mc["request"] == req
                and mc["referer"] == esc_default(dj["http_referer"])
                and mc["ua"] == esc_default(dj["http_user_agent"])
            )
            if not same and len(mismatch) < 5:
                mismatch.append((n, mc["request"], req))

            # --- time -------------------------------------------------------------------
            t_comb = parse_time_local(mc["time_local"])
            msec = float(dj["msec"])
            if abs(t_comb.timestamp() - int(msec)) > 1e-6:
                if len(mismatch) < 5:
                    mismatch.append((n, "time", f"{t_comb} vs {msec}"))
            if prev_t is not None and msec < prev_t - 1e-9:
                out_of_order += 1
            prev_t = msec
            if not (day_start <= msec < day_end):
                outside_window += 1
            hourly[int((msec - day_start) // 3600)] += 1
            status_from_comb[dj["status"]] += 1
            conns.add(dj["connection"])
            # keepalive means many requests share a $connection; the unique key for a
            # single request is (connection, connection_requests)
            conn_keys.add((dj["connection"], dj["connection_requests"]))
            seq_by_conn.setdefault(dj["connection"], []).append(dj["connection_requests"])

            # --- gzip_ratio must reproduce the sent size --------------------------------
            if dj["gzip_ratio"] != "-":
                gzip_n += 1
                if dj["body_bytes_sent"] <= 0 or float(dj["gzip_ratio"]) < 1.0:
                    gzip_bad += 1

            # --- upstream fields coherent ----------------------------------------------
            has_up = dj["upstream_addr"] != "-"
            if has_up != (dj["upstream_response_time"] != "-"):
                upstream_incoherent += 1

    # trailing-length equality
    with open(p_comb, encoding="utf-8") as f:
        n_comb = sum(1 for _ in f)
    with open(p_json, encoding="utf-8") as f:
        n_json = sum(1 for _ in f)

    check(n_comb == n_json, "access.log and access.json.log have equal line counts",
          f"{n_comb} vs {n_json}")
    check(unparsed_comb == 0, "every combined line matches %{COMBINEDAPACHELOG}",
          f"{unparsed_comb} failed")
    check(unparsed_json == 0, "every JSON line parses", f"{unparsed_json} failed")
    check(not mismatch, "combined and JSON agree field-by-field, line for line",
          str(mismatch[:3]))

    print("\n== time ==")
    check(out_of_order == 0, "access logs are ordered by completion time",
          f"{out_of_order} inversions")
    check(outside_window == 0, "all events inside the 24h window", f"{outside_window} outside")
    check(len(hourly) == 24, "all 24 hours populated", f"{sorted(hourly)}")

    print("\n== field coherence ==")
    check(gzip_bad == 0, f"gzip_ratio coherent on {gzip_n:,} compressed responses",
          f"{gzip_bad} bad")
    check(upstream_incoherent == 0, "upstream_addr and upstream_response_time agree on presence",
          f"{upstream_incoherent} incoherent")
    check(len(conn_keys) == n_json,
          "(connection, connection_requests) uniquely identifies a request",
          f"{len(conn_keys)} distinct keys for {n_json} rows")
    bad_seq = [c for c, seqs in seq_by_conn.items()
               if sorted(seqs) != list(range(1, len(seqs) + 1))]
    check(not bad_seq, "connection_requests is 1..n within every connection",
          f"{len(bad_seq)} connections with gaps, e.g. {bad_seq[:2]}")
    print(f"  info  {len(conns):,} TCP connections carrying {n_json:,} requests "
          f"({n_json / max(len(conns), 1):.2f} req/conn)")

    print("\n== error.log ==")
    err_unparsed = 0
    err_out_of_order = 0
    err_prev = None
    levels = Counter()
    joinable = 0
    joined = 0
    with open(p_err, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            m = ERR_RE.match(line.rstrip("\n"))
            if not m:
                err_unparsed += 1
                if err_unparsed <= 3:
                    notes.append("unparsed error line %d: %s" % (i, line[:160]))
                continue
            levels[m["level"]] += 1
            ts = datetime.strptime(m["ts"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if err_prev is not None and ts < err_prev:
                err_out_of_order += 1
            err_prev = ts
            if m["conn"] and "request: " in m["msg"]:
                joinable += 1
                if int(m["conn"]) in conns:
                    joined += 1

    check(err_unparsed == 0, "every error.log line matches the nginx error format",
          f"{err_unparsed} failed")
    check(err_out_of_order == 0, "error.log ordered by time", f"{err_out_of_order} inversions")
    check(joinable > 0 and joined == joinable,
          f"all {joinable:,} request-bearing error lines join to an access-log connection",
          f"{joinable - joined} orphans")
    print("  info  levels:", dict(levels.most_common()))

    print("\n== shape ==")
    peak_h = max(hourly, key=lambda h: hourly[h])
    trough_h = min(hourly, key=lambda h: hourly[h])
    ratio = hourly[peak_h] / hourly[trough_h]
    check(2.0 < ratio < 8.0, f"diurnal peak/trough ratio is plausible ({ratio:.2f}x, "
                             f"peak {peak_h:02d}:00, trough {trough_h:02d}:00)")
    fam = Counter()
    for s, c in status_from_comb.items():
        fam[str(s)[0] + "xx"] += c
    tot = sum(fam.values())
    check(0.002 < fam["5xx"] / tot < 0.02, f"5xx share is a realistic error budget "
                                           f"({100 * fam['5xx'] / tot:.2f}%)")
    check(fam["2xx"] / tot > 0.6, f"2xx dominates ({100 * fam['2xx'] / tot:.1f}%)")
    print("  info  hourly:", " ".join(f"{hourly[h] // 1000}k" for h in range(24)))

    if notes:
        print("\n== notes ==")
        for x in notes[:12]:
            print("  -", x)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
