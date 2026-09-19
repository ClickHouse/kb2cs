#!/usr/bin/env python3
"""Connection configuration for the verification tooling.

Every endpoint and credential is read from the ENVIRONMENT with defaults that point at the
reference stack in this repo. That is the whole point: the same scripts verify this synthetic
corpus and a customer's own Elastic → ClickStack migration, with no edits.

    # against the reference stack in this repo -- nothing to set
    python3 verify-tiles-vs-elastic.py

    # against a real deployment
    export ES_URL=https://es.example.com:9200 ES_USER=svc_migration ES_PASSWORD=...
    export CLICKHOUSE_URL=https://abc.clickhouse.cloud:8443 CLICKHOUSE_PASSWORD=...
    export CLICKSTACK_MCP_URL=https://hdx.example.com/api/mcp CLICKSTACK_API_KEY=...
    python3 verify-tiles-vs-elastic.py

ClickHouse is reachable two ways and the choice is deliberate rather than a fallback chain:
set `CLICKHOUSE_URL` and it goes over HTTPS with credentials, which is the only route that
works for ClickHouse Cloud or any remote cluster. Leave it unset and it shells into a local
container via `docker compose exec`, which is how the bundled all-in-one image is reachable —
that image requires a password on 8123 that the container's own client does not need.

Credentials are never defaulted to anything real. `elastic:changeme` and the container name
below are this repo's throwaway local values; see README.md.
"""
import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- Elasticsearch (the source)
ES_URL = os.environ.get("ES_URL", "http://localhost:9200").rstrip("/")
ES_USER = os.environ.get("ES_USER", "elastic")
ES_PASSWORD = os.environ.get("ES_PASSWORD", "changeme")
ES_AUTH = "Basic " + base64.b64encode(
    ("%s:%s" % (ES_USER, ES_PASSWORD)).encode()).decode()

# NOTE there is deliberately no ES_LOGS_PREFIX / ES_METRICS_PREFIX here. An earlier version
# defined both and nothing ever read them: the expect_* modules name full index patterns
# (`metrics-nginx.stubstatus-default`), not prefix + suffix, because a customer writes their
# own expectations against their own index names anyway. Config that is settable and has no
# effect is worse than no config -- someone sets it and nothing happens.

# ---------------------------------------------------------------- ClickHouse (the target)
CH_URL = os.environ.get("CLICKHOUSE_URL", "").rstrip("/")
CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CH_DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "default")
# Only used when CLICKHOUSE_URL is unset: the local all-in-one container to exec into.
# `docker compose exec` needs the directory holding the compose file, which for this repo is
# the reference stack -- NOT this directory, since the harness is deliberately separate from
# the demo it was written against.
CH_CONTAINER = os.environ.get("CLICKSTACK_CONTAINER", "clickstack")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CH_COMPOSE_DIR = os.environ.get(
    "CLICKSTACK_COMPOSE_DIR",
    os.path.join(_REPO, "reference-stack", "stack", "clickstack"))


def table(name):
    """Fully-qualified target table, so a customer's non-`default` database works."""
    return "%s.%s" % (CH_DATABASE, name)


class QueryError(RuntimeError):
    pass


def ch_query(sql, fmt="JSONCompact"):
    """Run SQL against the target. Returns parsed JSON, or raw text for FORMAT TSV.

    Raises QueryError rather than returning a sentinel: a swallowed query error reads as
    "this tile has no data", which is the single most misleading failure mode in this repo.
    Callers that legitimately tolerate failure catch it.
    """
    out = _run(sql + " FORMAT " + fmt)
    if fmt != "JSONCompact":
        return out
    return json.loads(out) if out else {"meta": [], "data": []}


def ch_statement(sql):
    """Run a statement that returns no rows -- DDL, or `INSERT ... VALUES`.

    Separate from `ch_query` because that one appends `FORMAT ...`, which a statement
    rejects. Every other verifier in this directory is read-only and does not need this;
    `verify-ecs-source.py` does, because the only way to learn how the target treats an
    ECS-shaped table is to put one there.
    """
    return _run(sql)


def _run(q):
    if CH_URL:
        params = urllib.parse.urlencode({"database": CH_DATABASE})
        req = urllib.request.Request(CH_URL + "/?" + params, data=q.encode(),
                                     method="POST")
        req.add_header("X-ClickHouse-User", CH_USER)
        if CH_PASSWORD:
            req.add_header("X-ClickHouse-Key", CH_PASSWORD)
        try:
            out = urllib.request.urlopen(req, timeout=300).read().decode()
        except urllib.error.HTTPError as exc:
            raise QueryError(exc.read().decode()[:400]) from None
    else:
        p = subprocess.run(
            ["docker", "compose", "exec", "-T", CH_CONTAINER,
             "clickhouse-client", "--database", CH_DATABASE, "--query", q],
            capture_output=True, text=True, cwd=CH_COMPOSE_DIR,
            stdin=subprocess.DEVNULL)
        if p.returncode:
            raise QueryError(p.stderr[:400])
        out = p.stdout
    return out.strip()


def es_post(path, body):
    """POST to Elasticsearch and return the parsed response."""
    req = urllib.request.Request(
        ES_URL + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": ES_AUTH},
        method="POST")
    return json.load(urllib.request.urlopen(req, timeout=300))


def es_get(path):
    req = urllib.request.Request(ES_URL + path, headers={"Authorization": ES_AUTH})
    return json.load(urllib.request.urlopen(req, timeout=120))


def describe():
    """One line per endpoint, for a script to print before it starts asserting."""
    ch = CH_URL or ("docker compose exec %s (in %s)"
                    % (CH_CONTAINER, os.path.basename(CH_COMPOSE_DIR)))
    return ("source: %s as %s\ntarget: %s database=%s"
            % (ES_URL, ES_USER, ch, CH_DATABASE))
