#!/usr/bin/env python3
"""Report the ClickStack tile schema from a running server, so the migration is planned
against what the target actually accepts rather than against notes from another version.

    CLICKSTACK_PERSONAL_API_KEY=... python3 introspect-clickstack.py     # preferred
    python3 introspect-clickstack.py --key <personal-api-key>
    python3 introspect-clickstack.py --key-cmd <command that prints the key>
    python3 introspect-clickstack.py --url https://<host>/api/mcp --key ...
    python3 introspect-clickstack.py --key ... --json      # machine-readable

`--url` defaults to a local all-in-one container (http://localhost:8080/api/mcp); point it at
the deployment's own host otherwise. Prefer the environment variable over `--key`: an
argument is visible in `ps` and lands in shell history.

Prints:
  * the tool list
  * the `displayType` vocabulary, and whether a map type exists
  * where a filter belongs for each tile type -- the single most expensive thing to get
    wrong, because a misplaced predicate does not error, it just silently changes every number
  * the `aggFn` enum and the allowed `quantile` levels
  * the default `whereLanguage`

The key is the HyperDX **personal access key** (Team Settings > API Keys), which is NOT the
ingestion key used by the collector. Standard library only, no MCP client needed -- the
point is to be able to run this before a session has an MCP connection.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BUILDER_CHARTS = ("line", "stacked_bar", "table", "number", "pie", "bar")


ATTEMPTS = 5


def rpc(url, key, method, params, session=None, rid=1):
    """One JSON-RPC call to the MCP HTTP endpoint. Returns (result, session_id).

    Retries on an empty-bodied HTTP 400. Measured against ClickStack 2.35.0-beta, the
    endpoint fails *every other* request from one Python process with a bodyless 400 while
    answering 10/10 from curl -- byte-identical request line, headers and body, verified by
    capturing both. No header (Connection, User-Agent, Accept-Encoding) changes it, so it
    is server-side session state rather than anything the caller controls. A single retry
    is always enough; the alternation means attempt N+1 lands on the good parity.

    Do not "fix" this by reporting a stale key: the 400 carries no body, and a genuinely
    bad key returns 401.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": rid,
                       "method": method, "params": params}).encode()
    last = ""
    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        # the endpoint may answer as JSON or as a single SSE frame; accept both
        req.add_header("Accept", "application/json, text/event-stream")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        if session:
            req.add_header("mcp-session-id", session)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                sid = resp.headers.get("mcp-session-id") or session
            break
        except urllib.error.HTTPError as e:
            last = e.read().decode("utf-8", "replace")[:300]
            if e.code == 400 and attempt < ATTEMPTS - 1:
                time.sleep(0.2)
                continue
            hint = ("\nA 401/403 means the personal access key is stale -- it is "
                    "regenerated whenever the ClickStack mongo volume is recreated."
                    if e.code in (401, 403) else "")
            sys.exit(f"HTTP {e.code} from {url} on {method} "
                     f"(after {attempt + 1} attempt(s))\n{last}{hint}")
        except urllib.error.URLError as e:
            sys.exit(f"could not reach {url}: {e.reason}\nIs the stack up?")

    # SSE framing: lines of "event: ..." / "data: {...}". Take the first JSON object.
    payload = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            payload = json.loads(line)
            break
    if payload is None:
        sys.exit(f"unparseable response from {url}:\n{raw[:300]}")
    if "error" in payload:
        sys.exit(f"MCP error on {method}: {payload['error']}")
    return payload.get("result") or {}, sid


def deref(node, root, depth=0):
    """Resolve a local JSON-Schema $ref, e.g. #/properties/tiles/items/anyOf/0/..."""
    while isinstance(node, dict) and "$ref" in node and depth < 20:
        cur = root
        for seg in node["$ref"].lstrip("#/").split("/"):
            if not seg:
                continue
            try:
                cur = cur[int(seg)] if seg.isdigit() else cur[seg]
            except (KeyError, IndexError, TypeError):
                return node
        node, depth = cur, depth + 1
    return node


def classify_where(cfg_props, root):
    """Is a tile-level `where` allowed, forbidden, or absent?

    The schema marks the builder charts with {"not": {}} -- JSON Schema for "nothing
    validates here" -- plus a description pointing you at the select item. That is a
    deliberate signal, not an omission, and it is easy to misread as "key exists, so I
    may use it".
    """
    if "where" not in cfg_props:
        return "absent", ""
    w = deref(cfg_props["where"], root)
    if not isinstance(w, dict):
        return "absent", ""
    if "not" in w:
        return "forbidden", (w.get("description") or "")
    return "allowed", (w.get("description") or "")


def analyse(schema):
    out = {"displayTypes": [], "tiles": [], "aggFns": [], "quantileLevels": [],
           "whereLanguage": {}, "dashboardKeys": sorted((schema.get("properties") or {}).keys())}
    items = ((schema.get("properties") or {}).get("tiles") or {}).get("items") or {}
    branches = items.get("anyOf") or items.get("oneOf") or ([items] if items else [])

    for b in branches:
        cfg = ((b.get("properties") or {}).get("config") or {})
        cp = cfg.get("properties") or {}
        dt = cp.get("displayType") or {}
        const, enum = dt.get("const"), dt.get("enum")
        is_sql = (cp.get("configType") or {}).get("const") == "sql"

        placement, note = classify_where(cp, schema)
        sel = deref(cp.get("select"), schema) if "select" in cp else None
        item_where = False
        sel_kind = "absent"
        if isinstance(sel, dict):
            sel_kind = sel.get("type") or "?"
            if sel_kind == "array":
                sel_items = deref(sel.get("items"), schema) or {}
                iprops = (sel_items.get("properties") or {})
                item_where = "where" in iprops
                if not out["aggFns"]:
                    out["aggFns"] = ((iprops.get("aggFn") or {}).get("enum")) or []
                    out["quantileLevels"] = ((iprops.get("level") or {}).get("enum")) or []
                    wl = iprops.get("whereLanguage") or {}
                    if wl:
                        out["whereLanguage"] = {"enum": wl.get("enum"),
                                                "default": wl.get("default")}

        name = const or ("sql tile" if is_sql else "(no displayType)")
        if const:
            out["displayTypes"].append(const)
        out["tiles"].append({
            "tile": name,
            "renders_as": enum if enum else None,
            "tile_where": placement,
            "tile_where_note": note,
            "select": sel_kind,
            "select_item_where": item_where,
            "config_keys": sorted(cp.keys()),
        })
    return out


def call_tool(url, key, sid, name, args, rid=90):
    """Invoke an MCP tool and return its first JSON content block."""
    res, _ = rpc(url, key, "tools/call", {"name": name, "arguments": args},
                 session=sid, rid=rid)
    for c in res.get("content") or []:
        text = c.get("text")
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return {}


def report_sources(url, key, sid, tools, as_json):
    """The TARGET half of the data-view mapping.

    Every tile carries exactly one `sourceId`, so each Kibana data view has to be assigned
    to one of these. Match on three things, in this order:

      1. signal kind  -- a `logs-*` view belongs on a `log` source and a `metrics-*` view on
                         a `metric` source. This is not cosmetic: metric tiles need
                         `metricName`/`metricType`/`isDelta` on every select item, which log
                         tiles do not have.
      2. the columns  -- the fields the panels read must exist here. `inventory-panels.py
                         --sources` prints them per data view.
      3. the timestamp -- the data view's `timeFieldName` becomes this source's
                         `timestampValueExpression`.
    """
    if "clickstack_list_sources" not in tools:
        sys.exit("clickstack_list_sources is not exposed; cannot enumerate target sources")
    listing = call_tool(url, key, sid, "clickstack_list_sources", {})
    sources = listing.get("sources") if isinstance(listing, dict) else listing
    sources = sources or []

    out = []
    for s in sources:
        entry = {k: s.get(k) for k in ("id", "name", "kind", "connectionId",
                                       "timestampColumn")}
        if "clickstack_describe_source" in tools:
            d = call_tool(url, key, sid, "clickstack_describe_source",
                          {"sourceId": s.get("id")}, rid=91)
            src = (d or {}).get("source") or {}
            cfg = src.get("config") or {}
            cols = src.get("columns") or []
            entry["database"] = cfg.get("databaseName")
            entry["table"] = cfg.get("tableName")
            # A metric source has no single table: it fans out to one table per metric type
            # (gauge/sum/histogram/summary). That is exactly why a metric tile must carry
            # `metricType` on each select item while a log tile does not -- the type picks
            # the table. Surface it, because it is the shape that makes the metrics path
            # different rather than merely unfamiliar.
            entry["metric_tables"] = cfg.get("metricTables") or {}
            entry["columns"] = len(cols)
            entry["attr_columns"] = sorted(
                c["name"] for c in cols
                if isinstance(c.get("type"), str) and c["type"].startswith("Map("))
            # Materialized/promoted columns are reachable from the builder tools, so they
            # are part of what a tile can select -- worth seeing next to the map columns.
            entry["sample_columns"] = [c["name"] for c in cols][:14]
        out.append(entry)

    if as_json:
        json.dump({"sources": out}, sys.stdout, indent=2)
        print()
        return 0

    print("ClickStack sources (the targets a tile's `sourceId` can point at)\n")
    print(f"  {'kind':8} {'name':18} {'database.table':28} {'timestamp':14} cols")
    print("  " + "-" * 78)
    for e in out:
        mt = e.get("metric_tables") or {}
        tbl = ".".join(x for x in (e.get("database"), e.get("table")) if x)
        if not e.get("table") and mt:
            tbl = "%s.{%s}" % (e.get("database") or "?", "|".join(sorted(mt)))
        print(f"  {str(e.get('kind'))[:7]:8} {str(e.get('name'))[:17]:18} {tbl[:27] or '?':28} "
              f"{str(e.get('timestampColumn'))[:13]:14} {e.get('columns', '?')}")
        print(f"           id: {e.get('id')}")
        if e.get("attr_columns"):
            print(f"           attribute maps: {', '.join(e['attr_columns'])}")
        if mt:
            print("           one table per metric type: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(mt.items())))
            print("           -> a metric tile must set `metricType` per select item; "
                  "that is what picks the table")
    print("\nMap each Kibana data view onto one of these. Match the signal kind first "
          "(`logs-*` -> a log source, `metrics-*` -> a metric source: the tile keys differ), "
          "then confirm the fields the panels need exist on it, then check the data view's "
          "time field lines up with the source's timestamp column.")
    print("\nOne data view can legitimately map to several sources if the target split that "
          "data across tables -- and several data views can map to one source. The mapping "
          "is per (data view, target table), not per name.")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get(
        "CLICKSTACK_MCP_URL", "http://localhost:8080/api/mcp"))
    ap.add_argument("--key", default=(os.environ.get("CLICKSTACK_PERSONAL_API_KEY")
                       or os.environ.get("CLICKSTACK_API_KEY")))
    ap.add_argument("--key-cmd", help="shell command that prints the personal access key")
    ap.add_argument("--json", action="store_true", help="dump the analysis as JSON")
    ap.add_argument("--sources", action="store_true",
                    help="list the target's sources (the other half of the data-view "
                         "mapping; pair with inventory-panels.py --sources)")
    a = ap.parse_args(argv[1:])

    key = a.key
    if not key and a.key_cmd:
        key = subprocess.run(a.key_cmd, shell=True, capture_output=True,
                             text=True).stdout.strip()
    if not key:
        sys.exit("no key: pass --key, --key-cmd, or set CLICKSTACK_API_KEY "
                 "(CLICKSTACK_PERSONAL_API_KEY is also accepted)")

    init, sid = rpc(a.url, key, "initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "introspect-clickstack", "version": "1"}}, rid=1)
    server = init.get("serverInfo") or {}
    listing, _ = rpc(a.url, key, "tools/list", {}, session=sid, rid=2)
    tools = {t["name"]: t for t in listing.get("tools") or []}

    if a.sources:
        return report_sources(a.url, key, sid, tools, a.json)

    save = tools.get("clickstack_save_dashboard")
    if not save:
        sys.exit("clickstack_save_dashboard is not exposed; cannot read the tile schema")
    data = analyse(save.get("inputSchema") or {})
    data["server"] = server
    data["tools"] = sorted(tools)

    if a.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        return 0

    print(f"server: {server.get('name')} {server.get('version')}   ({a.url})")
    print(f"tools:  {len(tools)}")
    print(f"\ndisplayType vocabulary ({len(data['displayTypes'])}): "
          f"{' · '.join(data['displayTypes'])}")
    # Match "map" as a WORD, never as a substring -- "heatmap" contains "map" and is not a
    # map. A false positive here is the worst possible one: it would say a geo panel is
    # migratable when it is the one panel class that is not.
    maps = [d for d in data["displayTypes"]
            if any(tok in ("map", "geo", "geomap", "choropleth", "worldmap")
                   for tok in re.split(r"[^a-z0-9]+", d.lower()))]
    print("map tile type: " + (", ".join(maps) if maps
          else "NONE -- geo map panels cannot be migrated faithfully"))

    print("\nfilter placement per tile type")
    print(f"  {'tile':16} {'tile-level where':12} {'select':8} {'item where':10}")
    print("  " + "-" * 50)
    for t in data["tiles"]:
        print(f"  {str(t['tile'])[:15]:16} {t['tile_where']:12} "
              f"{str(t['select']):8} {str(t['select_item_where']):10}")
    forbidden = [t["tile"] for t in data["tiles"] if t["tile_where"] == "forbidden"]
    allowed = [t["tile"] for t in data["tiles"] if t["tile_where"] == "allowed"]
    if forbidden:
        print(f"\n  filter on EACH select item: {', '.join(forbidden)}")
        note = next((t["tile_where_note"] for t in data["tiles"]
                     if t["tile_where"] == "forbidden" and t["tile_where_note"]), "")
        if note:
            print(f"    schema says: {note}")
    if allowed:
        print(f"  filter at tile level:       {', '.join(allowed)}")

    sqls = [t for t in data["tiles"] if t["renders_as"]]
    for t in sqls:
        print(f"\n{t['tile']}: renders as {' | '.join(t['renders_as'])} "
              f"(note: not a displayType of its own)")

    if data["aggFns"]:
        print(f"\naggFn: {' · '.join(data['aggFns'])}")
    if data["quantileLevels"]:
        print(f"quantile levels: {data['quantileLevels']}  "
              "<- any other percentile needs a SQL tile")
    if data["whereLanguage"]:
        wl = data["whereLanguage"]
        print(f"whereLanguage: {wl.get('enum')}, DEFAULT = {wl.get('default')!r}"
              + ("  <- SQL expressions must set this explicitly"
                 if wl.get("default") != "sql" else ""))
    print(f"\ndashboard-level keys: {', '.join(data['dashboardKeys'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
