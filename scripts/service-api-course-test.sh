#!/usr/bin/env sh
# End-to-end local API acceptance run for a course Document Package.
#
# The script starts the local Docker stack, publishes the package into the
# shared exchange volume, validates the contract through HTTP, submits a real
# model run, follows progress, and copies completed artifacts to the product
# prototype's result directory.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_PACKAGE="$REPO_ROOT/../product-design/data/he-input/pmpbok-full.hepkg"
DEFAULT_OUTPUT_ROOT="$REPO_ROOT/../product-design/data/test-runs/pmpbok-full"

PACKAGE_SOURCE="$DEFAULT_PACKAGE"
OUTPUT_ROOT="$DEFAULT_OUTPUT_ROOT"
MODEL_PROFILE="minimax-course-default"
POLL_SECONDS=10
MAX_RESUMES=3
RUN_ID=""
FORCE_BUILD=0

usage() {
    cat <<EOF
Usage:
  $0
  $0 --package /path/to/document.hepkg
  $0 --run-id run_xxx

Options:
  --package PATH       Document Package directory to submit.
                       Default: $DEFAULT_PACKAGE
  --output-root PATH   Directory that receives completed artifacts.
                       Default: $DEFAULT_OUTPUT_ROOT
  --model-profile ID  Docker Model Profile name.
                       Default: $MODEL_PROFILE
  --poll-seconds N     API progress polling interval. Default: 10
  --max-resumes N      Automatically resume recoverable failures at most N
                       times. Default: 3
  --run-id ID          Monitor and collect an existing run instead of creating one.
  --build              Rebuild the service image before starting. Do not use
                       this while another extraction run is active.
  -h, --help           Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --package)
            [ "$#" -ge 2 ] || { echo "Missing value for --package" >&2; exit 2; }
            PACKAGE_SOURCE="$2"
            shift 2
            ;;
        --output-root)
            [ "$#" -ge 2 ] || { echo "Missing value for --output-root" >&2; exit 2; }
            OUTPUT_ROOT="$2"
            shift 2
            ;;
        --model-profile)
            [ "$#" -ge 2 ] || { echo "Missing value for --model-profile" >&2; exit 2; }
            MODEL_PROFILE="$2"
            shift 2
            ;;
        --poll-seconds)
            [ "$#" -ge 2 ] || { echo "Missing value for --poll-seconds" >&2; exit 2; }
            POLL_SECONDS="$2"
            shift 2
            ;;
        --max-resumes)
            [ "$#" -ge 2 ] || { echo "Missing value for --max-resumes" >&2; exit 2; }
            MAX_RESUMES="$2"
            shift 2
            ;;
        --run-id)
            [ "$#" -ge 2 ] || { echo "Missing value for --run-id" >&2; exit 2; }
            RUN_ID="$2"
            shift 2
            ;;
        --build)
            FORCE_BUILD=1
            shift
            ;;
        --no-build)
            # Backward-compatible no-op. Reusing the existing image is now
            # the safe default for long-running extraction jobs.
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$POLL_SECONDS" in
    ''|*[!0-9]*) echo "--poll-seconds must be a positive integer" >&2; exit 2 ;;
esac
[ "$POLL_SECONDS" -gt 0 ] || { echo "--poll-seconds must be greater than zero" >&2; exit 2; }
case "$MAX_RESUMES" in
    ''|*[!0-9]*) echo "--max-resumes must be a non-negative integer" >&2; exit 2 ;;
esac

for command in docker curl jq; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Required command not found: $command" >&2
        exit 1
    }
done

cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/docker/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE" >&2
    echo "Copy docker/.env.example to docker/.env and configure provider keys first." >&2
    exit 1
fi

compose() {
    docker compose --env-file "$ENV_FILE" \
        -f "$REPO_ROOT/docker/compose.yml" \
        -f "$REPO_ROOT/docker/compose.dev.yml" "$@"
}

prepare_database() {
    compose up -d postgres
    database_ready=0
    for _ in $(seq 1 60); do
        if compose exec -T postgres pg_isready -U hyperextract -d hyperextract \
            >/dev/null 2>&1; then
            database_ready=1
            break
        fi
        sleep 2
    done
    [ "$database_ready" -eq 1 ] || {
        echo "PostgreSQL did not become ready." >&2
        return 1
    }
    compose run --rm --no-deps he-api alembic upgrade head
}

COMPOSE_JSON="$(compose config --format json)"
API_PORT="$(printf '%s' "$COMPOSE_JSON" | jq -r '.services["he-api"].ports[0].published // "8000"')"
BASE_URL="http://127.0.0.1:$API_PORT"

if curl -fsS "$BASE_URL/health/ready" >/dev/null 2>&1; then
    echo "[docker] Reusing the healthy local API and Worker"
elif [ "$FORCE_BUILD" -eq 1 ]; then
    echo "[docker] Rebuilding and starting PostgreSQL, migration, API, and Worker"
    compose build he-api
    prepare_database
    compose up -d he-api he-worker
else
    RUNNING_SERVICES="$(compose ps --status running --services 2>/dev/null || true)"
    if printf '%s\n' "$RUNNING_SERVICES" | grep -qx 'he-worker'; then
        echo "[docker] Worker is active; starting only PostgreSQL, migration, and API"
        prepare_database
        compose up -d he-api
    else
        echo "[docker] Starting PostgreSQL, migration, API, and Worker without rebuilding"
        prepare_database
        compose up -d he-api he-worker
    fi
fi

echo "[health] Waiting for $BASE_URL/health/ready"
READY=0
for _ in $(seq 1 90); do
    if curl -fsS "$BASE_URL/health/ready" >/tmp/he-api-ready-$$.json 2>/dev/null; then
        READY=1
        break
    fi
    sleep 2
done
if [ "$READY" -ne 1 ]; then
    echo "API did not become ready." >&2
    compose ps >&2
    compose logs --tail=80 he-api he-worker postgres >&2
    exit 1
fi
rm -f /tmp/he-api-ready-$$.json
echo "[health] API is ready"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/he-api-course-test.XXXXXX")"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ -z "$RUN_ID" ]; then
    [ -d "$PACKAGE_SOURCE" ] || {
        echo "Document Package not found: $PACKAGE_SOURCE" >&2
        exit 1
    }
    [ -f "$PACKAGE_SOURCE/manifest.json" ] || {
        echo "manifest.json not found in: $PACKAGE_SOURCE" >&2
        exit 1
    }
    PACKAGE_SOURCE="$(cd "$PACKAGE_SOURCE" && pwd)"

    CONTRACT_VERSION="$(jq -r '.schema_version // empty' "$PACKAGE_SOURCE/manifest.json")"
    DOCUMENT_ID="$(jq -r '.document.id // "document"' "$PACKAGE_SOURCE/manifest.json")"
    SAFE_DOCUMENT_ID="$(printf '%s' "$DOCUMENT_ID" | tr -cs 'A-Za-z0-9._-' '-')"

    echo "[pack] Building .hepkg.tar.gz from $PACKAGE_SOURCE"
    tarball="$TMP_DIR/course.hepkg.tar.gz"
    tar -C "$PACKAGE_SOURCE" -czf "$tarball" .

    TRANSPORT_SHA256="$(shasum -a 256 "$tarball" | awk '{print $1}')"

    FINGERPRINT="$(compose exec -T he-api python -c \
        'import sys, tarfile, tempfile, shutil, os
from hyperextract.documents import document_package_fingerprint
src=sys.argv[1]
tmp=tempfile.mkdtemp(prefix=".fingerprint-")
try:
    with tarfile.open(src, "r:gz") as t:
        t.extractall(tmp)
    print(document_package_fingerprint(tmp))
finally:
    shutil.rmtree(tmp, ignore_errors=True)' \
        "$tarball" 2>/dev/null </dev/null)"
    # The exec above reads the tarball from the host path inside the container;
    # to make it portable, copy the tarball into the API container first.
    CONTAINER_TARBALL="/tmp/course-$$.hepkg.tar.gz"
    compose cp "$tarball" "he-api:$CONTAINER_TARBALL" >/dev/null 2>&1 || true
    FINGERPRINT="$(compose exec -T he-api python -c \
        'import sys, tarfile, tempfile, shutil
from hyperextract.documents import document_package_fingerprint
src=sys.argv[1]
tmp=tempfile.mkdtemp(prefix=".fp-")
try:
    with tarfile.open(src, "r:gz") as t:
        t.extractall(tmp)
    print(document_package_fingerprint(tmp))
finally:
    shutil.rmtree(tmp, ignore_errors=True)' \
        "$CONTAINER_TARBALL" | tail -n 1)"

    echo "[upload] multipart POST /v1/runs (contract=$CONTRACT_VERSION, fp=$FINGERPRINT)"
    IDEMPOTENCY_KEY="$SAFE_DOCUMENT_ID-api-$(date +%Y%m%d%H%M%S)-$$"
    CREATE_CODE="$(curl -sS -o "$TMP_DIR/create-response.json" -w '%{http_code}' \
        -X POST "$BASE_URL/v1/runs" \
        -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
        -F "package=@$tarball;filename=course.hepkg.tar.gz;type=application/gzip" \
        -F "contract_version=$CONTRACT_VERSION" \
        -F "package_fingerprint=$FINGERPRINT" \
        -F "transport_sha256=$TRANSPORT_SHA256" \
        -F "options={\"pipeline\":{\"name\":\"course_graph\",\"profile\":{\"name\":\"course_knowledge_graph\",\"version\":\"1\"}},\"execution\":{\"model_profile\":\"$MODEL_PROFILE\"},\"client_context\":{\"service\":\"local-api-acceptance\",\"task_id\":\"$IDEMPOTENCY_KEY\",\"course_id\":\"$DOCUMENT_ID\"}}")"
    case "$CREATE_CODE" in
        200|201|202) ;;
        *)
            echo "Run submission failed (HTTP $CREATE_CODE):" >&2
            jq . "$TMP_DIR/create-response.json" >&2 || cat "$TMP_DIR/create-response.json" >&2
            exit 1
            ;;
    esac
    RUN_ID="$(jq -r '.run_id // empty' "$TMP_DIR/create-response.json")"
    [ -n "$RUN_ID" ] || {
        echo "Run response did not contain run_id:" >&2
        cat "$TMP_DIR/create-response.json" >&2
        exit 1
    }
    echo "[submit] RUN_ID=$RUN_ID"
else
    echo "[resume-monitor] RUN_ID=$RUN_ID"
fi

on_interrupt() {
    printf '\n[monitor] Monitoring stopped. The server run continues.\n' >&2
    printf '[monitor] Reconnect with: %s --run-id %s\n' "$0" "$RUN_ID" >&2
    exit 130
}
trap on_interrupt INT TERM

echo "[monitor] Following API progress every $POLL_SECONDS seconds"
LAST_PROGRESS=""
RESUME_COUNT=0
while :; do
    if ! STATUS_CODE="$(curl -sS -o "$TMP_DIR/status.json" -w '%{http_code}' "$BASE_URL/v1/runs/$RUN_ID")"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] API temporarily unavailable; restoring API without touching Worker" >&2
        compose up -d postgres he-api >/dev/null 2>&1 || true
        sleep "$POLL_SECONDS"
        continue
    fi
    if [ "$STATUS_CODE" != "200" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Run status temporarily unavailable (HTTP $STATUS_CODE); retrying" >&2
        sleep "$POLL_SECONDS"
        continue
    fi

    RUN_STATUS="$(jq -r '.status' "$TMP_DIR/status.json")"
    PROGRESS="$(jq -r '[.stage, .stage_status, (.progress.message // ""), (.progress.current // "-"), (.progress.total // "-")] | @tsv' "$TMP_DIR/status.json")"
    if [ "$PROGRESS" != "$LAST_PROGRESS" ]; then
        printf '[%s] %s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$RUN_STATUS" "$PROGRESS"
        LAST_PROGRESS="$PROGRESS"
    fi

    case "$RUN_STATUS" in
        completed) break ;;
        failed)
            RESUMABLE="$(jq -r '.resumable // false' "$TMP_DIR/status.json")"
            if [ "$RESUMABLE" = "true" ] && [ "$RESUME_COUNT" -lt "$MAX_RESUMES" ]; then
                RESUME_COUNT=$((RESUME_COUNT + 1))
                ERROR_CODE="$(jq -r '.error_summary.code // "UNKNOWN"' "$TMP_DIR/status.json")"
                echo "[resume $RESUME_COUNT/$MAX_RESUMES] Recoverable failure $ERROR_CODE; resuming from checkpoint"
                RESUME_CODE="$(curl -sS -o "$TMP_DIR/resume-response.json" -w '%{http_code}' \
                    -X POST "$BASE_URL/v1/runs/$RUN_ID/resume")"
                if [ "$RESUME_CODE" = "202" ]; then
                    jq '{run_id,status,stage,stage_status,attempt}' "$TMP_DIR/resume-response.json"
                    LAST_PROGRESS=""
                    sleep "$POLL_SECONDS"
                    continue
                fi
                echo "Resume request failed (HTTP $RESUME_CODE):" >&2
                jq . "$TMP_DIR/resume-response.json" >&2 || cat "$TMP_DIR/resume-response.json" >&2
            fi
            echo "[result] Run ended with status: $RUN_STATUS" >&2
            jq . "$TMP_DIR/status.json" >&2
            curl -sS "$BASE_URL/v1/runs/$RUN_ID/errors" | jq . >&2 || true
            exit 1
            ;;
        cancelled)
            echo "[result] Run ended with status: $RUN_STATUS" >&2
            jq . "$TMP_DIR/status.json" >&2
            curl -sS "$BASE_URL/v1/runs/$RUN_ID/errors" | jq . >&2 || true
            exit 1
            ;;
    esac
    sleep "$POLL_SECONDS"
done

trap cleanup EXIT
echo "[result] Run completed; downloading result via HTTP"
RESULT_CODE="$(curl -sS -o "$TMP_DIR/course-graph.json" -w '%{http_code}' \
    "$BASE_URL/v1/runs/$RUN_ID/result")"
if [ "$RESULT_CODE" != "200" ]; then
    echo "Result download failed (HTTP $RESULT_CODE)" >&2
    exit 1
fi
curl -fsS "$BASE_URL/v1/runs/$RUN_ID/artifacts" >"$TMP_DIR/artifacts.json"
jq . "$TMP_DIR/artifacts.json"

OUTPUT_ROOT="$(mkdir -p "$OUTPUT_ROOT" && cd "$OUTPUT_ROOT" && pwd)"
OUTPUT_DIR="$OUTPUT_ROOT/api-$RUN_ID"
mkdir -p "$OUTPUT_DIR"
cp "$TMP_DIR/course-graph.json" "$OUTPUT_DIR/course-graph.json"
cp "$TMP_DIR/artifacts.json" "$OUTPUT_DIR/artifacts.json"

echo "[result] Artifacts copied to $OUTPUT_DIR"
jq '{
  run_id,
  outline_nodes: (.outline_nodes | length),
  knowledge_nodes: (.knowledge_nodes | length),
  semantic_edges: (.semantic_edges | length),
  structural_edges: (.structural_edges | length)
}' "$OUTPUT_DIR/course-graph.json"

echo "[done] Open the product prototype and select: full--api-$RUN_ID"
