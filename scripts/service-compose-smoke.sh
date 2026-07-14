#!/usr/bin/env sh
# Deterministic, isolated Compose smoke test for the Hyper-Extract service.
#
# Brings up postgres / he-migrate / he-api / he-worker under a UNIQUE project
# name and exchange volume, with all provider keys empty, then verifies:
#   1. the API becomes ready (/health/ready returns 200),
#   2. the API and a helper container observe the SAME /exchange sentinel,
#   3. the sentinel survives an API + Worker restart.
#
# It NEVER submits a real extraction run, so it never calls a model provider.
# On exit (success or failure) it removes ONLY its own project's containers,
# networks and volumes via a trap — it never touches operator data.

set -eu

# --- Isolation: unique project name, volume, network and port -------------
SMOKE_ID="he-smoke-$(date +%s)-$$"
PROJECT_NAME="$SMOKE_ID"
export EXCHANGE_VOLUME_NAME="$SMOKE_ID-exchange"
export API_NETWORK_NAME="$SMOKE_ID-api"
export POSTGRES_PASSWORD="smoke-not-secret"
export HE_API_PORT="$((18000 + ($$ % 1000)))"
export PLATFORM="${PLATFORM:-linux/amd64}"
export MODEL_PROFILES_FILE="./model-profiles.example.toml"

# No provider keys: the smoke must not reach any model endpoint.
export OPENAI_API_KEY=""
export MIMIMAX_API_KEY=""
export EMBEDDING_API_KEY=""
export ANTHROPIC_API_KEY=""

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILES="-f docker/service.compose.yml -f docker/service.compose.dev.yml"
COMPOSE="docker compose --project-name $PROJECT_NAME --env-file docker/.env.example $COMPOSE_FILES"

cleanup() {
    echo "smoke: cleaning up project $PROJECT_NAME"
    $COMPOSE down --volumes --remove-orphans >/dev/null 2>&1 || true
    # Remove the uniquely-named exchange volume if it survived.
    docker volume rm "$EXCHANGE_VOLUME_NAME" >/dev/null 2>&1 || true
    docker network rm "$API_NETWORK_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "smoke: project=$PROJECT_NAME port=$HE_API_PORT exchange=$EXCHANGE_VOLUME_NAME"

# Probe readiness from INSIDE the api container on its fixed port 8000. This
# avoids any dependency on the host port mapping (which the dev override
# controls via HE_API_PORT and which can be shadowed by --env-file defaults).
api_ready() {
    $COMPOSE exec -T he-api python -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).status==200 else 1)" \
        >/dev/null 2>&1
}

echo "smoke: building image"
$COMPOSE build >/dev/null

echo "smoke: starting postgres / migrate / api / worker"
$COMPOSE up -d postgres he-migrate he-api he-worker >/dev/null

# --- 1. Wait for API readiness --------------------------------------------
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
    $COMPOSE logs he-api he-migrate postgres 2>&1 | tail -40 >&2
    exit 1
fi
echo "smoke: API is ready"

# --- 2. Shared /exchange sentinel visible to API and a helper -------------
SENTINEL="smoke-sentinel-$SMOKE_ID"
docker run --rm -v "$EXCHANGE_VOLUME_NAME:/exchange" alpine \
    sh -c "printf '%s' '$SENTINEL' > /exchange/.smoke-sentinel"

api_view="$($COMPOSE exec -T he-api cat /exchange/.smoke-sentinel)"
helper_view="$SENTINEL"
if [ "$api_view" != "$helper_view" ]; then
    echo "smoke: sentinel mismatch (api='$api_view' helper='$helper_view')" >&2
    exit 1
fi
echo "smoke: /exchange sentinel shared correctly"

# --- 3. Sentinel survives an API + Worker restart -------------------------
echo "smoke: restarting he-api and he-worker"
$COMPOSE restart he-api he-worker >/dev/null

ready=0
for _ in $(seq 1 60); do
    if api_ready; then
        ready=1
        break
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    echo "smoke: API did not become ready after restart" >&2
    exit 1
fi

api_view_after="$($COMPOSE exec -T he-api cat /exchange/.smoke-sentinel)"
if [ "$api_view_after" != "$SENTINEL" ]; then
    echo "smoke: sentinel lost after restart" >&2
    exit 1
fi

echo "smoke: OK — readiness, shared volume and restart persistence verified"
