#!/usr/bin/env bash

# Hyper-Extract production deployment entrypoint.
# Persistent paths: docker/data/postgres and docker/data/exchange.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/docker/.env"
COMPOSE_FILE="$PROJECT_ROOT/docker/compose.yml"
DATA_ROOT="$PROJECT_ROOT/docker/data"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

log() {
    printf '\n==> %s\n' "$1"
}

die() {
    printf '错误: %s\n' "$1" >&2
    exit 1
}

compose() {
    "${COMPOSE[@]}" "$@"
}

on_error() {
    local status="$1"
    local line="$2"

    trap - ERR
    set +e
    printf '\n部署失败: 第 %s 行，退出码 %s\n' "$line" "$status" >&2
    printf '\n容器状态:\n' >&2
    compose ps >&2 || true
    printf '\n最近日志:\n' >&2
    compose logs --tail 80 postgres he-api he-worker >&2 || true
    exit "$status"
}

trap 'on_error $? $LINENO' ERR

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

check_prerequisites() {
    require_command git
    require_command docker

    docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2"
    docker info >/dev/null 2>&1 || die "无法连接 Docker daemon"
    [[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE，请先复制 docker/.env.example"

    cd "$PROJECT_ROOT"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "当前目录不是 Git 仓库"
    git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' \
        >/dev/null 2>&1 || die "当前分支没有 upstream"
    if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
        die "工作区存在未提交或未跟踪文件，拒绝部署"
    fi
}

update_checkout() {
    local before_pull
    local after_pull

    log "拉取最新代码"
    before_pull="$(git rev-parse HEAD)"
    git pull --ff-only
    after_pull="$(git rev-parse HEAD)"

    if [[ "$before_pull" != "$after_pull" && "${HE_DEPLOY_REEXEC:-0}" != "1" ]]; then
        log "使用更新后的部署脚本继续"
        exec env HE_DEPLOY_REEXEC=1 "$SCRIPT_PATH"
    fi
}

prepare_storage() {
    log "准备宿主机数据目录"
    mkdir -p "$DATA_ROOT/postgres" "$DATA_ROOT/exchange"
    export HE_DATA_ROOT="$DATA_ROOT"

    compose config --quiet

    log "构建服务镜像"
    compose build he-api

    log "初始化 exchange 权限"
    compose run --rm --no-deps --user 0:0 he-api sh -eu -c \
        'mkdir -p /exchange/uploads /exchange/packages /exchange/runs /exchange/probes && chown -R 10001:10001 /exchange && chmod 0775 /exchange /exchange/uploads /exchange/packages /exchange/runs /exchange/probes'
}

wait_for_postgres() {
    local attempt
    for attempt in $(seq 1 60); do
        if compose exec -T postgres pg_isready -U hyperextract -d hyperextract \
            >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    printf 'PostgreSQL 在 120 秒内未就绪\n' >&2
    return 1
}

wait_for_api() {
    local attempt
    for attempt in $(seq 1 90); do
        if compose exec -T he-api python -c \
            "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).status == 200 else 1)" \
            >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    printf 'API 在 180 秒内未通过 /health/ready\n' >&2
    return 1
}

deploy_services() {
    log "启动 PostgreSQL"
    compose up -d postgres
    wait_for_postgres

    log "进入短暂维护窗口"
    compose stop -t 20 he-api || true
    compose stop -t 90 he-worker || true

    log "执行数据库迁移"
    compose run --rm --no-deps he-api alembic upgrade head

    log "启动 API 与 Worker"
    compose up -d --remove-orphans he-api he-worker
    wait_for_api
}

report_success() {
    local revision
    revision="$(git rev-parse --short HEAD)"

    log "部署完成"
    printf 'Git revision: %s\n' "$revision"
    printf '数据目录: %s\n' "$DATA_ROOT"
    printf '镜像:\n'
    compose config --images | sort -u
    printf '\n服务状态:\n'
    compose ps
}

main() {
    check_prerequisites
    update_checkout
    prepare_storage
    deploy_services
    report_success
}

main "$@"
