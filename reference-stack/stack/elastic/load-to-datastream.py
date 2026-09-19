#!/usr/bin/env python3
"""
Backfill the nginx dataset into the Fleet integration's data streams, so the shipped
Kibana dashboards have something to draw.

  logs-nginx.access-default   <- data/access.log   (stock combined format)
  logs-nginx.error-default    <- data/error.log

Only access.log is loaded, not access.json.log: the integration's pipeline expects the
combined format, and loading both would double every request.

Timestamps are rewritten INSIDE each log line before sending, so the integration's own grok
and date processors parse the shifted time. That keeps the document wholly self-consistent --
`message`, `nginx.access.time` and `@timestamp` all agree -- which is not something you can
achieve by shifting after parsing.

Apache is loaded by the same code path via `--service apache`, reading /data/apache/:

  logs-apache.access-default  <- data/apache/access.log  (apache 'combined')
  logs-apache.error-default   <- data/apache/error.log

Apache's `combined` LogFormat is byte-compatible with nginx's, so only the error log needs
its own timestamp shifter. Each service replaces only its own two data streams, so loading
one never disturbs the other.

Usage (inside the compose stack):
    docker compose run --rm load
    docker compose run --rm load python /load.py --no-shift
    docker compose run --rm load python /load.py --whole-days
    docker compose run --rm load python /load.py --service apache --align-hour
"""

import argparse
import calendar
import gzip
import json
import os
import re
import sys
import time
import base64
import urllib.error
import urllib.request

ES = os.environ.get("ES", "http://localhost:9200")
ES_USER = os.environ.get("ES_USER", "elastic")
ES_PASS = os.environ.get("ELASTIC_PASSWORD", "changeme")
_AUTH = "Basic " + base64.b64encode(("%s:%s" % (ES_USER, ES_PASS)).encode()).decode()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# 17/Aug/2026:14:23:11 +0000  -- nginx $time_local AND apache %t are the same format
ACCESS_TS = re.compile(r"\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]")
# 2026/08/17 14:23:11                        -- nginx error log
ERROR_TS = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})")
# [Mon Aug 17 14:23:11.123456 2026]          -- apache error log
APACHE_ERROR_TS = re.compile(
    r"^\[(\w{3}) (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2})\.(\d{6}) (\d{4})\]")
# 2026-08-17 09:14:22.481 UTC                -- postgres, log_line_prefix '%t ...'
POSTGRES_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3}) (\w{1,4})")
# 2026-08-17T09:14:22.481000Z                -- mysql error log
MYSQL_ERROR_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z")
# SET timestamp=1786924800;                  -- mysql slow log, and the ONLY timestamp in a
# slow-log record that the integration's pipeline actually reads. Measured against
# logs-mysql.slowlog-1.28.1/_simulate: make `# Time:` and `SET timestamp` disagree and
# @timestamp follows SET; delete the SET line and @timestamp is not set at all, while
# deleting `# Time:` changes nothing. So SET is what has to be rewritten -- `# Time:` is
# rewritten too, purely to keep the file internally consistent for anything else reading it.
MYSQL_SET_TS = re.compile(r"^SET timestamp=(\d+);", re.M)
# # Time: 2026-08-17T00:00:00.266864Z        -- slow-log record header (the record separator)
MYSQL_SLOW_TIME = re.compile(
    r"^# Time: (\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z", re.M)
# Aug 17 00:00:01                            -- classic syslog header (system auth + syslog)
SYSLOG_TS = re.compile(r"^(\w{3})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
# Syslog format carries NO YEAR, so a reader has to be told which one. Elastic Agent and
# Filebeat assume the current year, which is exactly why backfilling a syslog corpus across a
# New Year boundary misdates every line -- a real-world trap, not an artefact of this repo.
# The corpus is generated at generate.py's DAY, so that is the year to use.
SYSLOG_YEAR = 2026


def http(method, path, body=None, ctype="application/json"):
    req = urllib.request.Request(ES + path, method=method)
    req.add_header("Content-Type", ctype)
    req.add_header("Authorization", _AUTH)
    data = body.encode() if isinstance(body, str) else body
    with urllib.request.urlopen(req, data, timeout=300) as r:
        return json.loads(r.read() or b"{}")


def shift_access_line(line, delta):
    m = ACCESS_TS.search(line)
    if not m:
        return line, None
    d, mon, y, hh, mm, ss, tz = m.groups()
    epoch = calendar.timegm((int(y), MONTH_NUM[mon], int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    t = time.gmtime(epoch + delta)
    new = "[%02d/%s/%d:%02d:%02d:%02d %s]" % (
        t.tm_mday, MONTHS[t.tm_mon - 1], t.tm_year, t.tm_hour, t.tm_min, t.tm_sec, tz)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", t)
    return line[:m.start()] + new + line[m.end():], iso


def shift_error_line(line, delta):
    m = ERROR_TS.match(line)
    if not m:
        return line, None
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    epoch = calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0))
    t = time.gmtime(epoch + delta)
    new = time.strftime("%Y/%m/%d %H:%M:%S", t)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", t)
    return new + line[m.end():], iso


def shift_apache_error_line(line, delta):
    """Apache's error format, which unlike nginx's carries a weekday and microseconds.

    The weekday has to be recomputed rather than carried over: shifting the date without it
    leaves lines like `[Mon Sep 16 ...]` on a Wednesday. Grok does not check the two agree
    (DAY is matched and discarded), so nothing would fail -- the data would just be wrong.
    """
    m = APACHE_ERROR_TS.match(line)
    if not m:
        return line, None
    _dow, mon, d, hh, mm, ss, us, y = m.groups()
    epoch = calendar.timegm((int(y), MONTH_NUM[mon], int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    t = time.gmtime(epoch + delta)
    new = "[%s %s %02d %02d:%02d:%02d.%s %d]" % (
        DAYS[t.tm_wday], MONTHS[t.tm_mon - 1], t.tm_mday,
        t.tm_hour, t.tm_min, t.tm_sec, us, t.tm_year)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.", t) + us[:3] + "Z"
    return new + line[m.end():], iso


def shift_postgres_line(line, delta):
    """Postgres's `%t` prefix: whole-second timestamp plus milliseconds and a zone name.

    The zone abbreviation is carried through unchanged -- the integration's grok captures it
    into `event.timezone` and the date processor uses it, so rewriting the instant without it
    would move every log line by the UTC offset.
    """
    m = POSTGRES_TS.match(line)
    if not m:
        return line, None
    y, mo, d, hh, mm, ss, ms, tz = m.groups()
    epoch = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    t = time.gmtime(epoch + delta)
    new = "%04d-%02d-%02d %02d:%02d:%02d.%s %s" % (
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, ms, tz)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.", t) + ms + "Z"
    return new + line[m.end():], iso


def shift_mysql_error_line(line, delta):
    """MySQL's error log: a plain ISO-8601 UTC instant with microseconds.

    No zone abbreviation to preserve (unlike postgres) and no locale-dependent month name
    (unlike apache), so this is the simplest of the five.
    """
    m = MYSQL_ERROR_TS.match(line)
    if not m:
        return line, None
    y, mo, d, hh, mm, ss, us = m.groups()
    epoch = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    t = time.gmtime(epoch + delta)
    iso6 = time.strftime("%Y-%m-%dT%H:%M:%S.", t) + us + "Z"
    return iso6 + line[m.end():], iso6[:23] + "Z"


def shift_mysql_slowlog_record(record, delta):
    """Shift a whole multi-line slow-log record.

    Takes a RECORD, not a line -- see read_records. Rewrites both timestamps it contains:

      SET timestamp=<epoch>   authoritative; the pipeline's @timestamp comes from here
      # Time: <iso>           decorative for Elastic, but it is the record separator, so
                              leaving it on the old date would make the file self-contradictory

    `SET timestamp` is whole seconds, which is also the precision ClickStack's collector
    reads, so both stacks compute an identical delta for mysql -- the same property apache
    has, and the reason neither service needs the sub-second reconciliation nginx does.
    """
    ms = MYSQL_SET_TS.search(record)
    if not ms:
        return record, None
    new_epoch = int(ms.group(1)) + delta
    record = record[:ms.start()] + ("SET timestamp=%d;" % new_epoch) + record[ms.end():]

    def _time(m):
        y, mo, d, hh, mm, ss, us = m.groups()
        e = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0))
        return time.strftime("# Time: %Y-%m-%dT%H:%M:%S.", time.gmtime(e + delta)) + us + "Z"

    record = MYSQL_SLOW_TIME.sub(_time, record)
    return record, time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(new_epoch))


def shift_syslog_line(line, delta):
    """Classic syslog header, shared by `system.auth` and `system.syslog`.

    Both streams need BOTH halves of the shift, and for opposite reasons -- measured against
    `_simulate`, not assumed:

      logs-system.syslog  re-derives @timestamp from the message text and discards the value
                          on the document, so the LINE has to be rewritten.
      logs-system.auth    keeps the document's @timestamp when there is one (its date
                          processors are gated on `ctx['@timestamp'] == null`), so the
                          returned ISO has to be right.

    Rewriting only one of the two silently breaks one stream while the other looks perfect.
    """
    m = SYSLOG_TS.match(line)
    if not m:
        return line, None
    mon, d, hh, mm, ss = m.groups()
    epoch = calendar.timegm((SYSLOG_YEAR, MONTH_NUM[mon], int(d),
                             int(hh), int(mm), int(ss), 0, 0, 0))
    t = time.gmtime(epoch + delta)
    # `%2d` on the day: syslog space-pads single digits, and the integration's grok has a
    # `MMM  d` format (two spaces) specifically for that case.
    new = "%s %2d %02d:%02d:%02d" % (MONTHS[t.tm_mon - 1], t.tm_mday,
                                     t.tm_hour, t.tm_min, t.tm_sec)
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", t)
    return new + line[m.end():], iso


def read_records(f, record_start):
    """Yield one logical record per document.

    Line-oriented logs yield a line each. MySQL's slow log does not: one query is five
    lines, and the integration's grok wants all five in a single `message`. Reading it line
    by line yields five documents per query, four of which carry no query at all. So the
    reader is told where a record STARTS and accumulates until the next start.
    """
    if record_start is None:
        for line in f:
            yield line.rstrip("\n")
        return
    cur = []
    for line in f:
        if line.startswith(record_start) and cur:
            yield "".join(cur).rstrip("\n")
            cur = []
        cur.append(line)
    if cur:
        yield "".join(cur).rstrip("\n")


# Everything that differs between the two services. The loader itself is format-agnostic:
# apache's access log is byte-compatible with nginx's `combined`, so only the error log
# needs its own shifter.
# `files` is a list of (filename, data stream, shifter) or, for a log whose records span
# several lines, (filename, data stream, shifter, record-start prefix). nginx and apache each
# ship an access log and an error log; postgres ships one combined query log and mysql a slow
# log plus an error log, so the loader iterates a list rather than assuming a fixed pair.
SERVICES = {
    "nginx": {
        "subdir": "",
        "shift_from": "access.log",
        "files": [("access.log", "logs-nginx.access-default", shift_access_line),
                  ("error.log", "logs-nginx.error-default", shift_error_line)],
    },
    "apache": {
        "subdir": "apache",
        "shift_from": "access.log",
        "files": [("access.log", "logs-apache.access-default", shift_access_line),
                  ("error.log", "logs-apache.error-default", shift_apache_error_line)],
    },
    # Named for the INTEGRATION, not the daemon: `event.module` and `service.type` are
    # constant_keyword in the mapping and must read `postgresql`, so a service key of
    # "postgres" is rejected at index time with a document_parsing_exception.
    "postgresql": {
        "subdir": "postgres",
        # postgres has no access log to anchor on, so the shift is computed from its own
        # query log -- see compute_delta's postgres branch.
        "shift_from": "postgresql.log",
        "files": [("postgresql.log", "logs-postgresql.log-default", shift_postgres_line)],
    },
    # The `system` integration is the first one that is not a single daemon: it monitors the
    # fleet the other four serve. Both of its logs share one shifter, because both are plain
    # syslog format.
    "system": {
        "subdir": "system",
        "shift_from": "syslog.log",
        "files": [("syslog.log", "logs-system.syslog-default", shift_syslog_line),
                  ("auth.log", "logs-system.auth-default", shift_syslog_line)],
    },
    "mysql": {
        "subdir": "mysql",
        # Anchored on the slow log rather than the error log: it is the busier of the two
        # and the one the dashboards are mostly built on. Both files get the same delta.
        "shift_from": "slowlog.log",
        "files": [("slowlog.log", "logs-mysql.slowlog-default",
                   shift_mysql_slowlog_record, "# Time: "),
                  ("error.log", "logs-mysql.error-default", shift_mysql_error_line)],
    },
}


def compute_delta(path, mode, anchor_epoch=None):
    """Seconds to add so the dataset's last event lands on the anchor.

    The anchor defaults to now, which is fine for a single stack but makes two stacks drift
    apart by however long elapsed between their loads -- see RUNBOOK.md, "Keeping the two
    stacks on the same clock". Use mode="align-hour" (both sides) or pass an explicit
    anchor_epoch (both sides) when the two are going to be compared.
    """
    if mode == "none":
        return 0
    last = None
    with open(path, "rb") as f:                      # cheap tail
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8", "replace")
        last = tail.strip().split("\n")[-1]
    m = ACCESS_TS.search(last)
    if m:
        d, mon, y, hh, mm, ss, _ = m.groups()
        end = calendar.timegm((int(y), MONTH_NUM[mon], int(d),
                               int(hh), int(mm), int(ss), 0, 0, 0))
    else:
        pm = POSTGRES_TS.match(last)
        if pm:
            y, mo, d, hh, mm, ss, _ms, _tz = pm.groups()
            end = calendar.timegm((int(y), int(mo), int(d),
                                   int(hh), int(mm), int(ss), 0, 0, 0))
        else:
            sm = SYSLOG_TS.match(last)
            if sm:
                mon, d, hh, mm, ss = sm.groups()
                end = calendar.timegm((SYSLOG_YEAR, MONTH_NUM[mon], int(d),
                                       int(hh), int(mm), int(ss), 0, 0, 0))
            else:
                # mysql slow log: the LAST line of a record is the statement, not a
                # timestamp, so scan the whole tail for the final `SET timestamp=`.
                hits = MYSQL_SET_TS.findall(tail)
                if not hits:
                    sys.exit("cannot read a timestamp from the end of %s" % path)
                end = int(hits[-1])

    anchor = int(anchor_epoch) if anchor_epoch else int(time.time())
    if mode == "align-hour":
        # Most recent whole hour. Deterministic for any two loads inside the same hour,
        # which is what keeps this stack and ClickStack on a common clock.
        anchor = (anchor // 3600) * 3600

    delta = anchor - end
    if mode == "whole-days":
        delta = (delta // 86400) * 86400
    return delta


def load(path, datastream, shifter, delta, module="nginx", record_start=None,
         batch_bytes=8 << 20):
    # Every shipped dashboard filters on `data_stream.dataset`, and 8 of nginx's panels
    # are useless without it. Elastic Agent sets these three fields automatically; a bulk
    # loader has to do it by hand. They are constant_keyword in the integration's mapping,
    # so the values must match the target data stream name exactly (logs-<dataset>-<ns>) --
    # a mismatch is rejected at index time rather than silently ignored.
    _type, _dataset, _ns = datastream.split("-", 1)[0], datastream.split("-")[1], datastream.rsplit("-", 1)[1]
    meta = {
        "data_stream": {"type": _type, "dataset": _dataset, "namespace": _ns},
        "event": {"dataset": _dataset, "module": module},
        "service": {"type": module},
    }
    sent = errors = 0
    first_errors = []
    buf, nbuf = [], 0
    opener = gzip.open if path.endswith(".gz") else open

    def flush():
        nonlocal sent, errors, buf, nbuf
        if not buf:
            return
        resp = http("POST", "/%s/_bulk" % datastream, "".join(buf), "application/x-ndjson")
        n = len(buf) // 2
        if resp.get("errors"):
            for item in resp.get("items", []):
                err = item.get("create", {}).get("error")
                if err:
                    errors += 1
                    if len(first_errors) < 3:
                        first_errors.append("%s: %s" % (err.get("type"), str(err.get("reason"))[:200]))
        sent += n
        buf, nbuf = [], 0
        print("    %s: %,d sent, %,d rejected" % (datastream, sent, errors).replace(",d", "d")
              if False else "    %s: %d sent, %d rejected" % (datastream, sent, errors),
              end="\r", flush=True)

    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in read_records(f, record_start):
            if not line.strip():
                continue
            line, iso = shifter(line, delta)
            doc = {"message": line}
            doc.update(meta)
            if iso:
                # data streams reject a document with no @timestamp; the integration's date
                # processor overwrites this with the value it parses out of `message`,
                # which is the same instant because we rewrote the line above.
                doc["@timestamp"] = iso
            buf.append('{"create":{}}\n')
            buf.append(json.dumps(doc, ensure_ascii=False) + "\n")
            nbuf += len(buf[-1])
            if nbuf >= batch_bytes:
                flush()
    flush()
    print()
    return sent, errors, first_errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data")
    ap.add_argument("--no-shift", action="store_true", help="keep the original 2026-08-17 dates")
    ap.add_argument("--align-hour", action="store_true",
                    help="end the dataset on the most recent whole hour; use on BOTH stacks "
                         "so they land on the same clock and can be compared side by side")
    ap.add_argument("--anchor-epoch", type=int, default=None,
                    help="explicit anchor (unix seconds) the last event is moved to. Pass the "
                         "same value to both stacks for an exact match regardless of run time")
    ap.add_argument("--whole-days", action="store_true",
                    help="shift by whole days so the diurnal peak stays at the same clock hour")
    ap.add_argument("--append", action="store_true",
                    help="add to whatever is already indexed instead of replacing it")
    ap.add_argument("--service", choices=sorted(SERVICES), default="nginx",
                    help="which service's logs to load (default nginx). Each writes only its "
                         "own two data streams, so loading one never disturbs the other")
    args = ap.parse_args()

    svc = SERVICES[args.service]
    root = os.path.join(args.data, svc["subdir"]) if svc["subdir"] else args.data
    # entries are 3-tuples, or 4-tuples for a multi-line log; pad so the rest is uniform
    targets = [(os.path.join(root, e[0]),) + tuple(e[1:]) + (None,) * (4 - len(e))
               for e in svc["files"]]
    for path, _ds, _sh, _rs in targets:
        if not os.path.exists(path):
            sys.exit("missing %s -- is ../../data mounted?" % path)
    anchor_file = os.path.join(root, svc["shift_from"])

    if args.no_shift:
        mode = "none"
    elif args.whole_days:
        mode = "whole-days"
    elif args.align_hour:
        mode = "align-hour"
    else:
        mode = "now"
    anchor = args.anchor_epoch or (os.environ.get("SHIFT_ANCHOR_EPOCH") or None)
    delta = compute_delta(anchor_file, mode, anchor)
    print("[load] time shift: %+d seconds (%.2f days), mode=%s%s"
          % (delta, delta / 86400.0, mode, (", anchor=%s" % anchor) if anchor else ""))

    info = http("GET", "/")
    print("[load] elasticsearch %s" % info["version"]["number"])

    # Replace by default. This dataset is a fixed corpus, not a stream, so loading it twice
    # is always a mistake -- and one that hides well, since it leaves every count exactly
    # doubled with no error anywhere.
    streams = ",".join(ds for _p, ds, _s, _r in targets)
    if not args.append:
        try:
            http("DELETE", "/_data_stream/%s" % streams)
            print("[load] cleared existing data streams (use --append to keep them)")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            print("[load] no existing data streams to clear")

    results = []
    for path, ds, shifter, record_start in targets:
        print("[load] %s -> %s%s" % (path, ds, "  (multi-line records)" if record_start else ""))
        results.append((ds,) + load(path, ds, shifter, delta, args.service, record_start))

    http("POST", "/%s/_refresh" % streams)

    print("\n[load] summary")
    failed = 0
    for ds, sent, errs, msgs in results:
        print("    %-30s %7d sent, %d rejected" % (ds, sent, errs))
        for m in msgs:
            print("      ! %s" % m)
        failed += errs
    if failed:
        sys.exit(1)
    print("\n[load] open Kibana -> Analytics > Dashboard > search '%s'" % args.service)


if __name__ == "__main__":
    main()
