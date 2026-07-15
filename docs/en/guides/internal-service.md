# Internal Course Graph Service

The internal service separates the data plane from the control plane. A caller
packs a Document Package v1.1 directory into a `.hepkg.tar.gz` archive and
uploads it over HTTP via `POST /v1/runs`. Hyper-Extract never starts Docling,
never accepts `file://` URIs or request-level API keys, and never exposes the
shared `/exchange` volume to external callers.

The caller-owned [ExtractionBrief](extraction-brief.md) YAML must live inside
the package and be declared by `manifest.extraction_brief`. The API does not
accept request-level system prompt text or an external brief path. HE validates
the brief bytes and includes the normalized intent in package, prompt, and
checkpoint fingerprints.

## Transport and content contract

The Document Package v1.1 directory is the **content contract**; the
`.tar.gz` is only a **transport envelope**. Two distinct hashes must be
supplied and are validated independently:

- `package_fingerprint` — the canonical SHA-256 of the *extracted* Document
  Package (computed by `document_package_fingerprint`).
- `transport_sha256` — the SHA-256 of the uploaded `.tar.gz` byte stream.

The archive root must **directly** contain `manifest.json`, `outline.json`,
`provenance.jsonl`, `extraction-brief.yaml`, and `content/`. A nested
top-level directory is rejected with `PACKAGE_ARCHIVE_INVALID`.

The compressed upload limit is configured with `HE_SERVICE_MAX_UPLOAD_BYTES`
and defaults to `500000000` bytes (500 MB). The limit is enforced incrementally
while reading the request; it is not hard-coded in the route and oversized
requests are stopped before the full body is written to disk.

The HE Worker reads `HE_SERVICE_PIPELINE_MAX_WORKERS` to determine how many
document chunks a single run may process concurrently. It defaults to `2` and
must be a positive integer. This is an operator setting and cannot be overridden
through `POST /v1/runs`. Effective generation and embedding concurrency is the
smaller of this value and each Model Profile capability's
`recommended_concurrency`.

Discover the contract with:

```bash
curl http://he-api:8000/v1/contracts/document-package/v1
```

## Readiness

`GET /health/ready` runs six checks and collects every failure: `database`
(`SELECT 1`), `migration` (`alembic_version` equals the script head),
`package_root` (readable), `run_root` (create/fsync/delete a probe file),
`model_profiles` (the configured file parses and the default profile yields a
public descriptor), and `worker` (a heartbeat newer than
`2 * heartbeat_seconds`). Any failure returns `503` with the failed check
names in `error.details`; the response never includes database URLs or secret
values.

## Create a run

```bash
curl -X POST http://he-api:8000/v1/runs \
  -H 'Idempotency-Key: course-2026-001' \
  -F 'package=@course.hepkg.tar.gz;type=application/gzip' \
  -F 'contract_version=1.1' \
  -F 'package_fingerprint=<canonical-fingerprint>' \
  -F 'transport_sha256=<tarball-sha256>' \
  -F 'options={
    "pipeline":{"name":"course_graph","profile":{"name":"course_knowledge_graph","version":"1"}},
    "execution":{"model_profile":"openai-compatible-default","context_policy":"auto","priority":"normal"}
  }'
```

The API returns `202 Accepted` only after the upload is complete, the
transport hash matches, the tarball is safely extracted, the Document Package
contract and canonical fingerprint are validated, the Package is atomically
published to `/exchange/packages/pkg_<fingerprint>.hepkg/`, and a `queued` run
exists in PostgreSQL with its `work/`, `state/`, and `diagnostics/attempts/`
directories prepared. The `202` response carries stable HTTP links
(`self`, `result`, `artifacts`, `errors`) and never a `file://` URI.

Error codes: `400 PACKAGE_REQUIRED` / `INVALID_MULTIPART_REQUEST`,
`413 PACKAGE_UPLOAD_TOO_LARGE`, `409 IDEMPOTENCY_KEY_CONFLICT`,
`422 PACKAGE_ARCHIVE_INVALID` / `PACKAGE_EXPANDED_TOO_LARGE` /
`PACKAGE_TRANSPORT_HASH_MISMATCH` / `DOCUMENT_PACKAGE_HASH_MISMATCH` /
`DOCUMENT_PACKAGE_INVALID`, `500 PACKAGE_PUBLICATION_FAILED`.

## Observe a run

Poll `GET /v1/runs/{run_id}` (always `Cache-Control: no-store`). The response
merges the PostgreSQL lifecycle (status, attempt, lease) with two bounded state
files:

```text
/exchange/runs/<run_id>/state/
  progress.json  # high-frequency current snapshot and safe ticker messages
  timeline.json  # low-frequency fixed lifecycle summary
```

Timeline v1 always contains exactly these nine activities in this order:

```text
DOCUMENT_INGESTING -> CHUNK_PLANNING -> EXTRACTING_CHUNK
-> DEDUPLICATING -> BUILDING_GLOBAL_EDGES -> QUALITY_CHECKING
-> BUILDING_COMMUNITIES -> FINALIZING -> ARTIFACT_PUBLISHING
```

Each activity occurs once and has one of `pending`, `running`, `completed`,
`failed`, or `skipped`; at most one is `running`. Chunk progress updates the
existing `EXTRACTING_CHUNK` item rather than appending events. The Runner writes
`timeline.json` only on stage start/completion/failure/skip/recovery. The ticker
writes only `progress.json`. The API validates run ID, attempt, and lease owner,
overlays the valid progress snapshot onto the current timeline item, and derives
top-level `activity/message/message_seq/progress` from that same merged state.

While `running`, the snapshot is only accepted when its internal `worker_id`
matches the current DB lease owner; otherwise the API degrades to a stable,
stage-appropriate fixed nine-item timeline and `progress: null`. `percent` is computed only when
`current` and `total` are valid and is clamped to `0..100`; it is never
fabricated during long model calls (only the safe wait message and
`message_seq` advance). Recovery preserves completed steps and their original
start times, increments the current attempt, and cannot let an older worker
overwrite the accepted status. A missing, corrupt, or unknown timeline falls
back to the DB stage; in particular `stage=publish` maps to
`ARTIFACT_PUBLISHING`.

The response includes `timeline_schema_version: "1.0"`. Discover and lock the
machine-readable schemas with:

```text
GET /v1/contracts/run-status/v1
GET /v1/contracts/result-metadata/v1
```

Use `POST /v1/runs/{run_id}/cancel` and
`POST /v1/runs/{run_id}/resume`. Cancellation happens at checkpoint-safe
boundaries and the Worker finalizes a running cancellation to `cancelled`.
Resume uses the same logical run and existing `.he-run` files.
An accepted operator resume increments `attempt` and resets the automatic
Worker lease-recovery counter, giving the new attempt a fresh bounded recovery
window instead of immediately inheriting an exhausted one.

`GET /v1/runs/{run_id}/errors` returns the stable, redacted failure history
(attempt, code, source, message, occurred_at). It never exposes exception repr,
headers, provider bodies, keys, or full prompt content; those persist only under
`diagnostics/attempts/` for operator forensics.

## Consume artifacts

Download the main result with `GET /v1/runs/{run_id}/result`. It streams the
fixed `course-graph.json` declared in the artifact manifest, with
`Content-Type`, `Content-Length`, `Content-Disposition`, and a SHA-256-based
`ETag`. It is only served after the database reports `completed` AND the
publication is fully consistent (`_SUCCESS` + manifest hash + every declared
artifact size/SHA-256). It never accepts a caller-supplied filename or path.

Fetch the companion sanitized summary with
`GET /v1/runs/{run_id}/result-metadata`. It is available only for a completed,
fully validated publication and returns:

- fixed identity `HyperExtractResultMetadata` / `1.0`;
- run ID and `_SUCCESS` completion time;
- Profile name/version/content hash/prompt hash;
- optional ExtractionBrief identity;
- Course Graph media type, schema name, byte size, and SHA-256;
- wall elapsed seconds and chunk count;
- coverage, knowledge-point, relation, distribution, dangling-edge, and quality
  summaries.

It deliberately omits paths, provider/model requests, token or cost details,
prompts, logs, worker IDs, and leases. A client should stream `/result`, verify
its ETag, then cross-check run ID, size, SHA-256, schema, media type, Profile
version, and graph counts against `/result-metadata`. The file stream and the
metadata object are separate contracts; a public facade may parse and wrap
them, but must not expose HE paths or forward the attachment response directly.

`GET /v1/runs/{run_id}/artifacts` returns the full manifest. A completed
publication contains `artifact-manifest.json` and `_SUCCESS`; the marker is
written last. Verify every declared SHA-256 before importing the Course Graph.
Required artifacts include the Course Graph, run summary, quality report,
performance report, and cost report. `performance-report.json` separates the
current process wall time from cumulative model time so a zero-call resume is
not presented as a fresh full run.

Deployments may configure per-million-token rates:

```bash
HYPER_EXTRACT_INPUT_COST_PER_MILLION=1.0
HYPER_EXTRACT_OUTPUT_COST_PER_MILLION=4.0
HYPER_EXTRACT_EMBEDDING_INPUT_COST_PER_MILLION=0.1
HYPER_EXTRACT_COST_CURRENCY=USD
```

Generation and embedding tokens are priced independently. Missing rates leave
the corresponding token class unpriced; the report is `partially_priced` when
only some classes have rates, and `unpriced` with a `null` amount when none do.
Hyper-Extract does not guess live provider pricing.

The first deployment uses an HTTP multipart upload and a shared `/exchange`
volume between the API and Worker only. Object-storage sources are adapters
that must materialize the same immutable Document Package contract; they do not
change the task or graph APIs.
