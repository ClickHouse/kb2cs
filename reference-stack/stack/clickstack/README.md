# ClickStack preloaded with the nginx dataset

The ClickStack counterpart to `../elastic/`. Same shape: `up -d` brings the platform up and
provisions it, one command loads the data, one script checks it.

```bash
cd stack/clickstack
docker compose up -d           # clickstack + registers a team, captures the API key
docker compose run --rm load   # ships all three log files through OTLP (~1-2 min)
./verify.sh                    # 15 checks
```

The collector never exits on its own, so `run --rm load` keeps streaming after the data has
landed — Ctrl-C once `./verify.sh` is happy. `./load.sh` does the same load but detects
completion and stops for you; see below.

| | |
|---|---|
| HyperDX | http://localhost:8080 — `train@example.com` / `TrainingP4ss!` |
| ClickHouse | `localhost:9000`, http://localhost:8123 — `default`, no password |
| OTLP | `:4317` gRPC, `:4318` HTTP |

Verified end to end: 1,011,404 rows in `otel_logs`, 499,964 unique `request_id`s (no
duplicates, no loss), severity mapped on every row, newest record ~2 minutes old.

## Why there is a `setup` service

Until a HyperDX team exists, the bundled opamp-managed collector runs with
`receivers: [nop]` and **ports 4317/4318 are not bound at all**. Send data before that and you
get connection-refused, not an auth error. `setup` registers the team, reads the ingestion key
back out of `GET /team`, and writes it to a shared volume. It shares ClickStack's network
namespace because the HyperDX API on `:8000` is internal to the container.

It is idempotent: on a restart it probes the stored key against the OTLP endpoint and does
nothing if it still works. If the mongo volume outlives the secrets volume, it logs back in
with the same credentials and re-reads the key.

## How the API key reaches the collector

The key does not exist until runtime, and the collector image is distroless — no shell to
export a variable with. So `load` runs the repo's real config
(`../../ingest/clickstack/otel-collector-nginx.yaml`, unmodified) plus a small overlay:

```yaml
exporters:
  otlphttp/clickstack:
    headers:
      authorization: ${file:/secrets/api-key}
```

Two `--config` flags, later wins. The collector's `file:` provider reads the secret directly.
The main config keeps `${env:CLICKSTACK_API_KEY}` for the hand-run case, so the two paths do
not drift.

## Start-up ordering

`docker compose up -d` returns when containers have *started*, not when `setup` has finished
registering the team — and until that happens the bundled collector runs `receivers: [nop]`
with 4317/4318 unbound. Running the shipper too early produces a stream of
`connection refused` retries and a load that never lands.

`load.sh` therefore waits for the OTLP port to answer before it starts anything. If you ship
data by some other route, wait for this first:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:4318/v1/logs \
  -H 'Content-Type: application/json' -d '{}'
# 000 = not ready yet.  401 = bound and ready (it is rejecting an unauthenticated probe).
```

## Two ways to load, and what each gives up

Both work. Compose handles the ordering either way, because `load` depends on `setup`
completing successfully and `setup` only exits 0 once OTLP is accepting data.

```bash
docker compose run --rm load   # plain compose, same shape as ../elastic/
./load.sh                      # same thing, but it knows when to stop
```

The difference is that **the OTel collector tails files and never exits**. It has no run-once
mode. So with plain compose you get no completion signal: the container sits there streaming
logs after the last line has landed, and you decide when to Ctrl-C. Nothing is wrong with the
data — you just have to poll `otel_logs` or run `./verify.sh` and guess at the timing.

`load.sh` exists for that one gap. It starts the same service, watches the row count until it
stops moving, stops the container, and reports the final number. It also adds two guards that
compose cannot express:

- **Refuses to run against a non-empty `otel_logs`.** Appending is how you get silent
  duplicates, which is a real failure mode on this platform rather than a hypothetical one.
- **Drops stale filelog checkpoints first**, so a half-cleaned environment cannot produce a
  run that ships nothing.

Use `docker compose run --rm load` if you want to watch it work. Use `./load.sh` if you want
it to finish and tell you.

To reload:

```bash
docker compose exec clickstack clickhouse-client --query "TRUNCATE TABLE default.otel_logs"
docker volume rm nginx-training-clickstack_checkpoints
./load.sh
```

The checkpoint volume must go too, or the filelog receiver resumes from its saved offsets and
reads nothing.

## Geo enrichment (for the dashboard migration)

The Kibana nginx dashboards include a map panel, and ClickStack has no geo data out of the
box — the collector does no enrichment. `./geoip.sh` closes that gap:

```bash
./geoip.sh    # ~3 min: downloads DB-IP City Lite, builds an ip_trie dictionary, backfills
```

It loads 3.65M IPv4 CIDR blocks into `geo.dbip_city`, exposes them as the `geo.dbip`
dictionary, and adds four MATERIALIZED columns to `otel_logs`: `geo_country_code`,
`geo_city`, `geo_latitude`, `geo_longitude`.

This is deliberately *not* a geoip processor in the collector. On the Elastic side the
enrichment is baked in at index time and changing it means a reindex; here it is a dictionary
lookup you can repoint at next month's DB-IP file and recompute with a mutation. That contrast
is one of the better arguments in the migration.

**It agrees with Elastic.** Same 499,964 requests, DB-IP here vs MaxMind on the Elastic side:

| country | ClickStack (DB-IP) | Elastic (MaxMind) | delta |
|---|---:|---:|---:|
| US | 177,022 | 182,268 | −2.9% |
| CN | 46,921 | 46,196 | +1.6% |
| JP | 26,249 | 25,785 | +1.8% |
| GB | 21,334 | 21,155 | +0.8% |

City level resolves too (Columbus 20,318 · London 14,000 · Chiyoda City 11,220).

93.2% of requests are located. The remainder is honest rather than broken: 16,955 IPv6
clients, which this dictionary does not cover, and 17,280 rows returning `ZZ` — exactly the
health-check probes from `10.0.1.21/22`, since private addresses have no public geolocation.

Two things to know:

- **The IPv6 guard is load-bearing.** `toIPv4OrDefault` turns an IPv6 address into `0.0.0.0`,
  which matches DB-IP's own `0.0.0.0/8` block and returns country `ZZ`. Without the guard in
  `geoip.sql`, all 34,301 IPv6 rows would render as a plausible-looking country on the map.
- **`otel_logs` inserts now depend on the dictionary.** The materialized columns call
  `dictGet` on every insert. The dictionary is sourced from a local table, not the URL, so it
  reloads without network — but dropping `geo.dbip` would break ClickStack's ingest.

## Persistence

**Data survives restarts — you do not need to reload every time.** Verified with a full
`docker compose down` followed by `up -d`: 1,011,404 rows, all three streams exact, 499,964
unique `request_id`s, and `setup` reporting *"existing API key still valid, nothing to do"*.

Four named volumes:

| volume | holds | survives `down` |
|---|---|---|
| `chdata` → `/var/lib/clickhouse` | the logs | yes |
| `mongodata` → `/data/db` | HyperDX team, users, saved searches, dashboards | yes |
| `secrets` | the ingestion API key | yes |
| `checkpoints` | how far the shipper read each file | yes |

```bash
docker compose stop     # keeps everything
docker compose down     # keeps the volumes, so keeps your data
docker compose down -v  # DESTROYS the data volumes
```

**`docker compose down -v` is not a complete teardown here.** Compose skips services behind a
profile, so the `load` container and its `secrets` and `checkpoints` volumes survive — leaving
an API key for a team that no longer exists and read-offsets pointing at EOF. Use:

```bash
docker compose --profile manual down -v
```

`load.sh` defends against the half-cleaned case anyway by dropping the checkpoints volume
before every run, and `setup.sh` re-provisions a key that no longer authenticates.

As with the Elastic stack, timestamps are frozen at load time. The collector config shifts the
window so the last event lands at *the moment you load*, so after a few days HyperDX's default
"Last 15 minutes" will be empty again. Truncate and re-run `./load.sh` to refresh it.

## Two permissions traps

Both cost a debugging cycle, and both are why `user: "0:0"` appears twice in the compose file:

1. **`setup` cannot write to the secrets volume as uid 100.** Docker creates named volumes
   root-owned; `curlimages/curl` runs as `curl_user`. Symptom:
   `can't create /secrets/api-key: Permission denied`.
2. **The collector cannot write to the checkpoints volume as uid 10001.** Symptom:
   `storage client: open /storage/receiver_filelog_nginx_error: permission denied`, and the
   collector exits immediately having ingested nothing.

## Running both stacks at once

Ports do not collide, so `../elastic/` and this can run side by side — which is the point, if
you are demonstrating a migration. Between them they want about 5 GB, so check Docker's memory
allocation first.

| | this stack | `../elastic/` |
|---|---|---|
| UI | 8080 | 5601 |
| Database | 8123, 9000 | 9200 |
| Ingest | 4317, 4318 | — |
