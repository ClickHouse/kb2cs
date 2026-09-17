#!/usr/bin/env python3
"""
Generate the PostgreSQL dataset behind the Elastic `postgresql` integration's three
dashboards: a query log plus the two pg_stat_* metric series the panels read.

  data/postgres/postgresql.log             query log, log_line_prefix '%t [%p] %q%u@%d '
  data/metrics/postgres-database.jsonl     pg_stat_database, scraped every 30s
  data/metrics/postgres-statement.jsonl    pg_stat_statements, scraped every 5 min

The statement metrics are **derived from the log**, not invented alongside it: pg_stat_statements
is exactly the aggregate of the statements postgres executed, so the generator accumulates it
from the lines it has just written. That gives three invariants both platforms must reproduce:

  sum(final statement.query.calls)        == number of `duration:` log lines
  sum(final statement.query.time.total.ms) == summed duration of those lines
  final database.transactions.rollback    == number of ERROR lines

Two modelling notes that matter for the dashboards:

* **BEGIN and COMMIT are emitted deliberately, and in volume.** Two of the nine metrics panels
  carry a Lucene filter excluding them (`not query.text : ("BEGIN;" or "commit" or ...)`), so
  without boilerplate in the data that filter is untestable and a migration could drop it
  unnoticed.
* **pg_stat_database's row counters are cumulative** -- that is what postgres reports and what
  the dashboard's `differences()` formulas require. Elastic's mapping types `rows.*` and
  `deadlocks` as `gauge` while typing `blocks.time.*` and `conflicts` as `counter`; the
  dashboard differences all of them, so the gauge typing looks like a mapping slip. Generated
  cumulative, which is both true to postgres and what the panels need.

Deterministic: fixed seed, no wall-clock reads. Shares the nginx diurnal curve so the three
services rise and fall together.

Usage:  python3 generate-postgres.py [--statements 120000]
"""
import argparse, calendar, hashlib, json, os, random, sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as ng  # noqa: E402  -- DAY, MINUTE_CW, cumweights/pick/lognorm

SEED = 20260919
HOST = "pg-primary-01"
DB_INTERVAL = 30       # pg_stat_database scrape
ST_INTERVAL = 300      # pg_stat_statements scrape -- cheaper, and scraped less often for real

# (database, user, query text, median ms, sigma, weight)
QUERIES = [
    ("shop", "shop_app", "BEGIN", 0.06, 0.4, 150),
    ("shop", "shop_app", "COMMIT", 0.09, 0.5, 148),
    ("shop", "shop_app", "SELECT id, sku, price FROM products WHERE category = $1 ORDER BY rank LIMIT $2", 6.4, 0.7, 120),
    ("shop", "shop_app", "SELECT * FROM products WHERE id = $1", 0.8, 0.6, 210),
    ("shop", "shop_app", "SELECT c.id, ci.product_id, ci.qty FROM carts c JOIN cart_items ci ON ci.cart_id = c.id WHERE c.session = $1", 3.1, 0.8, 90),
    ("shop", "shop_app", "INSERT INTO cart_items (cart_id, product_id, qty) VALUES ($1, $2, $3)", 1.2, 0.6, 70),
    ("shop", "shop_app", "UPDATE carts SET updated_at = now() WHERE id = $1", 0.9, 0.6, 64),
    ("shop", "shop_app", "INSERT INTO orders (user_id, total_cents, status) VALUES ($1, $2, $3) RETURNING id", 2.6, 0.7, 22),
    ("shop", "shop_app", "UPDATE inventory SET on_hand = on_hand - $1 WHERE sku = $2", 1.8, 0.9, 24),
    ("shop", "shop_ro", "SELECT * FROM users WHERE email = $1", 1.1, 0.6, 40),
    ("shop", "shop_app", "DELETE FROM sessions WHERE expires_at < now()", 34.0, 0.9, 6),
    # analytics: few calls, slow -- these are what the Query Duration dashboard is for
    ("analytics", "analytics_ro", "SELECT date_trunc($1, created_at) AS bucket, count(*) FROM orders GROUP BY bucket ORDER BY bucket", 820.0, 0.7, 7),
    ("analytics", "analytics_ro", "SELECT p.category, sum(oi.qty * oi.unit_cents) FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.category", 1450.0, 0.6, 5),
    ("analytics", "analytics_ro", "SELECT user_id, count(*) AS orders, sum(total_cents) FROM orders GROUP BY user_id ORDER BY 3 DESC LIMIT $1", 640.0, 0.8, 4),
    ("analytics", "analytics_ro", "REFRESH MATERIALIZED VIEW daily_revenue", 4200.0, 0.4, 1),
    # the self-monitoring query the dashboards filter out by name
    ("analytics", "postgres", "SELECT * FROM pg_stat_statements", 12.0, 0.5, 9),
]
Q_CW = ng.cumweights([q[5] for q in QUERIES])

ERRORS = [
    ("shop", "shop_app", "ERROR", 'duplicate key value violates unique constraint "orders_pkey"'),
    ("shop", "shop_app", "ERROR", "deadlock detected"),
    ("shop", "shop_app", "ERROR", "canceling statement due to statement timeout"),
    ("analytics", "analytics_ro", "ERROR", "canceling statement due to statement timeout"),
    ("shop", "shop_app", "WARNING", "there is already a transaction in progress"),
    ("postgres", "postgres", "FATAL", 'password authentication failed for user "postgres"'),
]
ERR_CW = ng.cumweights([26, 9, 14, 11, 30, 10])


def query_id(db, query):
    """A stable pg_stat_statements-style queryid.

    NOT `hash()`: Python randomises string hashing per process unless PYTHONHASHSEED is set,
    so using it here made the generated file differ between runs -- which defeats the whole
    point of a checksummed corpus. Caught by regenerating and comparing bytes.
    """
    h = hashlib.sha1(("%s\x00%s" % (db, query)).encode()).digest()
    return int.from_bytes(h[:8], "big") % 10**15


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--statements", type=int, default=120000)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    args = ap.parse_args()

    root = os.path.abspath(args.out)
    os.makedirs(os.path.join(root, "postgres"), exist_ok=True)
    os.makedirs(os.path.join(root, "metrics"), exist_ok=True)
    rng = random.Random(SEED)

    # ------------------------------------------------------------------ the log
    events = []                       # (offset_s, database, user, level, text, duration_ms|None)
    for _ in range(args.statements):
        db, user, q, med, sigma, _ = ng.pick(rng, QUERIES, Q_CW)
        t = ng.session_start(rng)     # nginx's diurnal curve, so the services move together
        dur = ng.lognorm(rng, med, sigma, lo=0.02)
        events.append((t, db, user, "LOG", q, dur))
    n_err = max(1, args.statements // 900)
    for _ in range(n_err):
        db, user, level, msg = ng.pick(rng, ERRORS, ERR_CW)
        events.append((ng.session_start(rng), db, user, level, msg, None))
    # checkpoints: every 5 minutes, no user or database attached
    for i in range(288):
        events.append((i * 300 + rng.uniform(0, 12), None, None, "LOG",
                       "checkpoint complete: wrote %d buffers (%.1f%%)"
                       % (rng.randint(180, 1900), rng.uniform(0.3, 4.2)), None))
    events = [e for e in events if 0.0 <= e[0] < 86400.0]
    events.sort(key=lambda e: e[0])

    pids = [2400 + i for i in range(24)]
    log_path = os.path.join(root, "postgres", "postgresql.log")
    n_stmt = n_rollback = 0
    total_ms = 0.0
    # cumulative pg_stat_statements, keyed (database, query)
    stcum = {}
    st_rows = []      # (offset, db, query, calls, total_ms, mem...)
    with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
        next_st = ST_INTERVAL
        for off, db, user, level, text, dur in events:
            ts = ng.DAY + timedelta(seconds=off)
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S.") + "%03d UTC" % (ts.microsecond // 1000)
            pid = rng.choice(pids)
            who = "" if db is None else "%s@%s " % (user, db)
            if dur is None:
                fh.write("%s [%d] %s%s:  %s\n" % (stamp, pid, who, level, text))
                if level == "ERROR":
                    n_rollback += 1
            else:
                fh.write("%s [%d] %s%s:  duration: %.3f ms  statement: %s\n"
                         % (stamp, pid, who, level, dur, text))
                n_stmt += 1
                total_ms += dur
                c = stcum.setdefault((db, text), {"calls": 0, "ms": 0.0, "lr": 0, "lh": 0,
                                                  "sr": 0, "sh": 0, "rows": 0})
                c["calls"] += 1
                c["ms"] += dur
                # shared buffers dominate on a warm cache; local = per-backend temp buffers
                c["sh"] += rng.randint(4, 220)
                c["sr"] += 1 if rng.random() < 0.06 else 0
                c["lh"] += rng.randint(0, 3)
                c["lr"] += 1 if rng.random() < 0.01 else 0
                c["rows"] += rng.randint(0, 40)
            while off >= next_st and next_st <= 86400:
                for (d, q), v in stcum.items():
                    st_rows.append((next_st, d, q, dict(v)))
                next_st += ST_INTERVAL
        for (d, q), v in stcum.items():           # final scrape
            st_rows.append((86400, d, q, dict(v)))

    # ------------------------------------------------------------------ pg_stat_statements
    st_path = os.path.join(root, "metrics", "postgres-statement.jsonl")
    with open(st_path, "w", encoding="utf-8", newline="\n") as fh:
        for off, db, q, v in st_rows:
            ts = ng.DAY + timedelta(seconds=off)
            fh.write(json.dumps({
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "host": HOST, "database": db,
                "query": q, "query_id": query_id(db, q),
                "calls": v["calls"], "time_total_ms": round(v["ms"], 3),
                "rows": v["rows"],
                "mem_local_read": v["lr"], "mem_local_hit": v["lh"],
                "mem_shared_read": v["sr"], "mem_shared_hit": v["sh"],
            }, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------ pg_stat_database
    # Cumulative, derived from the statements in each interval so the two series agree.
    per_db = {}
    for off, db, user, level, text, dur in events:
        if db is None:
            continue
        i = int(off // DB_INTERVAL)
        b = per_db.setdefault(db, {})
        s = b.setdefault(i, {"stmt": 0, "err": 0, "sel": 0, "ins": 0, "upd": 0, "dele": 0})
        if dur is None:
            if level == "ERROR":
                s["err"] += 1
            continue
        s["stmt"] += 1
        head = text.split(None, 1)[0].upper()
        if head == "SELECT":
            s["sel"] += 1
        elif head == "INSERT":
            s["ins"] += 1
        elif head == "UPDATE":
            s["upd"] += 1
        elif head == "DELETE":
            s["dele"] += 1

    db_path = os.path.join(root, "metrics", "postgres-database.jsonl")
    nbins = 86400 // DB_INTERVAL
    cums = {db: {k: 0 for k in ("fetched", "returned", "inserted", "updated", "deleted",
                                "commit", "rollback", "read_ms", "write_ms",
                                "conflicts", "deadlocks")}
            for db in per_db}
    with open(db_path, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(nbins):
            ts = ng.DAY + timedelta(seconds=(i + 1) * DB_INTERVAL)
            for db in sorted(per_db):
                s = per_db[db].get(i, {"stmt": 0, "err": 0, "sel": 0, "ins": 0,
                                       "upd": 0, "dele": 0})
                c = cums[db]
                c["commit"] += s["stmt"] - s["err"]
                c["rollback"] += s["err"]
                # a SELECT scans more rows than it returns; the ratio is the interesting bit
                c["returned"] += s["sel"] * rng.randint(1, 26)
                c["fetched"] += s["sel"] * rng.randint(20, 400)
                c["inserted"] += s["ins"] * rng.randint(1, 3)
                c["updated"] += s["upd"]
                c["deleted"] += s["dele"] * rng.randint(1, 60)
                c["read_ms"] += int(s["stmt"] * rng.uniform(0.0, 1.4))
                c["write_ms"] += int(s["stmt"] * rng.uniform(0.0, 0.6))
                c["conflicts"] += 1 if rng.random() < 0.0006 else 0
                c["deadlocks"] += 1 if rng.random() < 0.0004 else 0
                fh.write(json.dumps({
                    "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "host": HOST,
                    "database": db, "oid": 16384 + sorted(per_db).index(db),
                    "rows_fetched": c["fetched"], "rows_returned": c["returned"],
                    "rows_inserted": c["inserted"], "rows_updated": c["updated"],
                    "rows_deleted": c["deleted"],
                    "xact_commit": c["commit"], "xact_rollback": c["rollback"],
                    "blk_read_time_ms": c["read_ms"], "blk_write_time_ms": c["write_ms"],
                    "conflicts": c["conflicts"], "deadlocks": c["deadlocks"],
                }, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------ summary
    stmt_calls = sum(v["calls"] for v in stcum.values())
    stmt_ms = sum(v["ms"] for v in stcum.values())
    print("postgresql.log            %s lines  %.1f MB" % (
        "{:,}".format(len(events)), os.path.getsize(log_path) / 1e6))
    print("postgres-statement.jsonl  %s scrapes (%d series x %d)" % (
        "{:,}".format(len(st_rows)), len(stcum), len(st_rows) // max(1, len(stcum))))
    print("postgres-database.jsonl   %s scrapes (%d databases x %d)" % (
        "{:,}".format(nbins * len(per_db)), len(per_db), nbins))
    print("\ninvariants both platforms must reproduce:")
    print("  statements logged            = %s" % "{:,}".format(n_stmt))
    print("  sum(statement.query.calls)   = %s   %s" % (
        "{:,}".format(stmt_calls), "match" if stmt_calls == n_stmt else "MISMATCH"))
    print("  summed duration (ms)         = %.3f" % total_ms)
    print("  sum(query.time.total.ms)     = %.3f   %s" % (
        stmt_ms, "match" if abs(stmt_ms - total_ms) < 0.01 else "MISMATCH"))
    print("  ERROR lines                  = %d" % n_rollback)
    print("  sum(transactions.rollback)   = %d   %s" % (
        sum(c["rollback"] for c in cums.values()),
        "match" if sum(c["rollback"] for c in cums.values()) == n_rollback else "MISMATCH"))
    slow = sum(1 for e in events if e[5] and e[5] > 30.0)
    print("  statements slower than 30 ms = %s  (the Slow Queries panel's threshold)"
          % "{:,}".format(slow))


if __name__ == "__main__":
    main()
