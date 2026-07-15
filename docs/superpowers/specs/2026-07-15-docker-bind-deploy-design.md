# Docker Bind Mount 与自动部署设计

## 背景与目标

当前 Compose 使用 Docker named volume 保存 PostgreSQL 和 `/exchange` 数据，并通过一次性的 `he-migrate` Compose 服务执行 Alembic。线上尚无需要保留的历史 volume 数据，因此本次直接切换为宿主机 bind mount，不提供旧卷迁移或兼容逻辑。

目标是让运维人员只执行一个命令：

```bash
./scripts/deploy.sh
```

脚本自动完成代码更新、目录准备、镜像构建、数据库迁移、服务启动和健康检查。正式部署不再依赖手工执行 Compose 子命令。

## 目标目录

```text
docker/
├── data/
│   └── .gitignore
├── compose.yml
├── compose.dev.yml
├── .env.example
├── conf/
│   └── model-profiles.example.toml
├── image/
│   ├── Dockerfile
│   └── entrypoint.sh
└── README.md
scripts/
└── deploy.sh
```

`docker/data/.gitignore` 只保留数据根目录，不跟踪 PostgreSQL、上传包、运行状态或产物。部署脚本负责创建 `postgres/` 与 `exchange/` 子目录。

## 持久化布局

Compose 使用可覆盖的数据根目录，默认值固定为 `docker/data`：

```yaml
services:
  postgres:
    environment:
      PGDATA: /var/lib/postgresql/data/pgdata
    volumes:
      - ${HE_DATA_ROOT:-./data}/postgres:/var/lib/postgresql/data

  he-api:
    volumes:
      - ${HE_DATA_ROOT:-./data}/exchange:/exchange

  he-worker:
    volumes:
      - ${HE_DATA_ROOT:-./data}/exchange:/exchange
```

相对路径按 `docker/compose.yml` 所在目录解析，因此默认落盘位置为：

```text
docker/data/postgres/
docker/data/exchange/
```

`PGDATA` 使用挂载目录下的 `pgdata/` 子目录，避免父目录中的管理文件影响 PostgreSQL `initdb` 空目录检查。PostgreSQL 官方镜像负责初始化自己的目录权限。

服务镜像仍以 UID/GID `10001:10001` 运行。部署脚本在启动 API 和 Worker 前，使用新构建服务镜像创建一次性 root 容器，将 `exchange/` 设置为 `10001:10001` 和组可写权限。Compose 不新增常驻 storage-init 服务。

## Compose 简化

删除以下内容：

- `he-migrate` 服务；
- API 和 Worker 对 `he-migrate` 的 `depends_on`；
- `exchange-data` 与 `postgres-data` 顶层 named volume；
- `.env.example` 和文档中的 `EXCHANGE_VOLUME_NAME`。

API 与 Worker 只保留对健康 PostgreSQL 的依赖。API 就绪检查继续验证数据库 revision 是否为 Alembic head，因此漏执行迁移时服务不会报告 ready。

## 部署脚本

新增 `scripts/deploy.sh`，使用 Bash 严格模式：

```bash
set -Eeuo pipefail
```

脚本只支持完整部署，不提供模块化构建、远程镜像推送或旧 volume 导入。执行阶段如下。

### 1. 前置检查

- 必须存在 `git`、`docker` 和 Docker Compose v2；
- 必须存在 `docker/.env`；
- 当前 Git 分支必须有 upstream；
- 工作区必须干净，包括未跟踪文件；被忽略的 `.env` 和 `docker/data` 不影响检查；
- Docker daemon 必须可访问。

脚本使用当前生产 checkout 的 upstream，不硬编码 `main`，并执行：

```bash
git pull --ff-only
```

若拉取后 HEAD 发生变化，脚本通过环境标记重新 `exec` 新版本的 `scripts/deploy.sh`，确保更新后的 Compose 与更新后的部署逻辑配套执行，且只允许重启一次，避免循环。

### 2. 准备目录和配置

脚本创建：

```text
docker/data/postgres/
docker/data/exchange/
```

然后使用 `docker compose config --quiet` 验证 `.env`、Compose 插值、bind mount 和 Profile 文件路径。配置失败时不改动运行中的 API 或 Worker。

### 3. 构建新镜像

脚本先构建 `he-api` 使用的服务镜像；Worker 使用相同 `HE_IMAGE`，因此不重复构建。构建失败时旧容器继续运行。

构建成功后，脚本使用同一镜像执行一次性 root 命令，为 `/exchange` 创建 `uploads`、`packages`、`runs` 和 `probes`，并设置 UID/GID `10001:10001` 与 `0775` 目录权限。命令执行完成后临时容器自动删除。

### 4. 启动数据库

脚本执行：

```bash
docker compose up -d postgres
```

并在有界循环中调用 `pg_isready`。超时后打印 PostgreSQL 日志并失败退出。

### 5. 进入短暂维护窗口

数据库健康且镜像准备完成后：

1. 先停止 API，阻止新任务进入；
2. 再以 90 秒宽限停止 Worker，让进行中的模型调用退出；
3. 使用新镜像执行 Alembic；
4. 迁移成功后启动新 API 和 Worker。

迁移命令复用 API 服务定义：

```bash
docker compose run --rm --no-deps he-api alembic upgrade head
```

该命令创建临时容器，完成后删除，不形成常驻迁移服务。首次部署和重复部署使用同一流程；数据库已在 head 时 Alembic 自动为空操作。

迁移失败时脚本不会启动新 API 或 Worker，也不会自动执行不可靠的 Schema 回滚；脚本打印迁移与 PostgreSQL 诊断信息并返回非零状态。

### 6. 启动与验收

迁移成功后执行：

```bash
docker compose up -d --remove-orphans he-api he-worker
```

`--remove-orphans` 清理旧部署可能残留的 `he-migrate` 容器。脚本从 API 容器内部轮询 `http://127.0.0.1:8000/health/ready`，因此生产 Compose 无需发布宿主机端口。

就绪超时后，脚本输出 `postgres`、`he-api` 和 `he-worker` 的状态及尾部日志并失败退出。成功时输出当前 Git revision、镜像名、数据目录和服务状态。

## 失败边界

| 失败阶段 | 运行状态 |
| --- | --- |
| Git 拉取、配置校验、目录准备或镜像构建失败 | 旧 API/Worker 继续运行 |
| PostgreSQL 启动失败 | API/Worker 尚未进入维护窗口 |
| 停止服务后 Alembic 失败 | API/Worker 保持停止，防止新代码运行在未知 Schema 上 |
| 新 API/Worker 启动或就绪失败 | 容器和日志保留，脚本返回非零状态 |

脚本不自动执行 `git reset`、数据库降级或数据目录删除。

## 本地开发与直接 Compose 操作

正式部署唯一推荐入口是 `scripts/deploy.sh`。本地开发仍可加载：

```bash
docker compose --env-file docker/.env \
  -f docker/compose.yml \
  -f docker/compose.dev.yml up -d
```

但直接 `up` 不再自动迁移。README 必须明确：诊断或本地手工操作时，应先执行一次性的 Alembic 命令。API readiness 会阻止未迁移实例被认为可用。

## 冒烟与验收脚本隔离

`scripts/service-compose-smoke.sh` 不调用 `deploy.sh`，避免测试过程中执行 Git 拉取。它使用 `mktemp -d` 创建绝对路径，并通过 `HE_DATA_ROOT` 覆盖默认 `docker/data`：

```text
<临时目录>/postgres/
<临时目录>/exchange/
```

冒烟流程显式启动 PostgreSQL、等待健康、运行一次性 Alembic、启动 API/Worker并验证重启持久性。退出 trap 只删除本次 `mktemp` 创建且通过前缀校验的目录，不触碰正式 `docker/data`。

本地 API 验收脚本同步改为显式运行一次性 Alembic，避免依赖已删除的 `he-migrate`。

## 文档与测试

更新中文 Docker README：

- 将 `scripts/deploy.sh` 作为正式部署唯一入口；
- 说明 `docker/data/postgres` 与 `docker/data/exchange` 的内容和备份方式；
- 删除 named volume、`EXCHANGE_VOLUME_NAME` 和 `down --volumes` 会删除数据的旧描述；
- 明确 bind mount 数据不会被 `docker compose down --volumes` 删除，但删除宿主机目录会永久丢失数据；
- 说明 PostgreSQL 在线备份仍使用 `pg_dump`，不能在运行时直接复制数据目录；
- 说明权限、维护窗口、迁移失败和手工 Compose 操作方式。

静态测试覆盖：

- Compose 使用 bind mount 且不声明 named volume；
- `PGDATA` 子目录正确；
- 不存在 `he-migrate` 服务或相关依赖；
- API 与 Worker 使用相同 exchange 路径；
- `deploy.sh` 使用 `git pull --ff-only`、先构建后停止、一次性 Alembic、有界健康等待和失败日志；
- 冒烟测试使用临时 `HE_DATA_ROOT`，不引用正式数据目录；
- README 使用新目录与部署入口。

验证命令包括：

```bash
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py
sh -n scripts/deploy.sh
sh -n scripts/service-compose-smoke.sh
docker compose --env-file docker/.env.example -f docker/compose.yml config --quiet
docker compose --env-file docker/.env.example -f docker/compose.yml -f docker/compose.dev.yml config --quiet
uv run ruff check hyperextract
uv run ruff format --check hyperextract
```

最终执行完整的确定性测试套件：

```bash
OPENAI_API_KEY="" uv run pytest
```
