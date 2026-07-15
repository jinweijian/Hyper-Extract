# Docker Bind Mount 与自动部署实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 PostgreSQL 与 `/exchange` 数据保存到 `docker/data`，移除常驻迁移服务，并提供一次命令完成 Git 更新、构建、迁移、启动和健康检查的部署脚本。

**Architecture:** Compose 只描述 PostgreSQL、API 与 Worker 的运行拓扑，持久化通过 `${HE_DATA_ROOT:-./data}` bind mount 完成。`scripts/deploy.sh` 成为生产部署编排器：先准备并构建，再进入短暂维护窗口，以 API 镜像运行一次性 Alembic，最后启动服务并等待 readiness。

**Tech Stack:** Bash、POSIX shell、Docker Compose v2、Docker bind mount、PostgreSQL 17、Alembic、pytest、PyYAML、Ruff。

## Global Constraints

- Python 保持 `>=3.11`，Python 命令通过 `uv` 执行。
- 不迁移或兼容任何旧 named volume 数据。
- 默认数据必须落到 `docker/data/postgres` 和 `docker/data/exchange`。
- Git 不跟踪数据库、上传包、运行状态、模型响应或产物。
- API 与 Worker 仍以 UID/GID `10001:10001` 运行。
- API 不接收模型密钥；只有 Worker 使用 `env_file: .env`。
- 生产 Compose 不发布宿主机端口。
- 数据库迁移只能在 API 与 Worker 停止后由部署脚本执行。
- 脚本不得执行 `git reset`、Alembic downgrade、数据目录删除或自动 Schema 回滚。
- 冒烟测试必须使用独立临时目录，不得访问正式 `docker/data`。
- 保留工作区已有服务层修改和未跟踪文件，不把它们混入本任务提交。

## 文件职责

- `docker/compose.yml`：bind mount、服务依赖和运行拓扑。
- `docker/data/.gitignore`：保留数据根目录并忽略全部运行数据。
- `docker/.env.example`：删除 named volume 变量，保留部署与模型变量模板。
- `scripts/deploy.sh`：生产部署唯一推荐入口。
- `scripts/service-compose-smoke.sh`：临时 bind mount 下的确定性 Compose 冒烟测试。
- `scripts/service-api-course-test.sh`：本地验收时显式执行迁移。
- `docker/README.md`：中文部署、数据维护、备份和故障说明。
- `tests/docker/test_service_docker_files.py`：Compose、部署脚本、冒烟隔离和文档的静态回归测试。

---

### Task 1: 将 Compose 持久化改为宿主机 bind mount

**Files:**
- Create: `docker/data/.gitignore`
- Modify: `docker/compose.yml`
- Modify: `docker/.env.example`
- Modify: `tests/docker/test_service_docker_files.py`

**Interfaces:**
- Consumes: 容器内路径 `/var/lib/postgresql/data`、`/exchange` 和现有 PostgreSQL healthcheck。
- Produces: `${HE_DATA_ROOT:-./data}/postgres`、`${HE_DATA_ROOT:-./data}/exchange`，以及只依赖健康 PostgreSQL 的 API/Worker。

- [ ] **Step 1: 用测试声明新 Compose 拓扑**

将旧迁移和 named volume 测试替换为：

```python
def test_compose_uses_host_data_directories_and_no_named_volumes(compose):
    services = compose["services"]
    assert "he-migrate" not in services
    assert "volumes" not in compose
    postgres = services["postgres"]
    assert postgres["environment"]["PGDATA"] == "/var/lib/postgresql/data/pgdata"
    assert (
        "${HE_DATA_ROOT:-./data}/postgres:/var/lib/postgresql/data"
        in postgres["volumes"]
    )
    exchange = "${HE_DATA_ROOT:-./data}/exchange:/exchange"
    assert exchange in services["he-api"]["volumes"]
    assert exchange in services["he-worker"]["volumes"]


def test_api_and_worker_depend_only_on_healthy_postgres(compose):
    services = compose["services"]
    for name in ("he-api", "he-worker"):
        assert services[name]["depends_on"] == {
            "postgres": {"condition": "service_healthy"}
        }
        assert services[name]["image"] == "${HE_IMAGE:-hyper-extract-service:dev}"
    assert "ports" not in services["postgres"]
```

更新 exchange 探测断言：

```python
assert "${HE_DATA_ROOT:-./data}/exchange:/exchange" in worker["volumes"]
```

将环境模板测试从必需键列表移除 `EXCHANGE_VOLUME_NAME`，并增加：

```python
assert "EXCHANGE_VOLUME_NAME" not in text
```

新增数据忽略测试：

```python
def test_docker_data_root_is_tracked_but_runtime_data_is_ignored():
    text = (ROOT / "docker/data/.gitignore").read_text().splitlines()
    assert text == ["*", "!.gitignore"]
```

- [ ] **Step 2: 运行 Docker 测试并确认旧拓扑导致失败**

Run:

```bash
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
```

Expected: FAIL，原因包括 `he-migrate` 仍存在、named volume 仍存在和 `docker/data/.gitignore` 不存在。

- [ ] **Step 3: 修改 Compose 和环境模板**

在 PostgreSQL 环境中增加：

```yaml
PGDATA: /var/lib/postgresql/data/pgdata
```

将 PostgreSQL、API 和 Worker 挂载分别改为：

```yaml
- ${HE_DATA_ROOT:-./data}/postgres:/var/lib/postgresql/data
- ${HE_DATA_ROOT:-./data}/exchange:/exchange
```

删除整个 `he-migrate` 服务、API/Worker 的迁移依赖和顶层 `volumes`。API 与 Worker 的 `depends_on` 只保留健康 PostgreSQL。

创建 `docker/data/.gitignore`：

```gitignore
*
!.gitignore
```

删除 `docker/.env.example` 的 `EXCHANGE_VOLUME_NAME` 段落。

- [ ] **Step 4: 验证 Compose 静态行为与解析**

Run:

```bash
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
docker compose --env-file docker/.env.example -f docker/compose.yml config --quiet
docker compose --env-file docker/.env.example \
  -f docker/compose.yml -f docker/compose.dev.yml config --quiet
```

Expected: Docker 测试 PASS，两个 Compose 命令退出码为 0。

- [ ] **Step 5: 提交 Compose 迁移**

```bash
git add docker/compose.yml docker/.env.example docker/data/.gitignore \
  tests/docker/test_service_docker_files.py
git commit -m "refactor(docker): persist service data on host"
```

---

### Task 2: 新增一键生产部署脚本

**Files:**
- Create: `scripts/deploy.sh`
- Modify: `tests/docker/test_service_docker_files.py`

**Interfaces:**
- Consumes: Task 1 的 Compose、`docker/.env`、当前 Git upstream 和 `he-api` 镜像。
- Produces: 可重复执行的 `scripts/deploy.sh`，退出码 0 表示 API readiness 已通过。

- [ ] **Step 1: 添加部署顺序与安全边界测试**

新增静态测试：

```python
def test_deploy_script_owns_pull_build_migrate_and_readiness():
    text = (ROOT / "scripts/deploy.sh").read_text()
    for required in (
        "set -Eeuo pipefail",
        "git pull --ff-only",
        "docker/data/postgres",
        "docker/data/exchange",
        "config --quiet",
        "build he-api",
        "up -d postgres",
        "pg_isready",
        "stop -t 20 he-api",
        "stop -t 90 he-worker",
        "run --rm --no-deps he-api alembic upgrade head",
        "up -d --remove-orphans he-api he-worker",
        "/health/ready",
        "HE_DEPLOY_REEXEC",
    ):
        assert required in text
    assert text.index("build he-api") < text.index("stop -t 20 he-api")
    assert text.index("stop -t 90 he-worker") < text.index("alembic upgrade head")
    assert "git reset" not in text
    assert "alembic downgrade" not in text


def test_deploy_script_reports_failure_diagnostics():
    text = (ROOT / "scripts/deploy.sh").read_text()
    assert "trap 'on_error" in text
    assert "compose ps" in text
    assert "logs --tail" in text
```

- [ ] **Step 2: 运行新测试并确认部署脚本缺失**

Run:

```bash
OPENAI_API_KEY="" uv run pytest \
  tests/docker/test_service_docker_files.py::test_deploy_script_owns_pull_build_migrate_and_readiness \
  tests/docker/test_service_docker_files.py::test_deploy_script_reports_failure_diagnostics -q
```

Expected: FAIL，`scripts/deploy.sh` 不存在。

- [ ] **Step 3: 实现脚本骨架、Git 更新与重执行**

脚本使用以下固定路径和 Compose 数组：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/docker/.env"
COMPOSE_FILE="$PROJECT_ROOT/docker/compose.yml"
DATA_ROOT="$PROJECT_ROOT/docker/data"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
```

前置检查必须验证命令、Docker daemon、`.env`、upstream 和干净工作区。Git 更新逻辑：

```bash
before_pull="$(git rev-parse HEAD)"
git pull --ff-only
after_pull="$(git rev-parse HEAD)"
if [[ "$before_pull" != "$after_pull" && "${HE_DEPLOY_REEXEC:-0}" != "1" ]]; then
    exec env HE_DEPLOY_REEXEC=1 "$SCRIPT_PATH"
fi
```

脚本固定导出生产数据根目录：

```bash
mkdir -p "$DATA_ROOT/postgres" "$DATA_ROOT/exchange"
export HE_DATA_ROOT="$DATA_ROOT"
```

- [ ] **Step 4: 实现构建、权限、迁移与健康等待**

脚本按以下命令顺序执行：

```bash
compose config --quiet
compose build he-api
compose run --rm --no-deps --user 0:0 he-api sh -eu -c \
  'mkdir -p /exchange/uploads /exchange/packages /exchange/runs /exchange/probes && chown -R 10001:10001 /exchange && chmod 0775 /exchange /exchange/uploads /exchange/packages /exchange/runs /exchange/probes'
compose up -d postgres
wait_for_postgres
compose stop -t 20 he-api
compose stop -t 90 he-worker
compose run --rm --no-deps he-api alembic upgrade head
compose up -d --remove-orphans he-api he-worker
wait_for_api
compose ps
```

其中 `compose()` 调用 `${COMPOSE[@]}`；`wait_for_postgres()` 最多 60 次、每次间隔 2 秒执行 `pg_isready`；`wait_for_api()` 最多 90 次、每次间隔 2 秒从 API 容器内部请求 `/health/ready`。超时返回非零状态。

错误 trap 调用：

```bash
compose ps >&2 || true
compose logs --tail 80 postgres he-api he-worker >&2 || true
```

- [ ] **Step 5: 验证脚本语法和静态行为**

Run:

```bash
bash -n scripts/deploy.sh
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
```

Expected: Bash 语法检查退出码为 0，Docker 测试 PASS。

- [ ] **Step 6: 提交部署入口**

```bash
git add scripts/deploy.sh tests/docker/test_service_docker_files.py
git commit -m "feat(docker): add one-command deployment"
```

---

### Task 3: 让冒烟与本地验收适配临时 bind mount

**Files:**
- Modify: `scripts/service-compose-smoke.sh`
- Modify: `scripts/service-api-course-test.sh`
- Modify: `tests/docker/test_service_docker_files.py`

**Interfaces:**
- Consumes: `${HE_DATA_ROOT}` bind mount 和一次性 Alembic 命令。
- Produces: 不接触 `docker/data`、不依赖 `he-migrate` 的测试与本地验收流程。

- [ ] **Step 1: 修改冒烟测试断言**

将 smoke 静态测试要求改为：

```python
def test_smoke_script_uses_temporary_bind_mount_and_explicit_migration():
    text = (ROOT / "scripts/service-compose-smoke.sh").read_text()
    for required in (
        "mktemp -d",
        "HE_DATA_ROOT",
        "run --rm --no-deps he-api alembic upgrade head",
        "up -d postgres",
        "up -d he-api he-worker",
        "down --remove-orphans",
        "before_worker_id",
        "after_worker_id",
    ):
        assert required in text
    assert "EXCHANGE_VOLUME_NAME" not in text
    assert "he-migrate" not in text
    assert "down --volumes" not in text
    assert 'case "$SMOKE_DATA_ROOT"' in text
```

- [ ] **Step 2: 运行测试并确认旧脚本失败**

Run:

```bash
OPENAI_API_KEY="" uv run pytest \
  tests/docker/test_service_docker_files.py::test_smoke_script_uses_temporary_bind_mount_and_explicit_migration -q
```

Expected: FAIL，旧脚本仍引用 named volume 和 `he-migrate`。

- [ ] **Step 3: 修改冒烟生命周期**

使用：

```sh
SMOKE_DATA_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/he-smoke-data.XXXXXX")"
export HE_DATA_ROOT="$SMOKE_DATA_ROOT"
```

cleanup 先执行 `down --remove-orphans`，然后只允许删除匹配 `${TMPDIR:-/tmp}/he-smoke-data.*` 的目录。启动顺序改为：

```sh
$COMPOSE build he-api >/dev/null
$COMPOSE run --rm --no-deps --user 0:0 he-api sh -eu -c \
    'mkdir -p /exchange/uploads /exchange/packages /exchange/runs /exchange/probes && chown -R 10001:10001 /exchange'
$COMPOSE up -d postgres >/dev/null
postgres_ready=0
for _ in $(seq 1 60); do
    if $COMPOSE exec -T postgres pg_isready -U hyperextract -d hyperextract >/dev/null 2>&1; then
        postgres_ready=1
        break
    fi
    sleep 2
done
[ "$postgres_ready" -eq 1 ] || {
    echo "smoke: PostgreSQL did not become ready" >&2
    exit 1
}
$COMPOSE run --rm --no-deps he-api alembic upgrade head >/dev/null
$COMPOSE up -d he-api he-worker >/dev/null
```

sentinel 辅助容器改为挂载 `$HE_DATA_ROOT/exchange:/exchange`。

- [ ] **Step 4: 同步修改本地验收脚本**

在 `scripts/service-api-course-test.sh` 启动栈之前增加：

```sh
compose up -d postgres
compose run --rm --no-deps he-api alembic upgrade head
```

执行 `sh -n scripts/service-api-course-test.sh` 验证语法。

- [ ] **Step 5: 验证并提交冒烟调整**

Run:

```bash
sh -n scripts/service-compose-smoke.sh
sh -n scripts/service-api-course-test.sh
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
```

Expected: 两个 Shell 语法检查退出码为 0，Docker 测试 PASS。

Commit:

```bash
git add scripts/service-compose-smoke.sh scripts/service-api-course-test.sh \
  tests/docker/test_service_docker_files.py
git commit -m "test(docker): isolate bind-mount smoke data"
```

---

### Task 4: 更新中文运维文档并执行完整验证

**Files:**
- Modify: `docker/README.md`
- Modify: `tests/docker/test_service_docker_files.py`

**Interfaces:**
- Consumes: Task 1–3 的实际目录、命令、维护窗口和失败边界。
- Produces: 以 `scripts/deploy.sh` 为唯一生产入口的中文部署手册。

- [ ] **Step 1: 用测试锁定新文档内容**

在 README 测试必需文本中增加：

```python
for required in (
    "./scripts/deploy.sh",
    "docker/data/postgres",
    "docker/data/exchange",
    "短暂维护窗口",
    "pg_dump",
    "bind mount",
):
    assert required in text
for removed in (
    "EXCHANGE_VOLUME_NAME",
    "hyper-extract-exchange",
    "he-migrate",
):
    assert removed not in text
```

- [ ] **Step 2: 运行 README 测试并确认旧文档失败**

Run:

```bash
OPENAI_API_KEY="" uv run pytest \
  tests/docker/test_service_docker_files.py::test_docker_readme_is_chinese_and_documents_configuration_boundaries -q
```

Expected: FAIL，旧文档仍描述 named volume 和 `he-migrate`。

- [ ] **Step 3: 重写相关 README 章节**

保持中文文档主体，更新以下内容：

- 目录结构增加 `data/` 和 `scripts/deploy.sh`；
- `/exchange` 和 PostgreSQL 改为 `docker/data` bind mount；
- 生产启动命令改为 `./scripts/deploy.sh`；
- 解释 Git 拉取、构建、权限初始化、停 API/Worker、一次性 Alembic、启动和 readiness；
- 本地直接 Compose 操作给出显式迁移命令；
- 删除 named volume 名称和 `he-migrate` 启动门禁；
- 说明 `docker compose down --volumes` 不删除 bind mount，删除 `docker/data` 才会丢失数据；
- PostgreSQL 在线备份继续使用 `pg_dump`，禁止运行时复制原始 PGDATA；
- 冒烟测试说明改为临时目录。

- [ ] **Step 4: 运行最终验证**

Run:

```bash
bash -n scripts/deploy.sh
sh -n scripts/service-compose-smoke.sh
sh -n scripts/service-api-course-test.sh
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
docker compose --env-file docker/.env.example -f docker/compose.yml config --quiet
docker compose --env-file docker/.env.example \
  -f docker/compose.yml -f docker/compose.dev.yml config --quiet
uv run ruff check hyperextract
uv run ruff format --check hyperextract
OPENAI_API_KEY="" uv run pytest
git diff --check
```

Expected: Shell 语法、Docker Compose、Ruff 和完整 pytest 全部退出码为 0。

- [ ] **Step 5: 检查禁止内容与工作区边界**

Run:

```bash
rg -n 'EXCHANGE_VOLUME_NAME|hyper-extract-exchange|he-migrate|postgres-data|exchange-data' \
  docker scripts/service-compose-smoke.sh tests/docker
git check-ignore -v docker/data/postgres docker/data/exchange
git status --short
```

Expected: 第一条命令只允许测试中的否定断言；两个数据目录被 `docker/data/.gitignore` 忽略；原有服务层修改仍保留。

- [ ] **Step 6: 提交文档与最终断言**

```bash
git add docker/README.md tests/docker/test_service_docker_files.py
git commit -m "docs(docker): document host data deployment"
```
