# Docker 部署目录迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Docker 部署文件直接迁移到职责清晰的 `compose`、`conf`、`image` 结构，并交付完整中文运维 README。

**Architecture:** 生产编排保留在 `docker/compose.yml`，开发端口覆盖放在 `docker/compose.dev.yml`；镜像构建文件集中到 `docker/image/`，模型路由模板集中到 `docker/conf/`。Compose 继续只向 Worker 注入密钥，TOML 只引用环境变量名，应用容器内路径保持不变。

**Tech Stack:** Docker Compose、Dockerfile、POSIX shell、TOML、pytest、PyYAML、Ruff。

## Global Constraints

- 直接迁移，不保留旧文件、重复配置或符号链接。
- Python 版本保持 `>=3.11`，包管理继续使用 `uv`。
- 不读取、不输出、不提交 `docker/.env` 中的密钥值。
- `docker/.env` 和 `.env.example` 统一使用 `MINIMAX_API_KEY`；`MIMIMAX_API_KEY` 只保留在兼容性实现和专门测试中。
- API 不加载模型密钥；只有 Worker 使用 `env_file: .env`。
- 容器内 Model Profile 路径保持 `/run/config/model-profiles.toml`。
- 生产 Compose 不发布 API 宿主机端口；开发覆盖只绑定 `127.0.0.1`。
- 保留工作区已有未提交修改，不提交与本次迁移无关的文件。
- 历史规格、历史计划和历史评审中的旧路径保留原貌；活动脚本、测试和运维文档不得引用旧路径。

## 文件职责映射

- `docker/compose.yml`：生产基础拓扑、网络、数据卷、服务配置。
- `docker/compose.dev.yml`：本地开发 API 端口和 `local-access` 网络覆盖。
- `docker/image/Dockerfile`：锁文件构建、非 root 运行时镜像。
- `docker/image/entrypoint.sh`：共享卷可读性检查、`umask` 和进程转交。
- `docker/conf/model-profiles.example.toml`：不含密钥值的模型能力和路由模板。
- `docker/.env.example`：Compose 插值变量和 Worker 密钥变量模板。
- `docker/README.md`：中文部署、配置、安全和运维手册。
- `tests/docker/conftest.py`：新 Compose 路径的 YAML fixture。
- `tests/docker/test_service_docker_files.py`：新布局、密钥隔离、挂载和活动脚本的静态回归测试。
- `scripts/service-compose-smoke.sh`：使用新 Compose 路径执行隔离冒烟测试。
- `scripts/service-api-course-test.sh`：使用新 Compose 路径执行本地 API 验收。

---

### Task 1: 用静态测试锁定新目录并迁移部署文件

**Files:**
- Move: `docker/service.compose.yml` → `docker/compose.yml`
- Move: `docker/service.compose.dev.yml` → `docker/compose.dev.yml`
- Move: `docker/service.Dockerfile` → `docker/image/Dockerfile`
- Move: `docker/entrypoint.sh` → `docker/image/entrypoint.sh`
- Move: `docker/model-profiles.example.toml` → `docker/conf/model-profiles.example.toml`
- Modify: `docker/compose.yml`
- Modify: `docker/image/Dockerfile`
- Modify: `docker/.env.example`
- Modify: `tests/docker/conftest.py`
- Modify: `tests/docker/test_service_docker_files.py`

**Interfaces:**
- Consumes: 当前 Compose 服务名 `postgres`、`he-migrate`、`he-api`、`he-worker` 和容器内配置路径 `/run/config/model-profiles.toml`。
- Produces: 新活动路径 `docker/compose.yml`、`docker/compose.dev.yml`、`docker/image/Dockerfile`、`docker/image/entrypoint.sh`、`docker/conf/model-profiles.example.toml`。

- [ ] **Step 1: 先修改测试路径和挂载断言**

将 `tests/docker/conftest.py` 的路径定义改为：

```python
ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker" / "compose.yml"
DEV_COMPOSE_PATH = ROOT / "docker" / "compose.dev.yml"
```

将 fixture docstring 分别改为 `docker/compose.yml` 和 `docker/compose.dev.yml`。将 `tests/docker/test_service_docker_files.py` 的文件读取路径和挂载断言改为：

```python
text = (ROOT / "docker/image/Dockerfile").read_text()
text = (ROOT / "docker/image/entrypoint.sh").read_text()
expected_mount = "${MODEL_PROFILES_FILE:-./conf/model-profiles.example.toml}:/run/config/model-profiles.toml:ro"
```

新增布局测试：

```python
def test_docker_layout_uses_compose_conf_and_image_directories():
    assert (ROOT / "docker/compose.yml").is_file()
    assert (ROOT / "docker/compose.dev.yml").is_file()
    assert (ROOT / "docker/conf/model-profiles.example.toml").is_file()
    assert (ROOT / "docker/image/Dockerfile").is_file()
    assert (ROOT / "docker/image/entrypoint.sh").is_file()
    for old_path in (
        "docker/service.compose.yml",
        "docker/service.compose.dev.yml",
        "docker/service.Dockerfile",
        "docker/entrypoint.sh",
        "docker/model-profiles.example.toml",
    ):
        assert not (ROOT / old_path).exists()
```

- [ ] **Step 2: 运行测试并确认它因新路径尚不存在而失败**

Run:

```bash
OPENAI_API_KEY="" pytest tests/docker/test_service_docker_files.py -q
```

Expected: FAIL，错误包含 `docker/compose.yml`、`docker/image/Dockerfile` 或其他新路径不存在。

- [ ] **Step 3: 创建职责目录并移动文件**

Run:

```bash
mkdir -p docker/conf docker/image
mv docker/service.compose.yml docker/compose.yml
mv docker/service.compose.dev.yml docker/compose.dev.yml
mv docker/service.Dockerfile docker/image/Dockerfile
mv docker/entrypoint.sh docker/image/entrypoint.sh
mv docker/model-profiles.example.toml docker/conf/model-profiles.example.toml
```

Expected: `find docker -maxdepth 2 -type f` 显示目标结构，旧路径不存在。

- [ ] **Step 4: 更新 Compose、Dockerfile 和环境模板内部路径**

在 `docker/compose.yml` 中将三个 build 定义统一为：

```yaml
build:
  context: ..
  dockerfile: docker/image/Dockerfile
```

将 API 和 Worker 的 Profile 挂载统一为：

```yaml
- ${MODEL_PROFILES_FILE:-./conf/model-profiles.example.toml}:/run/config/model-profiles.toml:ro
```

在 `docker/image/Dockerfile` 中使用新入口脚本路径：

```dockerfile
COPY docker/image/entrypoint.sh /usr/local/bin/he-entrypoint
```

在 `docker/.env.example` 中改为：

```dotenv
# Optional: path (relative to docker/) to the Model Profile TOML mounted into
# both the API and Worker containers at /run/config/model-profiles.toml.
MODEL_PROFILES_FILE=./conf/model-profiles.example.toml
```

同时将 `HE_IMAGE` 注释中的 Dockerfile 路径改为 `docker/image/Dockerfile`。

- [ ] **Step 5: 运行 Docker 静态测试**

Run:

```bash
OPENAI_API_KEY="" pytest tests/docker/test_service_docker_files.py -q
```

Expected: PASS。

- [ ] **Step 6: 检查迁移差异，不提交工作区原有修改**

Run:

```bash
git status --short docker tests/docker
git diff --check -- docker tests/docker
```

Expected: 旧文件显示删除、目标文件显示新增或重命名，且没有空白错误。由于被迁移文件带有用户原有未提交修改，本任务不创建实现提交。

---

### Task 2: 更新活动脚本并安全修正本地变量名

**Files:**
- Modify: `scripts/service-compose-smoke.sh`
- Modify: `scripts/service-api-course-test.sh`
- Modify: `tests/docker/test_service_docker_files.py`
- Modify without staging: `docker/.env`

**Interfaces:**
- Consumes: Task 1 产生的 `docker/compose.yml`、`docker/compose.dev.yml` 和默认 Profile 路径 `./conf/model-profiles.example.toml`。
- Produces: 所有活动脚本只调用新路径；本地 Worker 环境包含与 TOML 一致的 `MINIMAX_API_KEY` 名称。

- [ ] **Step 1: 为活动脚本的新路径增加静态断言**

将现有 smoke 测试补充为：

```python
def test_smoke_script_is_isolated_and_cleans_up():
    text = (ROOT / "scripts" / "service-compose-smoke.sh").read_text()
    assert "set -eu" in text
    assert "OPENAI_API_KEY=" in text
    assert "--project-name" in text
    assert "down --volumes --remove-orphans" in text
    assert "trap " in text
    assert "before_worker_id" in text
    assert "after_worker_id" in text
    assert 'after_worker_id" != "$before_worker_id' in text
    assert "docker/compose.yml" in text
    assert "docker/compose.dev.yml" in text
    assert "./conf/model-profiles.example.toml" in text
    assert "service.compose" not in text
```

新增 API 验收脚本断言：

```python
def test_api_acceptance_script_uses_new_compose_paths():
    text = (ROOT / "scripts" / "service-api-course-test.sh").read_text()
    assert 'docker/compose.yml"' in text
    assert 'docker/compose.dev.yml"' in text
    assert "service.compose" not in text
```

- [ ] **Step 2: 运行新增断言并确认旧路径导致失败**

Run:

```bash
OPENAI_API_KEY="" pytest \
  tests/docker/test_service_docker_files.py::test_smoke_script_is_isolated_and_cleans_up \
  tests/docker/test_service_docker_files.py::test_api_acceptance_script_uses_new_compose_paths -q
```

Expected: FAIL，断言显示脚本仍包含 `service.compose` 或缺少新路径。

- [ ] **Step 3: 更新两个活动脚本**

在 `scripts/service-compose-smoke.sh` 中使用：

```sh
export MODEL_PROFILES_FILE="./conf/model-profiles.example.toml"
COMPOSE_FILES="-f docker/compose.yml -f docker/compose.dev.yml"
```

在 `scripts/service-api-course-test.sh` 的 `compose()` 中使用：

```sh
compose() {
    docker compose --env-file "$ENV_FILE" \
        -f "$REPO_ROOT/docker/compose.yml" \
        -f "$REPO_ROOT/docker/compose.dev.yml" "$@"
}
```

- [ ] **Step 4: 安全重命名被忽略的本地密钥变量**

先只检查变量名，不显示等号后的值：

```bash
awk -F= '/^[A-Za-z_][A-Za-z0-9_]*=/{print $1}' docker/.env | sort
```

若输出包含 `MIMIMAX_API_KEY`，执行机械式前缀替换：

```bash
perl -pi -e 's/^MIMIMAX_API_KEY=/MINIMAX_API_KEY=/' docker/.env
```

再次执行相同的 `awk` 命令。Expected: 出现 `MINIMAX_API_KEY`，不再出现 `MIMIMAX_API_KEY`；命令全程不输出密钥值。

- [ ] **Step 5: 验证脚本语法和静态断言**

Run:

```bash
sh -n scripts/service-compose-smoke.sh
sh -n scripts/service-api-course-test.sh
OPENAI_API_KEY="" pytest \
  tests/docker/test_service_docker_files.py::test_smoke_script_is_isolated_and_cleans_up \
  tests/docker/test_service_docker_files.py::test_api_acceptance_script_uses_new_compose_paths -q
```

Expected: 两个 `sh -n` 命令退出码为 0，两个 pytest 用例 PASS。

- [ ] **Step 6: 检查脚本差异和本地文件忽略状态**

Run:

```bash
git diff --check -- scripts tests/docker
git check-ignore -v docker/.env
git status --short scripts tests/docker docker/.env
```

Expected: `docker/.env` 由 `.gitignore` 忽略且不出现在 Git 状态中；不创建包含原有脚本修改的实现提交。

---

### Task 3: 将 Docker README 改写为中文并完成仓库级验证

**Files:**
- Modify: `docker/README.md`
- Modify: `tests/docker/test_service_docker_files.py`
- Verify only: `README.md`
- Verify only: `README_ZH.md`
- Verify only: `docs/en/`
- Verify only: `docs/zh/`

**Interfaces:**
- Consumes: Task 1 的新目录结构和 Task 2 的新启动命令。
- Produces: 中文运维入口文档，以及对目录、配置数据流和安全边界的可执行说明。

- [ ] **Step 1: 先把 README 静态断言改成中文要求**

将 README 测试改为：

```python
def test_docker_readme_is_chinese_and_documents_configuration_boundaries():
    text = (ROOT / "docker" / "README.md").read_text()
    for required in (
        "# Docker 部署",
        "docker/compose.yml",
        "docker/compose.dev.yml",
        "MINIMAX_API_KEY",
        "model-profiles.example.toml",
        "只有 Worker",
        "/exchange",
        "10001",
        "不要使用 Compose `--scale`",
    ):
        assert required in text
    assert "service.compose" not in text
    assert "service.Dockerfile" not in text
    assert "MIMIMAX_API_KEY" not in text
```

- [ ] **Step 2: 运行 README 测试并确认英文文档不满足要求**

Run:

```bash
OPENAI_API_KEY="" pytest tests/docker/test_service_docker_files.py::test_docker_readme_is_chinese_and_documents_configuration_boundaries -q
```

Expected: FAIL，缺少 `# Docker 部署` 或仍包含旧路径。

- [ ] **Step 3: 按固定章节完整改写中文 README**

`docker/README.md` 使用以下章节顺序：

```markdown
# Docker 部署

## 目录结构
## 服务拓扑与网络隔离
## 配置文件与密钥传递
### `.env` 的两个作用
### Model Profile 的作用
### 定义自有 Model Profile
## 共享 `/exchange` 数据卷
### 原子发布
### 所有权与权限
## 启动生产环境
## 启动本地开发环境
## 模型能力探测
## 启动顺序与数据库迁移
## 健康检查、停止与重启
## Worker 数量与模型配额
## 备份与破坏性操作
## 隔离冒烟测试
## 真实模型验收
```

配置章节必须明确：

```markdown
`--env-file docker/.env` 为 Compose 的 `${...}` 插值提供变量；
`he-worker.env_file: .env` 才把模型密钥注入 Worker 容器。
API 不加载该文件，只有 Worker 持有模型密钥。
```

自定义 Profile 示例必须包含：

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
```

并说明将示例复制成自有文件：

```bash
cp docker/conf/model-profiles.example.toml docker/conf/model-profiles.toml
```

对应 `.env` 配置为：

```dotenv
MODEL_PROFILES_FILE=./conf/model-profiles.toml
MY_LLM_API_KEY=...
MY_EMBEDDING_API_KEY=...
```

保留现有上传限制、并发、探测、共享卷权限、迁移门禁、健康检查、恢复、备份和真实模型验收信息，但全部使用中文表达和新路径。

- [ ] **Step 4: 运行 Docker 静态测试和 Shell 语法检查**

Run:

```bash
OPENAI_API_KEY="" pytest tests/docker/test_service_docker_files.py -q
sh -n docker/image/entrypoint.sh
sh -n scripts/service-compose-smoke.sh
sh -n scripts/service-api-course-test.sh
```

Expected: pytest 全部 PASS，三个 Shell 语法检查退出码为 0。

- [ ] **Step 5: 验证基础和开发 Compose 均可解析**

Run:

```bash
docker compose --env-file docker/.env.example \
  -f docker/compose.yml config --quiet
docker compose --env-file docker/.env.example \
  -f docker/compose.yml \
  -f docker/compose.dev.yml config --quiet
```

Expected: 两个命令退出码均为 0，不启动容器，不调用模型服务。

- [ ] **Step 6: 运行项目要求的 Python 静态检查**

Run:

```bash
ruff check hyperextract
ruff format --check hyperextract
```

Expected: 两个命令均退出码为 0。

- [ ] **Step 7: 检查活动引用、空白和用户修改保留情况**

Run:

```bash
rg -n 'docker/service\.compose|docker/service\.Dockerfile|docker/entrypoint\.sh|\./model-profiles\.example\.toml' \
  docker scripts tests README.md README_ZH.md docs/en docs/zh
rg -n 'MIMIMAX_API_KEY' docker scripts README.md README_ZH.md docs/en docs/zh
git diff --check
git status --short
```

Expected: 两次 `rg` 都没有活动引用；`git diff --check` 没有错误；Git 状态显示原有未提交服务改动仍存在，`docker/.env` 不在状态列表中。

- [ ] **Step 8: 给出完成摘要但不提交混合修改**

记录新目录、中文 README、`MINIMAX_API_KEY` 修正和所有验证命令的实际结果。由于 Docker 与脚本文件在执行前已有未提交修改，不创建混合实现提交；由用户在确认整体差异后决定提交范围。
