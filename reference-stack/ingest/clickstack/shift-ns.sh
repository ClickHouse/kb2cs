#!/usr/bin/env bash
# Print the SHIFT_NS value for otel-collector-nginx.yaml, which shifts the whole dataset
# forward so it lands near the present. Derived from the data itself, so it stays correct
# if you regenerate with a different DAY.
#
#   export SHIFT_NS=$(./ingest/clickstack/shift-ns.sh)               # last event ~= now
#   export SHIFT_NS=$(./ingest/clickstack/shift-ns.sh --align-hour)  # ends on a whole hour
#   export SHIFT_NS=$(./ingest/clickstack/shift-ns.sh --whole-days)  # keep clock alignment
#   export SHIFT_NS=0                                                # leave dates as-is
#
# Why the modes:
#
#   default      The last event lands at now, so the data covers the previous 24 hours and
#                every default time picker in HyperDX ("Last 15 minutes", "Last 24 hours")
#                is populated. The diurnal peak moves to an arbitrary wall-clock hour.
#
#   --align-hour The last event lands on the most recent whole hour. Use this when you are
#                comparing ClickStack against Elastic side by side: both loaders compute the
#                same anchor, so the two datasets land on the same clock instead of drifting
#                apart by however long elapsed between the two loads. Bucket boundaries also
#                line up with the time picker. Pair with `--align-hour` on the Elastic side.
#
#   --whole-days Shifts by a whole number of days, so 19:00 in the data is still 19:00 on
#                the clock and the traffic curve looks right against real time. The window
#                ends at the same time of day it originally did, which may be in the past
#                or the future depending on when you run it.
#
# SHIFT_ANCHOR_EPOCH overrides the anchor entirely, for all modes. Set it to the same value
# on both stacks and the two land on exactly the same clock regardless of when each runs:
#
#   export SHIFT_ANCHOR_EPOCH=$(date -u +%s)
#   export SHIFT_NS=$(./ingest/clickstack/shift-ns.sh)
#   docker compose run --rm load python /load.py --anchor-epoch "$SHIFT_ANCHOR_EPOCH"
#
# That is the only fully deterministic option: --align-hour still disagrees by exactly one
# hour if the two loads happen to straddle a :00 boundary.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${DATA:-$HERE/../../data}"

# SERVICE=apache shifts the apache dataset instead of the nginx one. The two are shifted
# INDEPENDENTLY and that is deliberate: each one's own last event is moved to the anchor, so
# both services end together on the wall clock even though their raw logs end seconds apart.
#
# Apache's %t carries only whole seconds, which is the same precision the Elasticsearch
# loader reads (its ACCESS_TS regex), so both stacks compute a byte-identical delta for
# apache. nginx's $msec is millisecond-precision, which is why its two loaders can differ by
# a fraction of a second -- see RUNBOOK.md.
#
# mysql behaves like apache, for a different reason: its anchor is the slow log's
# `SET timestamp=<epoch>`, which is whole seconds by format. Note the slow log's LAST LINE is
# the SQL statement, not a timestamp, so unlike every other service this one cannot read the
# tail's final line -- it scans backwards for the last `SET timestamp=`.
SERVICE="${SERVICE:-nginx}"
case "$SERVICE" in
  nginx)    SRC="$DATA/access.json.log" ;;
  apache)   SRC="$DATA/apache/access.log" ;;
  postgres) SRC="$DATA/postgres/postgresql.log" ;;
  mysql)    SRC="$DATA/mysql/slowlog.log" ;;
  system)   SRC="$DATA/system/syslog.log" ;;
  *) echo "unknown SERVICE: $SERVICE (use nginx, apache, postgres, mysql or system)" >&2; exit 1 ;;
esac

[ -r "$SRC" ] || { echo "cannot read $SRC" >&2; exit 1; }

MODE="${1:-now}"

# The final line is the last event in the dataset (both files are time-ordered).
if [ "$SERVICE" = "system" ]; then
  # Classic syslog: `Aug 17 00:00:01`, with NO YEAR in the line. The corpus year has to be
  # supplied, and it must be the same one stack/elastic/load-to-datastream.py uses
  # (SYSLOG_YEAR), or the two stacks shift by a year relative to each other.
  LAST_MSEC="$(tail -1 "$SRC" | python3 -c '
import calendar, re, sys
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
SYSLOG_YEAR = 2026
m = re.match(r"^(\w{3})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})", sys.stdin.read())
mon, d, hh, mm, ss = m.groups()
print(calendar.timegm((SYSLOG_YEAR, MONTHS.index(mon) + 1, int(d),
                       int(hh), int(mm), int(ss), 0, 0, 0)))
')"
elif [ "$SERVICE" = "mysql" ]; then
  # Records are multi-line and end with the statement, so grep the whole tail for the final
  # `SET timestamp=`. Same value the Elasticsearch loader's compute_delta() picks.
  LAST_MSEC="$(tail -40 "$SRC" | grep -o '^SET timestamp=[0-9]*;' | tail -1 |
               tr -dc '0-9')"
  [ -n "$LAST_MSEC" ] || { echo "no 'SET timestamp=' found in the tail of $SRC" >&2; exit 1; }
elif [ "$SERVICE" = "nginx" ]; then
  LAST_MSEC="$(tail -1 "$SRC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["msec"])')"
elif [ "$SERVICE" = "postgres" ]; then
  # log_line_prefix '%t ...': 2026-08-17 23:59:58.123 UTC
  LAST_MSEC="$(tail -1 "$SRC" | python3 -c '
import calendar, re, sys
m = re.match(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", sys.stdin.read())
y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
print(calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)))
')"
else
  # apache 'combined': ... [17/Aug/2026:23:59:58 +0000] "GET ..."
  LAST_MSEC="$(tail -1 "$SRC" | python3 -c '
import calendar, re, sys
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
m = re.search(r"\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]", sys.stdin.read())
d, mon, y, hh, mm, ss, _tz = m.groups()
print(calendar.timegm((int(y), MONTHS.index(mon) + 1, int(d), int(hh), int(mm), int(ss), 0, 0, 0)))
')"
fi

python3 - "$LAST_MSEC" "$MODE" <<'PY'
import os, sys, time

last = float(sys.argv[1])
mode = sys.argv[2]

# An explicit anchor makes the shift deterministic: both stacks given the same value land
# on exactly the same clock, whenever each of them happens to run.
anchor = os.environ.get("SHIFT_ANCHOR_EPOCH")
anchor = float(anchor) if anchor else time.time()

if mode == "--align-hour":
    # End the dataset on the most recent whole hour. Two loaders that run within the same
    # clock hour compute an identical anchor, so the datasets stay aligned with each other.
    anchor = (int(anchor) // 3600) * 3600

delta = anchor - last

if mode == "--whole-days":
    # round DOWN to whole days so the shifted window never runs past its original
    # time-of-day, and the diurnal curve stays aligned to real clock hours
    day = 86400.0
    delta = (int(delta // day)) * day
elif mode not in ("now", "", "--align-hour"):
    sys.exit("unknown mode: %s (use --align-hour, --whole-days, or nothing)" % mode)

print(int(delta * 1e9))
PY
