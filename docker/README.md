# Hyper-Extract service — Docker deployment

This directory contains the production Compose stack for the Hyper-Extract
internal service: a non-root image, a one-shot migration gate, a stateless API,
and one or more Workers. See
[`docs/zh/guides/internal-service.md`](../docs/zh/guides/internal-service.md)
for the HTTP/file contract this stack serves.

## Topology

```
                 service-api (internal, named)
   caller ─────────────────────────────────────── he-api
                                                     │
                            database (internal)      │
   postgres ◄──────────────────────────────────── he-api, he-worker, he-migrate
                                                     │
                            model-egress (egress)    │
   provider endpoints ◄─────────────────────────── he-worker
```

- **`database`** — internal-only. PostgreSQL, `he-migrate`, `he-api` and
  `he-worker` attach here. No outbound access.
- **`service-api`** — internal-only, but given a stable external name
  (`${API_NETWORK_NAME:-hyper-extract-api}`) so calling services can attach to
  it and reach `he-api`. Callers that join this network **cannot** reach
  PostgreSQL directly.
- **`model-egress`** — outbound access only. Only `he-worker` attaches here.

The API has **no model keys and no egress**. The Worker is the only component
that holds provider secrets (`env_file: .env`) and can reach model endpoints.

## Shared `/exchange` volume

Both `he-api` and `he-worker` mount the same named volume at `/exchange`. It is
declared with a stable external name so callers can mount it too:

```yaml
volumes:
  exchange-data:
    name: ${EXCHANGE_VOLUME_NAME:-hyper-extract-exchange}
```

A caller (another Compose project, a host process, or a sidecar) declares the
volume as external and writes Document Packages plus reads published artifacts
through it:

```yaml
volumes:
  hyper-extract-exchange:
    external: true
```

### Atomic publication

Publishers MUST write to a `.staging-<run_id>/` sibling directory and then
atomically rename it to the final `runs/<run_id>/` path. The Worker only
reconciles a run when `_SUCCESS` and `artifact-manifest.json` are both present
and consistent in the final directory. Partial publications are never
overwritten — the Worker fails with `ARTIFACT_STATE_INCONSISTENT` instead.

### Ownership and permissions

The image runs as UID/GID `10001:10001`. The `/exchange` volume must be
writable by that user. Do **not** use `chmod 777`. Operators set the volume
ownership once:

```sh
docker run --rm -v hyper-extract-exchange:/exchange alpine \
  chown -R 10001:10001 /exchange
```

and rely on `umask 0002` (set in the image) so group-writable artifacts are
shared cleanly between the service and callers running as UID 10001.

## Configuration

Copy `.env.example` to `.env` and fill in real values:

```sh
cp docker/.env.example docker/.env
# edit docker/.env — set POSTGRES_PASSWORD and add provider secrets for the Worker:
#   MIMIMAX_API_KEY=...
#   EMBEDDING_API_KEY=...
```

`docker/.env` is the single operator file. The Worker loads it via
`env_file: .env` (so it receives the provider keys); the API does **not** load
it, so it never sees model secrets. The same file supplies the interpolation
variables (`POSTGRES_PASSWORD`, `EXCHANGE_VOLUME_NAME`, `API_NETWORK_NAME`,
`HE_API_PORT`, `PLATFORM`, `HE_IMAGE`, `MODEL_PROFILES_FILE`).

Both the API and the Worker mount the same Model Profile TOML at
`/run/config/model-profiles.toml`. The API computes the secret-free Profile
fingerprint; the Worker additionally resolves the named secret env vars at run
time.

## Running the stack

```sh
docker compose --env-file docker/.env -f docker/service.compose.yml up -d
```

For local development (publishes the API on `127.0.0.1` only):

```sh
docker compose --env-file docker/.env \
  -f docker/service.compose.yml -f docker/service.compose.dev.yml up -d
```

The startup order is gated:

```
postgres healthy → he-migrate exits 0 → he-api + he-worker start
```

`he-migrate` runs `alembic upgrade head` exactly once (`restart: "no"`) and
owns the production schema. Runtime code never calls `create_all()`.

## Health, shutdown, and restart

| Service     | Healthcheck          | Stop grace | Restart          |
|-------------|----------------------|------------|------------------|
| `postgres`  | `pg_isready`         | default    | (default)        |
| `he-migrate`| none (one-shot)      | default    | `"no"`           |
| `he-api`    | `GET /health/ready`  | 20s        | `unless-stopped` |
| `he-worker` | none                 | 90s        | `unless-stopped` |

The API healthcheck probes `/health/ready` (database, migration head, volume
writability, Model Profile parse, recent Worker heartbeat) every 10s with a
3s timeout and a 20s start period. Compose will only route traffic to the API
once it reports ready.

The Worker deliberately has **no Docker HTTP healthcheck**: a single model
call can legitimately run for many minutes, and a naive liveness probe would
kill healthy work. Instead the Worker publishes a database heartbeat and renews
its task lease on an independent thread. A Worker that stops heartbeating is
detected by lease expiry, not by an HTTP probe. The 90-second stop grace
period lets in-flight model calls drain on `docker compose stop` before the
container is force-killed; lease renewal stops during shutdown so an expired
lease can be recovered by another replica.

## Scaling Workers

Workers claim runs from PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` and
renew leases independently, so replicas share the database and `/exchange`
without duplicate work:

```sh
docker compose --env-file docker/.env -f docker/service.compose.yml \
  up -d --scale he-worker=3
```

Crashed Workers' expired leases are requeued with `resume_from_checkpoint=true`
up to a bounded recovery count. Do not set `container_name` on scaled services.

## Backup and retention

- **PostgreSQL** — back up with `pg_dump` against the running `postgres`
  service. This holds run state, attempts, error history and leases.
- **`/exchange/runs`** — back this up only when cross-host resume is required.
  Published artifacts are immutable once `_SUCCESS` is written; a restore
  reconciles without rerunning the model.
- **`.he-run` Brief/Prompt snapshots** — written under
  `diagnostics/attempts/` per run. These are operator-debugging snapshots that
  may contain sensitive prompt material. Apply an explicit retention policy
  (e.g. prune snapshots older than N days); they are not required for resume.

## Destructive operations — read carefully

`docker compose down --volumes` **destroys the PostgreSQL database and the
`/exchange` run state permanently.** It is only used by the isolated smoke test
(`scripts/service-compose-smoke.sh`), which runs under a unique project name
and volume and cleans up only its own resources. Never run
`down --volumes` against the production project.
