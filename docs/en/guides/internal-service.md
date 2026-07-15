# Internal Course Graph Service

The internal service separates the data plane from the control plane. A parser
publishes a Document Package v1.1 directory to a shared `/exchange` volume. A
caller submits only its `file://` URI, canonical package fingerprint, and
server-side profile names. Hyper-Extract never starts Docling and API keys are
never accepted in task requests.

The caller-owned [ExtractionBrief](extraction-brief.md) YAML must live inside
the package and be declared by `manifest.extraction_brief`. The API does not
accept request-level system prompt text or an external brief path. HE validates
the brief bytes and includes the normalized intent in package, prompt, and
checkpoint fingerprints.

## Publish input

Write the package under `/exchange/packages/.staging-*`, validate it, then
atomically rename it to `/exchange/packages/<name>.hepkg`. API and Worker must
mount the same volume at the same path.

Discover the contract with:

```bash
curl http://he-api:8000/v1/contracts/document-package/v1
```

Validate before queueing. `contract_version` must match the package's
`manifest.schema_version`; a mismatch returns `DOCUMENT_PACKAGE_VERSION_MISMATCH`
and a non-standard layout returns `DOCUMENT_PACKAGE_LAYOUT_INVALID`:

```bash
curl -X POST http://he-api:8000/v1/document-packages/validate \
  -H 'Content-Type: application/json' \
  -d '{"contract_version":"1.1","package_uri":"file:///exchange/packages/course.hepkg/","sha256":"<canonical-fingerprint>"}'
```

## Readiness

`GET /health/ready` runs six checks and collects every failure: `database`
(`SELECT 1`), `migration` (`alembic_version` equals the script head), `package_root`
(readable), `run_root` (create/fsync/delete a probe file), `model_profiles`
(the configured file parses and the default profile yields a public descriptor),
and `worker` (a heartbeat newer than `2 * heartbeat_seconds`). Any failure
returns `503` with the failed check names in `error.details`; the response never
includes database URLs or secret values.

## Create and observe a run

```bash
curl -X POST http://he-api:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: course-2026-001' \
  -d '{
    "input":{"type":"document_package","contract_version":"1.1","package_uri":"file:///exchange/packages/course.hepkg/","package_format":"directory","sha256":"<canonical-fingerprint>"},
    "pipeline":{"name":"course_graph","profile":{"name":"course_knowledge_graph","version":"1"}},
    "execution":{"model_profile":"openai-compatible-default","context_policy":"auto","priority":"normal"}
  }'
```

Poll `GET /v1/runs/{run_id}`. It exposes the current stage, latest checkpoint
event, attempt, cancellation flag, resumability, and artifact links. Reuse the
same Idempotency Key after an uncertain create response; a changed request with
the same key is rejected.

Use `POST /v1/runs/{run_id}/cancel` and
`POST /v1/runs/{run_id}/resume`. Cancellation happens at checkpoint-safe
boundaries and the Worker finalizes a running cancellation to `cancelled`.
Resume uses the same logical run and existing `.he-run` files.

`GET /v1/runs/{run_id}/errors` returns the stable, redacted failure history
(attempt, code, source, message, occurred_at). It never exposes exception repr,
headers, provider bodies, keys, or full prompt content; those persist only under
`diagnostics/attempts/` for operator forensics.

## Consume artifacts

Only consume files declared by `GET /v1/runs/{run_id}/artifacts`. A completed
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

The first deployment supports shared-volume `file://` packages. HTTP and
object-storage sources are adapters that must materialize the same immutable
Document Package contract; they do not change the task or graph APIs.
