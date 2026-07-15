#!/usr/bin/env sh
# Deterministic, isolated Compose smoke test for the Hyper-Extract service.
#
# The test uses a unique project and a mktemp-backed HE_DATA_ROOT. It runs the
# production migration command explicitly and never touches docker/data or a
# model provider.

set -eu

SMOKE_ID="he-smoke-$(date +%s)-$$"
PROJECT_NAME="$SMOKE_ID"
SMOKE_TMP_BASE="${TMPDIR:-/tmp}"
SMOKE_DATA_ROOT="$(mktemp -d "$SMOKE_TMP_BASE/he-smoke-data.XXXXXX")"
export HE_DATA_ROOT="$SMOKE_DATA_ROOT"
export API_NETWORK_NAME="$SMOKE_ID-api"
export POSTGRES_PASSWORD="smoke-not-secret"
export HE_API_PORT="$((18000 + ($$ % 1000)))"
export PLATFORM="${PLATFORM:-linux/amd64}"
export MODEL_PROFILES_FILE="./conf/model-profiles.example.toml"
export HE_IMAGE="$SMOKE_ID-service:dev"

# No provider keys: the smoke test must not reach a model endpoint.
export OPENAI_API_KEY=""
export MINIMAX_API_KEY=""
export EMBEDDING_API_KEY=""
export ANTHROPIC_API_KEY=""

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILES="-f docker/compose.yml -f docker/compose.dev.yml"
COMPOSE="docker compose --project-name $PROJECT_NAME --env-file docker/.env.example $COMPOSE_FILES"

cleanup() {
    echo "smoke: cleaning up project $PROJECT_NAME"
    $COMPOSE down --remove-orphans >/dev/null 2>&1 || true
    docker network rm "$API_NETWORK_NAME" >/dev/null 2>&1 || true
    docker image rm "$HE_IMAGE" >/dev/null 2>&1 || true
    case "$SMOKE_DATA_ROOT" in
        "$SMOKE_TMP_BASE"/he-smoke-data.*)
            rm -rf -- "$SMOKE_DATA_ROOT"
            ;;
        *)
            echo "smoke: refusing to remove unexpected path: $SMOKE_DATA_ROOT" >&2
            ;;
    esac
}
trap cleanup EXIT INT TERM

echo "smoke: project=$PROJECT_NAME port=$HE_API_PORT data=$HE_DATA_ROOT"

api_ready() {
    $COMPOSE exec -T he-api python -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).status==200 else 1)" \
        >/dev/null 2>&1
}

latest_worker_id() {
    $COMPOSE exec -T postgres psql -U hyperextract -d hyperextract -Atc \
        "SELECT worker_id FROM he_worker_heartbeats ORDER BY last_seen_at DESC LIMIT 1" \
        2>/dev/null
}

worker_running() {
    $COMPOSE ps --status running --services | grep -qx "he-worker"
}

echo "smoke: building image"
$COMPOSE build he-api >/dev/null

echo "smoke: preparing exchange bind mount"
$COMPOSE run --rm --no-deps --user 0:0 he-api sh -eu -c \
    'mkdir -p /exchange/uploads /exchange/packages /exchange/runs /exchange/probes && chown -R 10001:10001 /exchange' \
    >/dev/null

echo "smoke: starting PostgreSQL"
$COMPOSE up -d postgres >/dev/null

postgres_ready=0
for _ in $(seq 1 60); do
    if $COMPOSE exec -T postgres pg_isready -U hyperextract -d hyperextract \
        >/dev/null 2>&1; then
        postgres_ready=1
        break
    fi
    sleep 2
done
if [ "$postgres_ready" -ne 1 ]; then
    echo "smoke: PostgreSQL did not become ready" >&2
    $COMPOSE logs postgres 2>&1 | tail -40 >&2
    exit 1
fi

echo "smoke: applying migrations"
$COMPOSE run --rm --no-deps he-api alembic upgrade head >/dev/null

echo "smoke: starting API and Worker"
$COMPOSE up -d he-api he-worker >/dev/null

echo "smoke: waiting for /health/ready"
ready=0
for _ in $(seq 1 60); do
    if api_ready; then
        ready=1
        break
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    echo "smoke: API did not become ready" >&2
    $COMPOSE logs he-api he-worker postgres 2>&1 | tail -40 >&2
    exit 1
fi
echo "smoke: API is ready"

SENTINEL="smoke-sentinel-$SMOKE_ID"
docker run --rm -v "$HE_DATA_ROOT/exchange:/exchange" alpine \
    sh -c "printf '%s' '$SENTINEL' > /exchange/.smoke-sentinel"

api_view="$($COMPOSE exec -T he-api cat /exchange/.smoke-sentinel)"
if [ "$api_view" != "$SENTINEL" ]; then
    echo "smoke: sentinel mismatch (api='$api_view' expected='$SENTINEL')" >&2
    exit 1
fi
echo "smoke: exchange bind mount is shared correctly"

echo "smoke: restarting API and Worker"
before_worker_id="$(latest_worker_id)"
if [ -z "$before_worker_id" ]; then
    echo "smoke: no Worker heartbeat found before restart" >&2
    exit 1
fi
$COMPOSE restart he-api he-worker >/dev/null

ready=0
after_worker_id=""
for _ in $(seq 1 60); do
    after_worker_id="$(latest_worker_id || true)"
    if worker_running && [ -n "$after_worker_id" ] && \
        [ "$after_worker_id" != "$before_worker_id" ] && api_ready; then
        ready=1
        break
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    echo "smoke: API/Worker did not return with a fresh Worker heartbeat" >&2
    exit 1
fi

api_view_after="$($COMPOSE exec -T he-api cat /exchange/.smoke-sentinel)"
if [ "$api_view_after" != "$SENTINEL" ]; then
    echo "smoke: sentinel lost after restart" >&2
    exit 1
fi

echo "smoke: OK — readiness, bind mount sharing and restart persistence verified"
