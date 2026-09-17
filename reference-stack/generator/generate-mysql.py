#!/usr/bin/env python3
"""
Generate the MySQL dataset behind the Elastic `mysql` integration's three dashboards.

  data/mysql/slowlog.log                  slow query log -- MULTI-LINE records
  data/mysql/error.log                    server error log
  data/metrics/mysql-status.jsonl         SHOW GLOBAL STATUS, scraped every 30s
  data/metrics/mysql-replica.jsonl        SHOW REPLICA STATUS, scraped every 30s

The service is a CMS on `mysql-primary-01` with a replica, databases `cms` and `sessions` --
deliberately not the shop, which nginx/apache serve and postgres reports on.

Derived rather than invented
----------------------------
A statement stream is generated once, and both the log and the status counters are read off
it, so they cannot disagree:

  slowlog records                == statements slower than long_query_time (1.0s)
  final mysql.status.questions   == final sum of command.{select,insert,update,delete}

**The slow log is multi-line**, which is the thing that makes this dataset different from the
other three. One record is five lines:

    # Time: <iso>
    # User@Host: user[user] @ host [ip]  Id: <n>
    # Query_time: <s>  Lock_time: <s> Rows_sent: <n>  Rows_examined: <n>
    SET timestamp=<epoch>;
    <the statement>;

So a loader that reads line by line produces five useless documents per query. Both loaders
here split on the `# Time: ` header instead -- the ES loader with a record reader, the
collector with a `multiline.line_start_pattern`.

Deterministic: fixed seed, no wall-clock reads, stable hashing only.

Usage:  python3 generate-mysql.py [--statements 90000]
"""
import argparse, calendar, json, os, random, sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402

SEED = 20260920
HOST = "mysql-primary-01"
REPLICA = "mysql-replica-01"
INTERVAL = 30
LONG_QUERY_TIME = 1.0        # seconds; anything slower is logged
MISS_RATE = 0.0005           # InnoDB pool.reads per read.requests -- a 99.95% hit rate

# (database, user, command, statement, median ms, sigma, weight)
QUERIES = [
    ("cms", "cms_app", "select", "SELECT p.id, p.title FROM posts p WHERE p.status = ? ORDER BY p.published_at DESC LIMIT ?", 4.2, 0.8, 190),
    ("cms", "cms_app", "select", "SELECT * FROM posts WHERE id = ?", 0.7, 0.6, 240),
    ("cms", "cms_app", "select", "SELECT p.id, p.title FROM posts p JOIN terms t ON t.post_id = p.id WHERE t.slug = ? ORDER BY p.published_at DESC LIMIT ?", 62.0, 1.3, 70),
    ("cms", "cms_app", "select", "SELECT meta_key, meta_value FROM post_meta WHERE post_id = ?", 1.1, 0.7, 150),
    ("cms", "cms_app", "insert", "INSERT INTO comments (post_id, author, body) VALUES (?, ?, ?)", 2.4, 0.7, 34),
    ("cms", "cms_app", "update", "UPDATE posts SET view_count = view_count + ? WHERE id = ?", 1.6, 0.8, 60),
    ("cms", "cms_ro", "select", "SELECT count(*) FROM comments WHERE approved = ?", 18.0, 1.0, 22),
    ("cms", "cms_app", "delete", "DELETE FROM post_revisions WHERE post_id = ? AND created_at < ?", 42.0, 1.1, 9),
    ("sessions", "cms_app", "select", "SELECT payload FROM sessions WHERE token = ?", 0.5, 0.5, 210),
    ("sessions", "cms_app", "insert", "INSERT INTO sessions (token, payload, expires_at) VALUES (?, ?, ?)", 1.3, 0.6, 58),
    ("sessions", "cms_app", "update", "UPDATE sessions SET expires_at = ? WHERE token = ?", 0.9, 0.6, 72),
    ("sessions", "cms_app", "delete", "DELETE FROM sessions WHERE expires_at < ?", 380.0, 1.2, 18),
    # the reporting query that dominates the slow log
    ("cms", "cms_ro", "select", "SELECT t.slug, count(*) AS n FROM posts p JOIN terms t ON t.post_id = p.id GROUP BY t.slug ORDER BY n DESC", 1400.0, 0.9, 30),
]
Q_CW = ng.cumweights([q[6] for q in QUERIES])

APP_HOSTS = [("app-01", "10.0.2.11"), ("app-02", "10.0.2.12"), ("app-03", "10.0.2.13")]

ERRORS = [
    ("Note", "MY-010914", "Server", "Aborted connection %d to db: '%s' user: '%s' host: '%s' (Got timeout reading communication packets)"),
    ("Warning", "MY-013360", "Server", "Plugin mysql_native_password reported: ''mysql_native_password'' is deprecated and will be removed in a future release."),
    ("Note", "MY-010051", "Server", "Event Scheduler: scheduler thread started with id %d"),
    ("ERROR", "MY-010584", "Repl", "Replica SQL for channel '': Worker 1 failed executing transaction; Could not execute Update_rows event on table cms.posts, Error_code: 1032"),
    ("Warning", "MY-010055", "Server", "IP address '%s' could not be resolved: Name or service not known"),
    ("System", "MY-010931", "Server", "/usr/sbin/mysqld: ready for connections. Version: '8.0.39'  socket: '/var/run/mysqld/mysqld.sock'  port: 3306"),
]
ERR_CW = ng.cumweights([42, 12, 6, 5, 25, 2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--statements", type=int, default=120000)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    args = ap.parse_args()
    root = os.path.abspath(args.out)
    os.makedirs(os.path.join(root, "mysql"), exist_ok=True)
    os.makedirs(os.path.join(root, "metrics"), exist_ok=True)
    rng = random.Random(SEED)
    day0 = int(ng.DAY.timestamp())

    # ------------------------------------------------------------------ statements
    stmts = []
    for _ in range(args.statements):
        db, user, cmd, sql, med, sigma, _w = ng.pick(rng, QUERIES, Q_CW)
        t = ng.session_start(rng)
        dur = ng.lognorm(rng, med, sigma, lo=0.05) / 1000.0     # seconds
        stmts.append((t, db, user, cmd, sql, dur))
    stmts = [s for s in stmts if 0.0 <= s[0] < 86400.0]
    stmts.sort(key=lambda s: s[0])

    # ------------------------------------------------------------------ slow log
    slow_path = os.path.join(root, "mysql", "slowlog.log")
    n_slow = 0
    with open(slow_path, "w", encoding="utf-8", newline="\n") as fh:
        for off, db, user, cmd, sql, dur in stmts:
            if dur <= LONG_QUERY_TIME:
                continue
            n_slow += 1
            ts = ng.DAY + timedelta(seconds=off)
            host, ip = rng.choice(APP_HOSTS)
            rows_sent = rng.randint(1, 60)
            # The record separator. Both loaders key on this line -- see the module docstring.
            fh.write("# Time: %sZ\n" % ts.strftime("%Y-%m-%dT%H:%M:%S.%f"))
            fh.write("# User@Host: %s[%s] @ %s [%s]  Id: %5d\n" % (
                user, user, host, ip, rng.randint(100, 99999)))
            fh.write("# Query_time: %.6f  Lock_time: %.6f Rows_sent: %d  Rows_examined: %d\n"
                     % (dur, rng.uniform(0.0, 0.004), rows_sent,
                        rows_sent * rng.randint(40, 9000)))
            fh.write("SET timestamp=%d;\n" % (day0 + int(off)))
            fh.write("%s;\n" % sql)

    # ------------------------------------------------------------------ error log
    err_path = os.path.join(root, "mysql", "error.log")
    n_err = max(1, args.statements // 450)
    err_lines = []
    for _ in range(n_err):
        level, code, sub, tmpl = ng.pick(rng, ERRORS, ERR_CW)
        off = ng.session_start(rng)
        if "%d to db" in tmpl:
            msg = tmpl % (rng.randint(1000, 99999), rng.choice(["cms", "sessions"]),
                          "cms_app", rng.choice(APP_HOSTS)[0])
        elif "scheduler thread" in tmpl:
            msg = tmpl % rng.randint(4, 40)
        elif "could not be resolved" in tmpl:
            msg = tmpl % ("%d.%d.%d.%d" % tuple(rng.randint(1, 250) for _ in range(4)))
        else:
            msg = tmpl
        err_lines.append((off, level, code, sub, msg))
    err_lines.append((1.5, "System", "MY-010931", "Server", ERRORS[5][3]))
    err_lines = [e for e in err_lines if 0.0 <= e[0] < 86400.0]
    err_lines.sort(key=lambda e: e[0])
    with open(err_path, "w", encoding="utf-8", newline="\n") as fh:
        for off, level, code, sub, msg in err_lines:
            ts = ng.DAY + timedelta(seconds=off)
            fh.write("%sZ %d [%s] [%s] [%s] %s\n" % (
                ts.strftime("%Y-%m-%dT%H:%M:%S.%f"), rng.choice([0, 0, 0, 8, 12, 41]),
                level, code, sub, msg))

    # ------------------------------------------------------------------ SHOW GLOBAL STATUS
    nbins = 86400 // INTERVAL
    per_bin = {}
    for off, db, user, cmd, sql, dur in stmts:
        b = per_bin.setdefault(int(off // INTERVAL), {"select": 0, "insert": 0,
                                                      "update": 0, "delete": 0, "slow": 0})
        b[cmd] += 1
        if dur > LONG_QUERY_TIME:
            b["slow"] += 1

    c = {k: 0 for k in ("select", "insert", "update", "delete", "questions",
                        "bytes_sent", "bytes_recv", "aborted_clients", "aborted_connects",
                        "ce_select", "ce_peer", "ce_internal", "ce_max", "ce_accept",
                        "ce_tcpwrap", "oc_hits", "oc_misses", "oc_overflows",
                        "ssl_hits", "ssl_misses", "ssl_size", "bp_reads",
                        "bp_read_requests", "connections",
                        "threads_created")}
    max_used = 0
    bp_read_frac = 0.0
    st_path = os.path.join(root, "metrics", "mysql-status.jsonl")
    with open(st_path, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(nbins):
            ts = ng.DAY + timedelta(seconds=(i + 1) * INTERVAL)
            b = per_bin.get(i, {"select": 0, "insert": 0, "update": 0, "delete": 0, "slow": 0})
            n = b["select"] + b["insert"] + b["update"] + b["delete"]
            for k in ("select", "insert", "update", "delete"):
                c[k] += b[k]
            c["questions"] += n
            c["bytes_sent"] += n * rng.randint(400, 9000)
            c["bytes_recv"] += n * rng.randint(120, 900)
            c["connections"] += max(0, int(n * 0.07))
            c["threads_created"] += 1 if rng.random() < 0.02 else 0
            c["aborted_clients"] += 1 if rng.random() < 0.02 else 0
            c["aborted_connects"] += 1 if rng.random() < 0.01 else 0
            c["ce_select"] += 1 if rng.random() < 0.0008 else 0
            c["ce_peer"] += 1 if rng.random() < 0.0015 else 0
            c["ce_internal"] += 1 if rng.random() < 0.0004 else 0
            c["ce_max"] += 1 if rng.random() < 0.0006 else 0
            c["ce_accept"] += 1 if rng.random() < 0.0003 else 0
            c["ce_tcpwrap"] += 0
            c["oc_hits"] += n * rng.randint(1, 4)
            c["oc_misses"] += 1 if rng.random() < 0.05 else 0
            c["oc_overflows"] += 1 if rng.random() < 0.02 else 0
            c["ssl_hits"] += max(0, int(n * 0.4))
            c["ssl_misses"] += 1 if rng.random() < 0.03 else 0
            c["ssl_size"] = 128
            # Both CUMULATIVE. The Buffer Pool Efficiency panel computes
            # max(pool.reads) / max(read.requests) * 100 -- a ratio of two LIFETIME totals,
            # i.e. the InnoDB buffer-pool MISS rate, so the two have to be generated
            # together or the ratio drifts all day instead of holding steady.
            req_inc = n * rng.randint(30, 260)
            c["bp_read_requests"] += req_inc
            # A disk read per ~2000 logical reads (a healthy pool sits well above 99.9%
            # hit rate). The carry matters: `int(req_inc * MISS_RATE)` alone truncated to
            # zero in every bucket at this query rate, leaving the counter flat at 0 and the
            # efficiency panel reading exactly 0.0000%.
            bp_read_frac += req_inc * MISS_RATE
            inc = int(bp_read_frac)
            bp_read_frac -= inc
            c["bp_reads"] += inc

            threads_connected = min(151, max(1, int(round(n / 1.2)) + rng.randint(0, 4)))
            max_used = max(max_used, threads_connected)
            bp_total = 8192
            bp_free = max(0, bp_total - 5800 - rng.randint(0, 380))
            bp_data = bp_total - bp_free - rng.randint(0, 60)
            fh.write(json.dumps({
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "host": HOST,
                # counters
                "questions": c["questions"],
                "command_select": c["select"], "command_insert": c["insert"],
                "command_update": c["update"], "command_delete": c["delete"],
                "bytes_sent": c["bytes_sent"], "bytes_received": c["bytes_recv"],
                "aborted_clients": c["aborted_clients"],
                "aborted_connects": c["aborted_connects"],
                "ce_select": c["ce_select"], "ce_peer_address": c["ce_peer"],
                "ce_internal": c["ce_internal"], "ce_max": c["ce_max"],
                "ce_accept": c["ce_accept"], "ce_tcpwrap": c["ce_tcpwrap"],
                "oc_hits": c["oc_hits"], "oc_misses": c["oc_misses"],
                "oc_overflows": c["oc_overflows"],
                "ssl_hits": c["ssl_hits"], "ssl_misses": c["ssl_misses"],
                "ssl_size": c["ssl_size"],
                "bp_pool_reads": c["bp_reads"], "connections": c["connections"],
                "max_used_connections": max_used,
                # gauges
                "threads_connected": threads_connected,
                "threads_running": max(1, int(round(threads_connected * 0.22))),
                "threads_cached": max(0, 8 - rng.randint(0, 3)),
                "threads_created": c["threads_created"],
                "open_files": 24 + rng.randint(0, 9),
                "open_tables": 96 + rng.randint(0, 14),
                "open_streams": 0,
                "bp_pages_total": bp_total, "bp_pages_free": bp_free,
                "bp_pages_data": bp_data,
                "bp_pages_dirty": rng.randint(0, 220),
                "bp_read_requests": c["bp_read_requests"],
            }, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------ SHOW REPLICA STATUS
    # SQL_Delay is the DELIBERATE delay of a delayed replica, set with
    # `CHANGE REPLICATION SOURCE TO SOURCE_DELAY = N`. It is 0 on an ordinary replica -- which
    # is what this generated, and that made `[Metrics MySQL] Replica Status`'s "SQL thread
    # delay" panel unplottable: a constant zero on both platforms, so the two stacks agreed
    # perfectly and both charts were empty. A panel that cannot render is a hole in the
    # corpus, not a property of it.
    #
    # So the replica is reconfigured for a maintenance window: SOURCE_DELAY=30 between 02:00
    # and 05:00. That gives the panel a step function -- far better for comparing two stacks
    # than a flat line, because a mis-scoped tile shows up instantly.
    #
    # Chosen by bucket INDEX, with no rng draws, so every other series stays byte-identical.
    DELAY_FROM, DELAY_TO = 240, 600          # 02:00 and 05:00 at 30 s per bucket
    SOURCE_DELAY = 30
    rep_path = os.path.join(root, "metrics", "mysql-replica.jsonl")
    read_pos = 4_000_000
    exec_pos = 4_000_000
    with open(rep_path, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(nbins):
            ts = ng.DAY + timedelta(seconds=(i + 1) * INTERVAL)
            b = per_bin.get(i, {"select": 0, "insert": 0, "update": 0, "delete": 0})
            writes = b["insert"] + b["update"] + b["delete"]
            read_pos += writes * rng.randint(180, 420) + rng.randint(0, 400)
            # the SQL thread trails the IO thread, and the gap is the lag
            behind = rng.choice([0, 0, 0, 0, 1, 1, 2, 3, 7]) if writes else 0
            # A delayed replica reports Seconds_Behind_Source as the configured delay PLUS
            # whatever it is genuinely behind by, so the two panels move together.
            sql_delay = SOURCE_DELAY if DELAY_FROM <= i < DELAY_TO else 0
            behind += sql_delay
            exec_pos = max(exec_pos, read_pos - behind * rng.randint(400, 5000))
            fh.write(json.dumps({
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "host": REPLICA,
                "seconds_behind_source": behind,
                "sql_delay_sec": sql_delay,
                "read_pos": read_pos, "exec_pos": exec_pos,
                # `user.name` is in the replica_status data stream's TSDB routing_path
                # alongside host.name and source.server.uuid, so it is a DIMENSION, not
                # decoration -- omit it and the series identity is wrong.
                "user": "repl",
                "source_host": HOST, "source_port": 3306,
                "source_server_id": 1, "source_uuid": "3e11fa47-71ca-11e1-9e33-c80aa9429562",
                "binlog_file": "mysql-bin.000148",
                "file_info": "mysql-bin.000148 %d" % read_pos,
            }, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------ summary
    print("slowlog.log            %s records  %.1f MB" % (
        "{:,}".format(n_slow), os.path.getsize(slow_path) / 1e6))
    print("error.log              %s lines" % "{:,}".format(len(err_lines)))
    print("mysql-status.jsonl     %s scrapes" % "{:,}".format(nbins))
    print("mysql-replica.jsonl    %s scrapes" % "{:,}".format(nbins))
    print("\ninvariants both platforms must reproduce:")
    print("  statements generated         = %s" % "{:,}".format(len(stmts)))
    print("  final questions              = %s   %s" % (
        "{:,}".format(c["questions"]),
        "match" if c["questions"] == len(stmts) else "MISMATCH"))
    four = c["select"] + c["insert"] + c["update"] + c["delete"]
    print("  final sum(command.*)         = %s   %s" % (
        "{:,}".format(four), "match" if four == c["questions"] else "MISMATCH"))
    print("  slowlog records              = %s  (statements slower than %.1fs)" % (
        "{:,}".format(n_slow), LONG_QUERY_TIME))
    print("  max_used_connections         = %d  (high-water mark, NOT a rate)" % max_used)
    print("  SOURCE_DELAY window          = 02:00-05:00, SQL_Delay %ds (%d scrapes)"
          % (SOURCE_DELAY, DELAY_TO - DELAY_FROM))
    print("  buffer pool miss rate        = %.4f%%  (pool.reads / read.requests, lifetime)"
          % (100.0 * c["bp_reads"] / c["bp_read_requests"]))


if __name__ == "__main__":
    main()
