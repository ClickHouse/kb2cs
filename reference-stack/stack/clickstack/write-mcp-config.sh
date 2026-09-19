#!/usr/bin/env bash
# Regenerate the repository's .mcp.json with this instance's live HyperDX personal API key.
#
#   ./reference-stack/stack/clickstack/write-mcp-config.sh
#   ...then restart Claude Code: MCP servers attach at session start, so approving one cannot
#   help a session already running.
#
# The key is baked in rather than read from an env var because Claude Code only sees the
# environment of the process that launched it -- exporting the variable in a different
# terminal silently yields "Missing environment variables" and a 401. The key is specific to
# your local, disposable stack and changes whenever the mongodata volume is recreated.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="$("$HERE/personal-key.sh")"
# The REPOSITORY root, not the reference stack's: Claude Code reads .mcp.json from the
# directory it was launched in. Resolved via git so it survives the layout moving again.
ROOT="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || echo "$HERE/../../..")"
OUT="$ROOT/.mcp.json"

python3 - "$KEY" "$OUT" <<'PY'
import json, sys
key, out = sys.argv[1], sys.argv[2]
json.dump({
  "$comment": [
    "MCP servers for the dashboard migration: read Elastic, write ClickStack.",
    "Regenerate after recreating the ClickStack stack:",
    "  ./reference-stack/stack/clickstack/write-mcp-config.sh",
    "Then restart Claude Code and approve both servers.",
    "",
    "The Elasticsearch MCP server has NO tools for Kibana saved objects, so dashboard",
    "definitions come from the Kibana REST API instead. See reference-stack/MIGRATION.md.",
    "",
    "Both stacks must be running before Claude Code starts, or the servers fail to connect."
  ],
  "mcpServers": {
    "elasticsearch": {
      "command": "npx",
      "args": ["-y", "@elastic/mcp-server-elasticsearch@0.3.1"],
      "env": {"ES_URL": "http://localhost:9200", "ES_USERNAME": "elastic", "ES_PASSWORD": "changeme"}
    },
    "clickstack": {
      "type": "http",
      "url": "http://localhost:8080/api/mcp",
      "headers": {"Authorization": "Bearer " + key}
    }
  }
}, open(out, "w"), indent=2)
print("  wrote", out, "with key", key[:8] + "...")
PY
