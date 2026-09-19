# Elasticsearch + Kibana with the nginx integration

A disposable stack for testing the **prebuilt Kibana nginx dashboards** against this dataset.

```bash
cd stack/elastic
docker compose up -d              # es + kibana, then installs the nginx integration
docker compose run --rm load      # backfill 511,440 log lines
./verify.sh                       # 13 checks, then prints the dashboard URLs
```

`./verify.sh` is the honest check: services up, integration installed, exact document counts,
zero grok failures, and every dashboard panel's underlying aggregation returning data inside
Kibana's default 24-hour window. It exits non-zero and names the fix command if something is
wrong — run it any time, not just after loading.

| | |
|---|---|
| Kibana | http://localhost:5601 — `elastic` / `changeme` |
| Elasticsearch | http://localhost:9200 — `elastic` / `changeme` |
| Stack version | 8.15.0, nginx integration 3.2.2 |
| Load time | ~2 minutes for 511,440 documents |

Verified end to end: 499,964 access + 11,476 error documents indexed, **zero rejections, zero
grok failures**, and every dashboard panel query returns data.

## What you get

The integration installs three dashboards. Two of them work with this dataset:

| Dashboard | Panels | Status |
|---|---:|---|
| **[Logs Nginx] Overview** | 8 | ✅ populated |
| **[Logs Nginx] Access and error logs** | 4 | ✅ populated |
| [Metrics Nginx] Overview | 8 | ❌ empty — see below |

Find them under **Analytics → Dashboard**, search `nginx`.

**The Metrics dashboard will stay empty and that is expected.** Its panels filter on
`data_stream.dataset: nginx.stubstatus`, which comes from polling nginx's `stub_status`
endpoint for live connection counters. That is a metrics source, not a log file, so nothing in
this dataset can populate it. Only the two log dashboards are in scope here.

## Two things this stack has to do that a naive setup misses

**1. Security cannot be turned off.** Fleet refuses to initialise without it —
`{"statusCode":403,"message":"Kibana security must be enabled to use Fleet"}` — and Fleet is
what installs the pipelines and dashboards. So the compose file enables authentication and
disables only TLS, which is the part that is genuinely safe to skip on localhost. That is also
why there is an `es-init` service: Kibana cannot start until `kibana_system` has a password,
and only the `elastic` superuser can set it.

**2. Documents must carry `data_stream.*` themselves.** Every panel on the two log dashboards
filters on `data_stream.dataset`. Elastic Agent sets that field automatically; a bulk loader
does not. Writing well-formed, fully-parsed documents into `logs-nginx.access-default` without
it produces a stack where everything looks healthy — 0 rejections, 0 grok failures, every ECS
field correctly populated — and **every dashboard panel is blank**. The loader therefore sets:

```json
"data_stream": {"type": "logs", "dataset": "nginx.access", "namespace": "default"},
"event":       {"dataset": "nginx.access", "module": "nginx"},
"service":     {"type": "nginx"}
```

These are `constant_keyword` in the integration's mapping, so the values must match the target
data stream name exactly; a mismatch is rejected at index time rather than ignored.

## Timestamps

Kibana's default time ranges are "Last 15 minutes" and "Last 24 hours", and the dataset is
dated 2026-08-17, so by default the loader shifts it to the present. It rewrites the timestamp
**inside each log line** before sending, so the integration's own grok and date processors
parse the shifted value and `message`, `nginx.access.time` and `@timestamp` all agree. That is
strictly better than the shift on the ClickStack side, which can only move `@timestamp` and
leaves the original date visible in the body.

```bash
docker compose run --rm load                              # last event lands at ~now
docker compose run --rm load python /load.py --whole-days # keep the diurnal peak on the clock
docker compose run --rm load python /load.py --no-shift   # keep the original 2026-08-17 dates
```

## About the map panel

`source.geo.*` is populated — the integration runs a `geoip` processor and Elasticsearch
downloads the GeoLite2 databases on startup. The top countries come out as US 182,268 · CN
46,196 · JP 25,785 · GB 21,155.

**None of that geography means anything.** The client IPs are synthetic random public
addresses, so GeoLite2 resolves them to wherever those ranges happen to live. The map panel
renders, which is the point for a dashboard walkthrough, but do not draw conclusions from it.

## Persistence

**Indexed data survives a restart — you do not need to reload every time.** Elasticsearch's
data directory is a named volume (`nginx-training-elastic_esdata`, ~180 MB once loaded), and
Kibana keeps its saved objects inside Elasticsearch, so the dashboards, the installed
integration and the ingest pipelines all persist with it.

Verified by a full `docker compose down` followed by `up -d`: 499,964 access docs, 11,476
error docs, 3 dashboards and 2 pipelines all still there.

```bash
docker compose stop     # keeps everything
docker compose down     # keeps the volume, so keeps your data
docker compose down -v  # DESTROYS the volume and the data
```

The Kibana encryption keys are pinned in the compose file for the same reason — left to
generate randomly, Kibana would lose its encrypted saved objects on every restart.

The `setup` service re-runs on each `up`. It is idempotent, just adds ~30 s.

### The one thing that does go stale

Timestamps are frozen at load time. The data was shifted so its last event sat at *the moment
you loaded it*, so after a few days Kibana's "Last 24 hours" will be empty again even though
every document is still there. Re-run the loader to re-shift:

```bash
docker compose run --rm load
```

That takes ~2 minutes and **replaces** rather than appends (see below), so it is safe to run
whenever the data has drifted out of the default time range.

## Reloading and cleanup

The loader replaces the data streams by default. This dataset is a fixed corpus rather than a
stream, so loading it twice is always a mistake — and one that hides well, leaving every count
exactly doubled with no error anywhere. Pass `--append` if you really want to add to what is
already indexed.

```bash
docker compose run --rm load                          # clears, then loads
docker compose run --rm load python /load.py --append # adds to what is there
```

Verify a load was clean:

```bash
curl -s -u elastic:changeme "http://localhost:9200/logs-*/_count" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"data_stream.dataset":"nginx.access"}}}'
# expect 499964
```

Tear the whole thing down, including the indexed data:

```bash
docker compose down -v
```

## Air-gapped

`setup.sh` pulls the package from `epr.elastic.co`. Without outbound network, run a local
registry and point Kibana at it:

```yaml
  package-registry:
    image: docker.elastic.co/package-registry/distribution:8.15.0
    ports: ["8080:8080"]
```

then add `XPACK_FLEET_REGISTRYURL: http://package-registry:8080` to the `kibana` service.

## How this relates to `ingest/elasticsearch/`

Two deliberately different paths, and they do not share indices:

| | `ingest/elasticsearch/` | this stack |
|---|---|---|
| Indices | `nginx-training-access-json`, `-access-combined`, `-error` | `logs-nginx.access-default`, `logs-nginx.error-default` |
| Parsing | hand-written grok + ingest pipelines in this repo | the integration's own pipelines |
| Purpose | compare hand-rolled ES modelling against ClickStack | drive the prebuilt dashboards |
| Loads | all three files, including `access.json.log` | `access.log` and `error.log` only |

The integration's pipeline expects the combined format, so `access.json.log` is not loaded
here — it describes the same 499,964 requests and would double every count.
