# Docker 部署

本目录提供 Hyper-Extract 内部服务的 Docker Compose 部署配置，包括 PostgreSQL、一次性数据库迁移、无状态 API 和 Worker。服务所实现的 HTTP 与文件契约见[内部服务指南](../docs/zh/guides/internal-service.md)。

## 目录结构

```text
docker/
├── README.md
├── compose.yml
├── compose.dev.yml
├── .env.example
├── conf/
│   └── model-profiles.example.toml
└── image/
    ├── Dockerfile
    └── entrypoint.sh
```

- `compose.yml`：生产基础编排，不向宿主机发布 API 端口。
- `compose.dev.yml`：本地开发覆盖，仅把 API 绑定到 `127.0.0.1`。
- `.env.example`：部署变量与密钥变量模板，实际值写入被 Git 忽略的 `.env`。
- `conf/model-profiles.example.toml`：模型路由、能力、并发和恢复策略示例，不保存密钥值。
- `image/`：服务镜像与容器入口脚本。

## 服务拓扑与网络隔离

```text
                 service-api（internal、固定名称）
   调用方 ───────────────────────────────────── he-api
                                                     │
                            database（internal）     │
   postgres ◄──────────────────── he-api、he-worker、he-migrate
                                                     │
                            model-egress（外网出口） │
   模型服务端点 ◄──────────────────────────────── he-worker
```

- `database`：仅内部访问。PostgreSQL、迁移、API 和 Worker 使用该网络。
- `service-api`：仅内部访问，但通过 `${API_NETWORK_NAME:-hyper-extract-api}` 获得稳定名称。其他 Compose 项目可将该网络声明为 external 并连接 `he-api`，但无法借此访问 PostgreSQL。
- `model-egress`：允许访问模型服务端点，只有 Worker 连接该网络。

API 不持有模型密钥，也不连接模型出口；只有 Worker 持有模型密钥并调用模型服务。

## 配置文件与密钥传递

先复制部署变量模板：

```bash
cp docker/.env.example docker/.env
```

至少设置数据库密码，并按所用 Profile 设置模型密钥：

```dotenv
POSTGRES_PASSWORD=请替换为强密码
MINIMAX_API_KEY=请填写真实密钥
EMBEDDING_API_KEY=请填写真实密钥
```

不要提交 `docker/.env`。该文件已被 `.gitignore` 和 `.dockerignore` 排除。

### `.env` 的两个作用

同一个文件在当前部署中有两个不同用途：

1. 命令行参数 `--env-file docker/.env` 为 Compose 文件中的 `${POSTGRES_PASSWORD}`、`${HE_IMAGE}`、`${MODEL_PROFILES_FILE}` 等插值提供值。
2. `he-worker` 的 `env_file: .env` 将模型密钥注入 Worker 容器。

第二个路径相对于 `docker/compose.yml` 所在目录解析，因此它读取的是 `docker/.env`。API 没有 `env_file` 配置，不会获得 `MINIMAX_API_KEY`、`EMBEDDING_API_KEY` 等密钥。

Compose 只将非敏感的默认模型名称和 Base URL 显式传给 API，使 API 与 Worker 能计算一致且不含密钥的 Profile 指纹。

### Model Profile 的作用

`docker/conf/model-profiles.example.toml` 定义一组具名模型运行配置。它不保存密钥，只保存密钥对应的环境变量名：

```toml
[profiles.minimax-m27]
llm_api_key_env = "MINIMAX_API_KEY"
embedder_api_key_env = "EMBEDDING_API_KEY"
```

API 读取 Profile 的公开部分，用于验证 Profile 名称、能力声明和指纹；Worker 在真正执行任务时，再根据 `llm_api_key_env` 与 `embedder_api_key_env` 从容器环境读取密钥。

示例文件中的主要字段如下：

| 字段 | 作用 |
| --- | --- |
| `transport` | 模型接口协议，例如 `openai_chat` |
| `llm` | `provider:model@base_url` 格式的大模型路由 |
| `llm_api_key_env` | 大模型密钥所在的环境变量名 |
| `embedder` | Embedding 模型路由 |
| `embedder_api_key_env` | Embedding 密钥所在的环境变量名 |
| `*_rate_limit_group` | 共享账号的限流分组 |
| `probe_required` | 是否必须先有模型能力探测证据 |
| `request_timeout` | 单次模型请求超时秒数 |
| `capabilities` | 结构化输出、上下文和参数映射能力 |
| `embedding_capabilities` | 批量大小、输入上限和异常条目策略 |
| `recovery` | 校验修复、重试和隔离策略 |

运行请求通过 `options.execution.model_profile` 选择 Profile，例如示例中的 `minimax-m27`。

### 定义自有 Model Profile

不要直接在示例文件中长期维护生产配置。先复制一份：

```bash
cp docker/conf/model-profiles.example.toml docker/conf/model-profiles.toml
```

然后在 `docker/conf/model-profiles.toml` 中定义自己的 Profile：

```toml
[profiles.my-model]
transport = "openai_chat"
llm = "openai:模型名称@https://模型接口/v1"
llm_api_key_env = "MY_LLM_API_KEY"
embedder = "openai:Embedding模型@https://Embedding接口/v1"
embedder_api_key_env = "MY_EMBEDDING_API_KEY"
llm_rate_limit_group = "my-llm-account"
embedder_rate_limit_group = "my-embedding-account"
probe_required = false
request_timeout = 900

[profiles.my-model.capabilities]
structured_output_modes = ["text_json"]
preferred_structured_output_mode = "text_json"
structured_output_fallback_order = ["text_json"]
output_token_parameter = "max_tokens"
supported_parameters = ["max_output_tokens", "timeout_seconds"]
context_tokens = 65536
max_output_tokens = 8192
recommended_concurrency = 2

[profiles.my-model.embedding_capabilities]
transport = "openai_embeddings"
accepts_token_ids = false
max_batch_items = 10
max_input_tokens_per_item = 8191
supports_dimensions = false
empty_input_policy = "quarantine"
item_failure_policy = "quarantine"
recommended_concurrency = 2

[profiles.my-model.recovery]
validation_repair_attempts = 1
validation_retry_attempts = 3
transient_retry_attempts = 4
invalid_list_item_policy = "quarantine"
invalid_item_ratio_threshold = 0.2
```

在 `docker/.env` 中指定文件路径并提供对应密钥：

```dotenv
MODEL_PROFILES_FILE=./conf/model-profiles.toml
MY_LLM_API_KEY=请填写真实密钥
MY_EMBEDDING_API_KEY=请填写真实密钥
```

`MODEL_PROFILES_FILE` 相对于 `docker/` 目录解析。修改 Profile 后重启 API 与 Worker，使两者重新读取相同配置。

MiniMax 的正确变量名是 `MINIMAX_API_KEY`。旧拼写已经废弃，不应继续写入新的部署配置。

### HTTP 上传与进度配置

外部调用方通过 `POST /v1/runs` 上传 `.hepkg.tar.gz`，不挂载 `/exchange`。相关部署参数如下：

| 变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `HE_SERVICE_MAX_UPLOAD_BYTES` | `500000000` | 上传压缩包大小上限 |
| `HE_SERVICE_MAX_EXPANDED_BYTES` | `2147483648` | 解压后总大小上限 |
| `HE_SERVICE_MAX_ARCHIVE_MEMBERS` | `20000` | 压缩包成员数量上限 |
| `HE_SERVICE_UPLOAD_READ_BLOCK` | `1048576` | 流式读取块大小 |
| `HE_SERVICE_PROGRESS_SECONDS` | `5` | 进度快照最小写入间隔 |
| `HE_SERVICE_PIPELINE_MAX_WORKERS` | `2` | 单次运行内并发处理的文档块数量 |

Profile 的 `recommended_concurrency` 会进一步限制生成与 Embedding 并发，因此提高 `HE_SERVICE_PIPELINE_MAX_WORKERS` 不能绕过模型服务商配额。

## 共享 `/exchange` 数据卷

API 与 Worker 将同一个命名卷挂载到 `/exchange`。外部调用方不挂载该卷，而是通过 HTTP 上传包和下载结果。

```yaml
volumes:
  exchange-data:
    name: ${EXCHANGE_VOLUME_NAME:-hyper-extract-exchange}
```

数据卷包含：

- `/exchange/uploads`：上传过程中的临时压缩包，发布后清理。
- `/exchange/packages`：按内容寻址发布的 Document Package。
- `/exchange/runs/<run_id>/`：每次运行的工作区、进度、诊断和最终产物。
- `/exchange/probes`：模型能力探测证据。

外部调用方不应使用 `file://` 路径，也不应直接访问 PostgreSQL 或 `/exchange`。

### 原子发布

发布方必须先写入最终目录同级的 `.staging-<run_id>/`，完成后再原子重命名为 `runs/<run_id>/`。Worker 只在最终目录同时存在且校验通过 `_SUCCESS` 与 `artifact-manifest.json` 时进行对账。

若发现部分发布或不一致状态，Worker 会以 `ARTIFACT_STATE_INCONSISTENT` 失败，不会覆盖已有内容。

### 所有权与权限

镜像使用固定 UID/GID `10001:10001`。数据卷必须允许该用户写入，不要使用 `chmod 777`。首次部署可设置所有权：

```bash
docker run --rm -v hyper-extract-exchange:/exchange alpine \
  chown -R 10001:10001 /exchange
```

入口脚本设置 `umask 0002`，新文件通常为 `0664`、目录为 `0775`，以便同一 GID 的协作进程写入。

## 启动生产环境

在项目根目录执行：

```bash
docker compose --env-file docker/.env \
  -f docker/compose.yml up -d
```

生产基础配置不会发布宿主机端口。调用方应加入 `${API_NETWORK_NAME:-hyper-extract-api}` 网络，并通过服务名 `he-api:8000` 访问 API。

`HE_IMAGE` 可覆盖默认镜像名。迁移、API 与 Worker 使用同一个由 `docker/image/Dockerfile` 构建的镜像，避免角色间版本不一致。

## 启动本地开发环境

本地开发额外加载 `compose.dev.yml`：

```bash
docker compose --env-file docker/.env \
  -f docker/compose.yml \
  -f docker/compose.dev.yml up -d
```

开发覆盖把 API 绑定到：

```text
http://127.0.0.1:${HE_API_PORT:-8000}
```

它不会把 PostgreSQL 暴露到宿主机，也不会让 API 加入模型出口网络。

项目还提供端到端本地 API 验收脚本：

```bash
./scripts/service-api-course-test.sh
```

指定其他 Document Package 或重新连接已有任务：

```bash
./scripts/service-api-course-test.sh --package /absolute/path/to/document.hepkg
./scripts/service-api-course-test.sh --run-id run_xxx
```

`Ctrl+C` 只停止本地监控，Worker 任务继续运行。可恢复失败默认最多续跑三次；使用 `--max-resumes N` 调整。脚本默认复用健康的本地栈，只有确认没有运行中的提取任务时才应传入 `--build` 重建镜像。

## 模型能力探测

示例 Profile 默认设置 `probe_required = false`，新部署可以先使用保守的声明能力。生产环境若要求实际探测证据，先执行：

```bash
docker compose --env-file docker/.env -f docker/compose.yml run --rm \
  he-worker he model probe --profile minimax-m27 \
  --file /run/config/model-profiles.toml
```

Worker 通过 `HE_PROBE_ROOT=/exchange/probes` 将证据保存到持久卷。探测成功后，把自有 Profile 的 `probe_required` 改为 `true` 并重启服务。

## 启动顺序与数据库迁移

Compose 使用以下启动门禁：

```text
postgres 健康 → he-migrate 成功退出 → he-api 与 he-worker 启动
```

`he-migrate` 只运行一次 `alembic upgrade head`，并设置 `restart: "no"`。生产 Schema 只由迁移管理，运行时代码不调用 `create_all()`。

## 健康检查、停止与重启

| 服务 | 健康检查 | 停止宽限 | 重启策略 |
| --- | --- | --- | --- |
| `postgres` | `pg_isready` | 默认 | 默认 |
| `he-migrate` | 一次性退出状态 | 默认 | `"no"` |
| `he-api` | `GET /health/ready` | 20 秒 | `unless-stopped` |
| `he-worker` | 数据库心跳与租约 | 90 秒 | `unless-stopped` |

API 就绪检查覆盖数据库、迁移版本、数据卷写入、Model Profile 解析和近期 Worker 心跳。

Worker 不配置 Docker HTTP healthcheck，因为一次合法模型请求可能持续数分钟。Worker 在独立线程中更新数据库心跳和任务租约；故障 Worker 由租约过期检测，90 秒停止宽限用于等待进行中的模型调用结束。

## Worker 数量与模型配额

在引入共享 PostgreSQL/Redis 限流协调器之前，只运行一个 Worker 进程。数据库租约能避免重复取得任务，但不能在多个进程间协调 RPM、TPM、熔断器和暂停窗口。

`HE_SERVICE_WORKER_PROCESSES` 只允许为 `1`，Worker 还会持有 `/exchange/.he-worker.lock`。不要使用 Compose `--scale` 扩展 Worker。

Worker 进程数与单次运行内并发不是同一概念：

- `HE_SERVICE_WORKER_PROCESSES=1`：该部署一次执行一个运行任务。
- `HE_SERVICE_PIPELINE_MAX_WORKERS`：该任务内部并发处理多少文档块。

Worker 崩溃后，租约过期的任务会以 `resume_from_checkpoint=true` 重新排队，恢复次数有上限。所有状态变更都会在数据库行锁下验证当前租约持有者，旧 Worker 不能覆盖接管者的结果。不要为 Worker 设置 `container_name`。

## 备份与破坏性操作

以下命令会永久删除 PostgreSQL 数据和 `/exchange` 状态：

```bash
docker compose down --volumes
```

不要对生产项目执行该命令。只有隔离冒烟脚本会在唯一项目名和唯一数据卷下使用它，并通过 `trap` 清理自身资源。

备份建议：

- PostgreSQL：对运行中的 `postgres` 服务执行 `pg_dump`，保存任务、尝试、错误和租约状态。
- `/exchange/runs`：仅在需要跨主机恢复时备份；带 `_SUCCESS` 的产物发布后不可变。
- `.he-run` 诊断快照：可能包含敏感 Prompt，应配置保留期限；恢复运行不依赖这些快照。

## 隔离冒烟测试

在项目根目录执行：

```bash
sh scripts/service-compose-smoke.sh
```

脚本不会调用真实模型。它使用唯一项目名、网络、端口和数据卷，验证：

1. API 能够就绪；
2. API 与辅助容器看到同一个 `/exchange` 数据；
3. API 和 Worker 重启后数据仍存在，并产生新的 Worker 心跳；
4. 退出时只清理本次测试创建的资源。

## 真实模型验收

真实模型验收会消耗模型额度且结果具有非确定性，只应手工执行，不应加入确定性 CI：

1. 在 `docker/.env` 中配置 Profile 引用的真实密钥，例如 `MINIMAX_API_KEY` 与 `EMBEDDING_API_KEY`。
2. 准备一个小型、不可变的 Document Package `1.1`，打包为根目录直接包含 `manifest.json`、`outline.json`、`provenance.jsonl`、`extraction-brief.yaml` 和 `content/` 的 `.hepkg.tar.gz`。
3. 计算 Package 规范指纹和传输压缩包 SHA-256。
4. 通过 `multipart/form-data` 调用 `POST /v1/runs`，提交压缩包、指纹、传输哈希、`contract_version: "1.1"` 和稳定的 `Idempotency-Key`，确认返回 `202`。
5. 轮询 `GET /v1/runs/{run_id}`，直到 `status=completed`。
6. 调用 `GET /v1/runs/{run_id}/result` 下载 `course-graph.json`，或调用 `GET /v1/runs/{run_id}/artifacts` 获取完整产物清单。
7. 检查 `run-summary.json`，确认 `extraction_brief` 与包内 Brief 一致。
