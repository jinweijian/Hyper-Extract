# Internal Course Graph Service

The internal service separates the data plane from the control plane. A parser
publishes a Document Package v1 directory to a shared `/exchange` volume. A
caller submits only its `file://` URI, canonical package fingerprint, and
server-side profile names. Hyper-Extract never starts Docling and API keys are
never accepted in task requests.

## Publish input

Write the package under `/exchange/packages/.staging-*`, validate it, then
atomically rename it to `/exchange/packages/<name>.hepkg`. API and Worker must
mount the same volume at the same path.

Discover the contract with:

```bash
curl http://he-api:8000/v1/contracts/document-package/v1
```

Validate before queueing:

```bash
curl -X POST http://he-api:8000/v1/document-packages/validate \
  -H 'Content-Type: application/json' \
  -d '{"contract_version":"1.0","package_uri":"file:///exchange/packages/course.hepkg/","sha256":"<canonical-fingerprint>"}'
```

## Create and observe a run

```bash
curl -X POST http://he-api:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: course-2026-001' \
  -d '{
    "input":{"type":"document_package","contract_version":"1.0","package_uri":"file:///exchange/packages/course.hepkg/","package_format":"directory","sha256":"<canonical-fingerprint>"},
    "pipeline":{"name":"course_graph","profile":{"name":"course_knowledge_graph","version":"1"}},
    "execution":{"model_profile":"minimax-course-default","context_policy":"auto","priority":"normal"}
  }'
```

Poll `GET /v1/runs/{run_id}`. It exposes the current stage, latest checkpoint
event, attempt, cancellation flag, resumability, and artifact links. Reuse the
same Idempotency Key after an uncertain create response; a changed request with
the same key is rejected.

Use `POST /v1/runs/{run_id}/cancel` and
`POST /v1/runs/{run_id}/resume`. Cancellation happens at checkpoint-safe
boundaries. Resume uses the same logical run and existing `.he-run` files.

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
HYPER_EXTRACT_COST_CURRENCY=USD
```

Without both rates, `cost-report.json` still records tokens but is explicitly
`unpriced` with a `null` amount. Hyper-Extract does not guess live provider
pricing.

The first deployment supports shared-volume `file://` packages. HTTP and
object-storage sources are adapters that must materialize the same immutable
Document Package contract; they do not change the task or graph APIs.
