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

The image entrypoint sets `umask 0002` before starting API, Worker or migration
commands, so files are created as `0664` and directories as `0775`. This keeps
artifacts group-writable for cooperating processes in GID 10001.

## Configuration

Copy `.env.example` to `.env` and fill in real values:

```sh
cp docker/.env.example docker/.env
# edit docker/.env — set POSTGRES_PASSWORD and add provider secrets for the Worker:
#   MINIMAX_API_KEY=...
#   EMBEDDING_API_KEY=...
```

`docker/.env` is the single operator file. The Worker loads it via
`env_file: .env` (so it receives the provider keys); the API does **not** load
it, so it never sees model secrets. For the environment-derived default Profile,
Compose passes only the non-secret model names and base URLs to the API; this
lets the API and Worker compute the same secret-free Profile fingerprint. The
same file supplies the interpolation
variables (`POSTGRES_PASSWORD`, `EXCHANGE_VOLUME_NAME`, `API_NETWORK_NAME`,
`HE_API_PORT`, `PLATFORM`, `HE_IMAGE`, `MODEL_PROFILES_FILE`).

`HE_IMAGE` names the image built from `docker/service.Dockerfile`; the same tag
is used by migration, API and Worker so Compose builds one service image for
all three roles.

Both the API and the Worker mount the same Model Profile TOML at
`/run/config/model-profiles.toml`. The API computes the secret-free Profile
fingerprint; the Worker additionally resolves the named secret env vars at run
time.

The example Profile starts with `probe_required = false`, so a fresh stack can
run using its conservative declared capabilities. To require observed provider
conformance in production, first create evidence in the persistent exchange
volume:

```sh
docker compose --env-file docker/.env -f docker/service.compose.yml run --rm \
  he-worker he model probe --profile minimax-m27 \
  --file /run/config/model-profiles.toml
```

The Worker stores probe evidence under `/exchange/probes` via `HE_PROBE_ROOT`.
After the command succeeds, set `probe_required = true` in the mounted Profile
file and restart the Worker. Evidence survives container replacement with the
rest of the exchange volume.

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
container is force-killed. Lease renewal continues while an in-flight call is
draining, then stops when the Worker exits; another replica can recover the
run after the final lease expires.

## Worker count and provider quotas

Run exactly one Worker process until a shared PostgreSQL/Redis rate-limit-group
coordinator is configured. Database leases prevent duplicate run ownership, but
they do not coordinate provider concurrency, RPM/TPM pause windows, or circuit
breaker state. `HE_SERVICE_WORKER_PROCESSES` therefore rejects values other than
`1`. The running Worker also holds an exclusive `/exchange/.he-worker.lock`, so
an accidentally scaled second replica exits instead of creating an independent
quota coordinator; do not use Compose `--scale` for Workers in this release.

Crashed Workers' expired leases are requeued with `resume_from_checkpoint=true`
up to a bounded recovery count. Every progress, failure, cancellation,
publication and completion transition verifies the live lease owner under a
database row lock, so a stale Worker cannot overwrite a replacement Worker.
Do not set `container_name` on the Worker service.

## Destructive operations — read carefully

`docker compose down --volumes` **destroys the PostgreSQL database and the
`/exchange` run state permanently.** The isolated smoke test
(`scripts/service-compose-smoke.sh`) is the only place that uses
`down --volumes`, and it does so under a unique project name and volume that it
cleans up via a `trap` — it never touches operator data.

### Smoke-test cleanup vs. operator data

| Operation | Smoke test | Operator stack |
|-----------|-----------|----------------|
| Project name | `he-smoke-<ts>-<pid>` (unique per run) | your stable project |
| Exchange volume | `he-smoke-<id>-exchange` (removed on exit) | `hyper-extract-exchange` (persistent) |
| `down --volumes` | yes, only its own resources | **never** against production |
| Provider keys | empty (no model calls) | real keys in `docker/.env` |

The smoke script verifies readiness, shared-volume visibility, restart
persistence, and a new Worker heartbeat after restart **without submitting a
real extraction run**. Run it from the repo root:

```sh
sh scripts/service-compose-smoke.sh
```

### Operator backup policy

- **PostgreSQL** — back up with `pg_dump` against the running `postgres`
  service. This holds run state, attempts, error history and leases.
- **`/exchange/runs`** — back this up only when cross-host resume is required.
  Published artifacts are immutable once `_SUCCESS` is written; a restore
  reconciles without rerunning the model.
- **`.he-run` Brief/Prompt snapshots** — written under `diagnostics/attempts/`
  per run. These are operator-debugging snapshots that may contain sensitive
  prompt material. Apply an explicit retention policy (e.g. prune snapshots
  older than N days); they are not required for resume.

## Real-provider acceptance runbook (opt-in, manual)

The deterministic smoke test never calls a model. Before a real release,
perform this manual acceptance once against a small package:

1. Inject the Worker's real provider keys into `docker/.env`
   (`MINIMAX_API_KEY`, `EMBEDDING_API_KEY`).
2. Publish one small immutable Document Package `1.1` (with a Brief) to
   `/exchange/packages/<name>.hepkg/`.
3. `POST /v1/document-packages/validate` with `contract_version: "1.1"` and the
   canonical fingerprint; confirm `200`.
4. `POST /v1/runs` with the same package and a stable `Idempotency-Key`.
5. Poll `GET /v1/runs/{run_id}` until `status=completed`.
6. `GET /v1/runs/{run_id}/artifacts`; verify `_SUCCESS` exists and every
   declared artifact SHA-256 matches the file on disk.
7. Inspect `run-summary.json` and confirm `extraction_brief` reflects the
   package Brief.

Never make this procedure part of deterministic CI — it spends model budget and
is non-deterministic.
