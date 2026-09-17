#!/usr/bin/env python3
"""Query the HyperDX saved-object payload. Used by every verify-<integration>.sh.

Reads the combined /dashboards + /saved-search payload on stdin (separated by the
<<<DASHBOARDS>>> / <<<SEARCHES>>> markers each verifier writes) and answers one
query per invocation:

    tilecount:<dashboard name>          -> number of tiles, or nothing if absent
    search:<saved search name>          -> "yes", or nothing if absent
    groupby:<dashboard>:<tile name>     -> that tile's groupBy expression
    unscoped:<dashboard>                -> comma-separated tiles missing a log.stream filter
    tags:<dashboard>                    -> that dashboard's tags, comma-separated and sorted
    displaytypes:<dashboard>            -> tile displayTypes, comma-separated and sorted
    serieslimit:<dashboard>             -> any tile that sets seriesLimit (should be none;
                                           it bypasses the select-item `where`)
    sqltemplate:<dashboard>:<substr>    -> the stored sqlTemplate of the first tile whose
                                           name contains <substr>, macros left intact

`sqltemplate` exists so a check can run the SQL the tile ACTUALLY stores rather than a
hand-written equivalent. That distinction is not academic: a Drops Rate tile here passed a
hand-written check for weeks-equivalent while the stored query was subtly different and drew
the wrong series.

Shared by every verify-<integration>.sh, which is why nothing here
mentions either integration by name.

Kept separate from the shell script because extracting a multiIf containing commas,
quotes and brackets out of nested JSON is not a job for sed.
"""
import json
import sys


def load():
    raw = sys.stdin.read()
    try:
        dash = raw.split("<<<DASHBOARDS>>>", 1)[1].split("<<<SEARCHES>>>", 1)[0]
        srch = raw.split("<<<SEARCHES>>>", 1)[1]
    except IndexError:
        return [], []
    def parse(s):
        s = s.strip()
        if not s.startswith("["):
            return []
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return []
    return parse(dash), parse(srch)


def find(dashboards, name):
    for d in dashboards:
        if d.get("name") == name:
            return d
    return None


def tile_name(tile):
    """Tile title. The PERSISTED schema keeps it at config.name; the MCP save_dashboard
    API accepts it at the tile level and maps it inward. Read both so this works against
    either shape."""
    return tile.get("name") or (tile.get("config") or {}).get("name") or ""


def tile_filters(tile):
    """Every filter string attached to a tile, wherever the schema puts it.

    Note the naming mismatch: MCP's save_dashboard takes `where`/`whereLanguage` on each
    select item, but ClickStack PERSISTS them as `aggCondition`/`aggConditionLanguage`.
    Checking only for `where` reports every correctly-filtered tile as unfiltered."""
    cfg = tile.get("config", {}) or {}
    parts = [cfg.get("where") or "", cfg.get("sqlTemplate") or ""]
    for sel in cfg.get("select") or []:
        if isinstance(sel, dict):
            parts.append(sel.get("where") or "")
            parts.append(sel.get("aggCondition") or "")
    return " ".join(parts)


def main():
    if len(sys.argv) < 2:
        return 1
    q = sys.argv[1]
    dashboards, searches = load()

    if q.startswith("tilecount:"):
        d = find(dashboards, q[len("tilecount:"):])
        if d:
            print(len(d.get("tiles") or []))

    elif q.startswith("search:"):
        want = q[len("search:"):]
        if any(s.get("name") == want for s in searches):
            print("yes")

    elif q.startswith("groupby:"):
        _, dname, tname = q.split(":", 2)
        d = find(dashboards, dname)
        if d:
            for t in d.get("tiles") or []:
                if tile_name(t) == tname:
                    print((t.get("config") or {}).get("groupBy") or "")
                    break

    elif q.startswith("sqltemplate:"):
        _, dname, needle = q.split(":", 2)
        d = find(dashboards, dname)
        if d:
            for t in d.get("tiles") or []:
                if needle.lower() in tile_name(t).lower():
                    print((t.get("config") or {}).get("sqlTemplate") or "")
                    break

    elif q.startswith("tags:"):
        d = find(dashboards, q[len("tags:"):])
        if d:
            print(",".join(sorted(d.get("tags") or [])))

    elif q.startswith("serieslimit:"):
        # Report any tile on the dashboard that sets `seriesLimit`, with its value. It exists
        # because seriesLimit ranks series over UNCONDITIONAL volume and ignores the
        # select-item `where` that scopes the tile -- so on a shared table it silently empties
        # a chart. It has now done that twice in this project (nginx logs 2026-09-16, system
        # logs 2026-09-17), and NO verification path can catch it after the fact:
        # `clickstack_timeseries` has no seriesLimit parameter, so re-issuing the tile through
        # the builder tool cannot reproduce it, and `query_tiles` returns hasData/rowCount but
        # no values. A structural assertion that it is absent is the only defence.
        # SCOPED TO line/stacked_bar ON PURPOSE. HyperDX persists a pie/bar tile's `limit`
        # under the name `seriesLimit` in the stored document -- MCP hands it back as
        # `limit`, but this API does not -- so an unscoped check reports every pie and bar as
        # a violation. On pie/bar the key IS the legitimate row cap; only on line and
        # stacked_bar is it the trap that bypasses the select-item `where`.
        d = find(dashboards, q[len("serieslimit:"):])
        if d:
            out = ["%s=%s" % (t.get("name"), t.get("config", {}).get("seriesLimit"))
                   for t in d.get("tiles", [])
                   if t.get("config", {}).get("seriesLimit") is not None
                   and t.get("config", {}).get("displayType") in ("line", "stacked_bar")]
            print("\n".join(out))
    elif q.startswith("displaytypes:"):
        d = find(dashboards, q[len("displaytypes:"):])
        if d:
            print(",".join(sorted((t.get("config") or {}).get("displayType") or "?"
                                  for t in d.get("tiles") or [])))

    elif q.startswith("unscoped:"):
        d = find(dashboards, q[len("unscoped:"):])
        if d:
            missing = []
            for t in d.get("tiles") or []:
                cfg = t.get("config") or {}
                if cfg.get("displayType") == "markdown":
                    continue
                if "log.stream" not in tile_filters(t):
                    missing.append(tile_name(t) or t.get("id") or "?")
            if missing:
                print(", ".join(missing))

    return 0


if __name__ == "__main__":
    sys.exit(main())
