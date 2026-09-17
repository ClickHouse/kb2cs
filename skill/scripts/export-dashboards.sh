#!/usr/bin/env bash
# Export Kibana dashboards (with all referenced objects) to NDJSON on stdout.
#
#   export-dashboards.sh [options] [dashboard-id...]
#
# With no dashboard ids, LISTS the dashboards it can see instead of exporting — the usual
# first call, since the ids are not guessable.
#
#   # list, then export, using an API key (recommended)
#   export KIBANA_URL=https://my-deployment.kb.europe-west1.gcp.cloud.es.io
#   export KIBANA_API_KEY=VnVhQ2ZHY0JDZGJrU...
#   export-dashboards.sh --all-spaces
#   export-dashboards.sh --space obs-team abc-123 def-456 > dashboards.ndjson
#
#   # self-managed with basic auth
#   export-dashboards.sh --url http://localhost:5601 --user elastic --pass changeme
#
# AUTH -- two schemes, because deployments differ and only one of them works everywhere:
#   --api-key / KIBANA_API_KEY   sends `Authorization: ApiKey <key>`. Use the `encoded`
#                                field from POST /_security/api_key, not `id` or `api_key`.
#                                Required on Elastic Serverless, and the only option where
#                                SSO (SAML/OIDC) has displaced basic auth.
#   --user / --pass              basic auth. Fine self-managed; often unavailable on Cloud.
# Prefer the environment variables: a key passed as an argument is visible in `ps` and lands
# in shell history.
#
# SPACES -- saved objects are scoped to a space and so is every Kibana API. Objects in a
# non-default space are invisible to an unscoped call, which looks exactly like "this
# deployment has no dashboards". Pass --space, or --all-spaces to sweep every one. Most
# non-trivial deployments use spaces, so --all-spaces is the honest way to take an inventory.
#
# includeReferencesDeep pulls in index patterns, referenced visualizations and saved
# searches. Panels are often stored BY VALUE inside attributes.panelsJSON regardless, so
# the export is the source of truth either way. Pipe it to inventory-panels.py.
set -euo pipefail

URL="${KIBANA_URL:-}"; SPACE="${KIBANA_SPACE:-}"; ALL_SPACES=0; LIST_SPACES=0
API_KEY="${KIBANA_API_KEY:-}"; USER="${KIBANA_USER:-}"; PASS="${KIBANA_PASS:-}"
IDS=()

usage() { sed -n '2,34p' "$0" | sed 's/^# \?//' >&2; exit "${1:-64}"; }

# Legacy positional form, kept so older notes keep working:
#   export-dashboards.sh <kibana-url> <user> <pass> [id...]
if [ $# -ge 3 ] && [[ "$1" == http* ]] && [[ "$2" != --* ]] && [[ "$3" != --* ]]; then
  URL=$1; USER=$2; PASS=$3; shift 3
  IDS=("$@")
  set --
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --url)         URL=${2:-}; shift 2 ;;
    --space)       SPACE=${2:-}; shift 2 ;;
    --all-spaces)  ALL_SPACES=1; shift ;;
    --list-spaces) LIST_SPACES=1; shift ;;
    --api-key)     API_KEY=${2:-}; shift 2 ;;
    --user)        USER=${2:-}; shift 2 ;;
    --pass)        PASS=${2:-}; shift 2 ;;
    -h|--help)     usage 0 ;;
    --*)           echo "unknown option: $1" >&2; usage ;;
    *)             IDS+=("$1"); shift ;;
  esac
done

[ -n "$URL" ] || { echo "no Kibana URL (--url or KIBANA_URL)" >&2; usage; }
URL=${URL%/}

if [ -n "$API_KEY" ]; then
  AUTH=(-H "Authorization: ApiKey $API_KEY")
elif [ -n "$USER" ]; then
  AUTH=(-u "$USER:$PASS")
else
  echo "no credentials: set KIBANA_API_KEY, or --user/--pass" >&2; usage
fi

kb() { # kb <space-or-empty> <path> [curl args...]
  local space=$1 path=$2 prefix=""
  [ -n "$space" ] && [ "$space" != "default" ] && prefix="/s/$space"
  shift 2
  curl -sS --fail-with-body "${AUTH[@]}" -H 'kbn-xsrf: true' "$URL$prefix$path" "$@"
}

# Spaces can be absent (feature disabled, or a deployment that does not expose the API).
# Treat that as "one unnamed space" rather than an error, so the script still works there.
list_spaces() {
  local out
  if out=$(kb "" "/api/spaces/space" 2>/dev/null); then
    printf '%s' "$out" | python3 -c 'import json,sys
try:
    print("\n".join(s["id"] for s in json.load(sys.stdin)))
except Exception:
    print("default")'
  else
    echo "default"
  fi
}

if [ "$LIST_SPACES" = "1" ]; then
  list_spaces; exit 0
fi

# ---------------------------------------------------------------- listing
# Paginated deliberately. `_find` caps at per_page, so a deployment with more dashboards
# than one page silently returns a truncated list -- worse than an error, because nothing
# says the rest exist.
list_dashboards() { # list_dashboards <space> ; writes the space total to /tmp/.kbtotal
  local space=$1 page=1 total=0 got=0 parsed
  while :; do
    parsed=$(kb "$space" "/api/saved_objects/_find?type=dashboard&per_page=100&page=$page&fields=title" \
      | python3 -c 'import json,sys
d = json.load(sys.stdin)
objs = d.get("saved_objects", [])
for o in objs:
    print("%s\t%s" % (o.get("id",""), (o.get("attributes") or {}).get("title","")))
sys.stderr.write("%d %d\n" % (d.get("total", len(objs)), len(objs)))' 2>/tmp/.kbcount)
    read -r total got < /tmp/.kbcount
    if [ -n "$parsed" ]; then
      if [ "$ALL_SPACES" = "1" ]; then
        printf '%s\n' "$parsed" | sed "s/^/$space\t/"
      else
        printf '%s\n' "$parsed"
      fi
    fi
    [ "${got:-0}" -eq 0 ] && break
    page=$((page+1))
    [ $(( (page-1)*100 )) -ge "${total:-0}" ] && break
  done
  echo "${total:-0}" > /tmp/.kbtotal
}

if [ ${#IDS[@]} -eq 0 ]; then
  if [ "$ALL_SPACES" = "1" ]; then
    echo "Dashboards on $URL, by space (space / id / title):" >&2
    grand=0
    for s in $(list_spaces); do
      list_dashboards "$s"
      grand=$(( grand + $(cat /tmp/.kbtotal) ))
    done
    { echo; echo "$grand dashboard(s) across all spaces."
      echo "Re-run with --space <id> and the ids you want."; } >&2
  else
    echo "Dashboards on $URL${SPACE:+ (space: $SPACE)}:" >&2
    list_dashboards "$SPACE"
    { echo; echo "$(cat /tmp/.kbtotal) dashboard(s) in this space. Re-run with the ids you want."
      echo "Objects in OTHER spaces are not listed -- use --all-spaces to sweep them."; } >&2
  fi
  exit 0
fi

# ---------------------------------------------------------------- export
# Build {"objects":[{"type":"dashboard","id":"..."},...],"includeReferencesDeep":true}
#
# Each argument is re-split on whitespace and commas, so all of these work:
#   ... id1 id2 id3            (normal)
#   ... "$IDS"                 (a single space-separated string -- zsh does NOT word-split
#                               unquoted expansions, so this is the common accident)
#   ... id1,id2                (a comma list pasted from somewhere)
# Without this, Kibana receives one id containing spaces and answers 400 / "not found",
# quoting the whole list back as a single object id.
BODY=$(python3 -c 'import json, re, sys
ids = [i for arg in sys.argv[1:] for i in re.split(r"[,\s]+", arg.strip()) if i]
if not ids:
    sys.exit("no dashboard ids after splitting arguments")
print(json.dumps({"objects": [{"type": "dashboard", "id": i} for i in ids],
                  "includeReferencesDeep": True}))' "${IDS[@]}")

kb "$SPACE" "/api/saved_objects/_export" \
  -X POST -H 'Content-Type: application/json' -d "$BODY"
