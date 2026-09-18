#!/usr/bin/env python3
"""Call a ClickStack MCP tool over plain HTTP.

The MCP server binds at session start, so it cannot be attached to a running session; this
is the documented workaround (same transport introspect-clickstack.py uses).

    python3 /tmp/cs.py <tool> '<json-args>'

Retries bodyless HTTP 400s: ClickStack 2.35.0-beta fails roughly every other request from a
single Python process with an empty 400, while answering the identical request from curl.
It is server-side session state, not a bad key (a bad key returns 401).
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# Environment-driven so the same helper talks to the reference stack in this repo and to a
# customer's HyperDX. The key comes from CLICKSTACK_API_KEY if set; otherwise it is read from
# the local container by `personal-key.sh`, which only works for the bundled all-in-one image.
URL = os.environ.get("CLICKSTACK_MCP_URL", "http://localhost:8080/api/mcp")
_key = None


def key():
    global _key
    if _key is None:
        # Either name works; introspect-clickstack.py documents the longer one.
        env = (os.environ.get("CLICKSTACK_API_KEY")
               or os.environ.get("CLICKSTACK_PERSONAL_API_KEY"))
        if env:
            _key = env.strip()
        else:
            repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            script = os.path.join(repo, "reference-stack", "stack", "clickstack",
                                  "personal-key.sh")
            _key = subprocess.run(
                [script], capture_output=True, text=True).stdout.strip().split("\n")[-1]
    return _key


def rpc(method, params, tries=8):
    """Raw JSON-RPC with the bodyless-400 retry."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last = None
    for _ in range(tries):
        req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + key(),
        })
        try:
            raw = urllib.request.urlopen(req, timeout=180).read().decode()
        except urllib.error.HTTPError as e:
            last = "HTTP %s: %s" % (e.code, e.read().decode()[:200])
            continue
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
        return json.loads(raw)
    raise SystemExit("all %d attempts failed; last: %s" % (tries, last))


def call(tool, args, tries=8):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    last = None
    for attempt in range(tries):
        # "Bearer <key>" here. Note the OTLP *ingestion* endpoint wants the bare key with no
        # scheme -- two different keys and two different schemes, easily confused.
        req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + key(),
        })
        try:
            raw = urllib.request.urlopen(req, timeout=180).read().decode()
        except urllib.error.HTTPError as e:
            last = "HTTP %s: %s" % (e.code, e.read().decode()[:300])
            continue
        # the endpoint answers as SSE even for a JSON request
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            last = "unparseable: %s" % raw[:300]
            continue
        if "error" in d:
            return {"_error": d["error"]}
        content = d.get("result", {}).get("content", [])
        out = []
        for c in content:
            t = c.get("text", "")
            try:
                out.append(json.loads(t))
            except json.JSONDecodeError:
                # A sampled column value can contain a literal control character, which
                # strict JSON rejects. Falling through to the raw string instead makes every
                # caller handle two return types; parsing it non-strictly does not.
                try:
                    out.append(json.loads(t, strict=False))
                except json.JSONDecodeError:
                    out.append(t)
            except TypeError:
                out.append(t)
        return out[0] if len(out) == 1 else out
    raise SystemExit("all %d attempts failed; last: %s" % (tries, last))


if __name__ == "__main__":
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call(tool, args), indent=1)[:6000])
