# Docker 部署目录迁移设计

## 背景与目标

当前 Docker 部署文件全部位于项目根目录下的 `docker/`，但文件名没有形成清晰的职责分组，部署 README 也以英文为主。本次调整采用直接迁移，不保留旧路径或符号链接，并统一为中文运维文档。

目标目录如下：

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

## 文件迁移

| 现有路径 | 目标路径 |
| --- | --- |
| `docker/service.compose.yml` | `docker/compose.yml` |
| `docker/service.compose.dev.yml` | `docker/compose.dev.yml` |
| `docker/service.Dockerfile` | `docker/image/Dockerfile` |
| `docker/entrypoint.sh` | `docker/image/entrypoint.sh` |
| `docker/model-profiles.example.toml` | `docker/conf/model-profiles.example.toml` |

`.env.example` 和 `README.md` 保持在 `docker/` 根目录，因为它们分别是部署入口配置模板和运维入口文档。

## Compose 与构建路径

基础编排文件改为 `docker/compose.yml`。镜像继续以项目根目录作为构建上下文，以保证 `pyproject.toml`、`uv.lock` 和 `hyperextract/` 可被复制；Dockerfile 路径改为 `docker/image/Dockerfile`。

Dockerfile 中的入口脚本复制路径相应改为 `docker/image/entrypoint.sh`。Model Profile 默认挂载源改为 `./conf/model-profiles.example.toml`，容器内路径仍为 `/run/config/model-profiles.toml`，因此应用内部配置不变。

## 开发与生产配置

`docker/compose.yml` 是生产基础配置，不向宿主机发布 API 端口。`docker/compose.dev.yml` 是本地开发覆盖文件，只负责将 API 绑定到 `127.0.0.1:${HE_API_PORT:-8000}`，并为本地访问补充非 internal 网络。

生产启动只加载基础文件：

```bash
docker compose --env-file docker/.env -f docker/compose.yml up -d
```

本地开发同时加载覆盖文件：

```bash
docker compose --env-file docker/.env \
  -f docker/compose.yml \
  -f docker/compose.dev.yml up -d
```

## 密钥与 Model Profile 数据流

`--env-file docker/.env` 为 Compose 的 `${...}` 插值提供变量。Worker 服务额外通过 `env_file: .env` 接收模型密钥；API 服务不加载该文件，因此不持有模型密钥。

`docker/conf/model-profiles.example.toml` 不保存密钥值，只保存模型路由、能力约束和密钥环境变量名。例如：

```toml
llm_api_key_env = "MINIMAX_API_KEY"
embedder_api_key_env = "EMBEDDING_API_KEY"
```

Worker 解析 Profile 后，按照这些名字从容器环境读取真实值。示例和文档统一使用正确拼写 `MINIMAX_API_KEY`；已废弃的 `MIMIMAX_API_KEY` 不再作为推荐配置。

本地忽略提交的 `docker/.env` 若仍使用旧拼写，将只重命名变量名并保留其值；该文件不会被提交或输出到日志。

## README 中文化

`docker/README.md` 全文改写为中文，保留现有运维信息和安全约束，并补充：

- 生产与开发启动命令；
- `.env` 的 Compose 插值和容器注入差异；
- API 与 Worker 的密钥隔离；
- `model-profiles.example.toml` 的用途、字段说明和自定义示例；
- `MINIMAX_API_KEY` 的正确拼写；
- Profile 文件复制、修改及通过 `MODEL_PROFILES_FILE` 挂载的方法；
- 数据卷权限、迁移、健康检查、停止和备份注意事项。

## 引用更新范围

迁移后更新仓库内所有旧路径引用，包括：

- Docker Compose 和 Dockerfile 内部路径；
- `scripts/` 下的部署、冒烟及验收脚本；
- `tests/docker/` 中的结构断言；
- 根 README、英文文档和中文文档中的部署命令；
- `docs/superpowers/` 中正在使用的实施计划引用；历史性说明只在会误导当前操作时更新。

迁移完成后使用全文检索确认不存在仍会被执行或复制的旧路径。

## 错误处理与兼容策略

本次是直接迁移，不保留旧文件、符号链接或重复 Compose 配置。使用旧命令时应明确失败，避免维护两套可能漂移的部署定义。

若 `MODEL_PROFILES_FILE` 指向不存在的文件，Compose 挂载或应用就绪检查应失败；README 将要求从示例复制真实配置，而不是把密钥写入 TOML。

## 验证

实施后执行以下验证：

1. 运行 `OPENAI_API_KEY="" pytest tests/docker/test_service_docker_files.py`。
2. 使用无真实密钥的临时环境执行 `docker compose ... config`，验证基础和开发覆盖文件均可解析。
3. 运行 `ruff check hyperextract` 与 `ruff format --check hyperextract`，确认迁移没有影响 Python 包。
4. 使用 `rg` 检查旧文件名、旧挂载路径和文档命令是否存在活动引用。
5. 检查 Git 状态，确保本地 `docker/.env` 未被纳入提交范围，且原有未提交修改均被保留。
