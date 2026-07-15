# PR #61 Internal API 与 Docker Service 实现审查

**审查日期：** 2026-07-14  
**审查对象：** [yifanfeng97/Hyper-Extract#61](https://github.com/yifanfeng97/Hyper-Extract/pull/61)  
**审查提交：** `c26c0cf8f1e633cc287a6e4be7c229adc6a84505`  
**目标分支：** `yifanfeng97:main`  
**原始结论：** 暂不建议合并  
**修复状态：** 本地修复与确定性门禁已完成，等待提交、GitHub CI 和发布前真实 Provider 验收

## 修复执行摘要（2026-07-14）

审核发现的本地可修复项已经在 `feature/internal-api-service` 工作树处理：

- [x] `update_progress`、`fail`、`complete`、`mark_cancelled` 和 `renew_lease` 校验存活租约及 Worker owner；
- [x] 最终文件发布在持有 owned-run 数据库行锁时执行，旧 Worker 无法跨过新 Worker claim；
- [x] 增加 SQLite 与 PostgreSQL stale-Worker fencing 测试；
- [x] completed、cancelled、failed 和 recovering Attempt 状态已持久化；
- [x] `WORKER_RECOVERY_EXHAUSTED` 已写入可查询错误历史；
- [x] uv builder 固定为 `0.9.26-python3.11-bookworm-slim`；
- [x] Python runtime 固定为 `3.11.15-slim-bookworm`；
- [x] entrypoint 实际设置 `umask 0002`，容器实测文件 `0664`、目录 `0775`；
- [x] Compose 实际使用 `HE_IMAGE`；
- [x] smoke test 要求重启后出现新的 Worker ID，并验证 Worker 仍在运行；
- [x] Ruff 已加入 dev dependency，`uv run ruff` 可在干净 uv 环境复现；
- [x] 补充英文 Course Document Pipeline 文档并同步中英文 nav；
- [ ] GitHub Tests/Lint 需要 maintainer 批准并实际运行；
- [ ] 发布前真实 Provider 验收仍需使用有效 Worker 密钥手工执行。

修复后的本地验证结果：

```text
non-integration tests: 501 passed, 6 skipped, 6 deselected
PostgreSQL concurrency/fencing: 2 passed
Ruff lint/format: passed
MkDocs strict build: passed
Compose config: passed
linux/amd64 Docker build: passed
container umask/mode check: 0002 / 0664 / 0775
Compose smoke with fresh Worker heartbeat: passed
```

## 1. 审查依据

本次审查以以下两份实施计划及仓库规范为准：

- [Internal API Service Implementation Plan](../plans/2026-07-13-internal-api-service.md)
- [Internal Service Docker Implementation Plan](../plans/2026-07-13-internal-service-docker.md)
- 仓库根目录 `AGENTS.md`
- `README.md`

审查范围包括：

- API 与 service-core 的包边界；
- Document Package 1.0/1.1 契约；
- Model Profile 密钥边界；
- Run 生命周期、租约、取消、恢复和幂等；
- Attempt 与错误历史；
- 产物发布及崩溃恢复；
- readiness；
- Docker 镜像、Compose 网络、共享卷、迁移及 smoke test；
- 本地测试、PostgreSQL 测试和实际 Docker smoke 验证。

## 2. 总体结论

PR 已经实现了计划中的大部分结构和功能，主要优点包括：

- HTTP 适配层已迁移到 `hyperextract/service/api/`；
- API 与 Worker 共用 `ServiceRuntime`；
- Document Package 1.1、ExtractionBrief 和服务布局校验已经接入；
- API 不解析模型密钥，Worker 才解析运行时密钥；
- PostgreSQL 幂等创建、Worker heartbeat、租约恢复和错误端点已有实现；
- `_SUCCESS` 崩溃窗口和部分产物状态已有恢复/拒绝逻辑；
- readiness 会检查数据库、迁移、共享卷、Profile 和 Worker；
- Docker Compose 已建立迁移 gate、网络隔离、持久卷和非 root 运行；
- 确定性测试、文档构建和 Compose smoke 均可以通过。

原始审查发现了一个阻塞性分布式并发缺陷：旧 Worker 在租约过期并被新 Worker 接管后，仍可以更新进度、写入失败或把任务标记为完成。该问题直接破坏计划所要求的安全扩容和租约所有权语义。

原始实现的 Attempt/错误历史也未完整持久化，Docker 镜像没有真正固定版本，共享卷权限文档与镜像实际行为不一致。

上述本地问题现已按本文“修复执行摘要”处理并通过确定性门禁。当前剩余外部门槛是 GitHub CI 审批执行，以及发布前使用真实模型密钥完成一次手工 Provider 验收。

## 3. 必须修复的问题

### 3.1 [P1] 所有运行态写入必须受租约所有权保护

**状态：** 已修复并通过 SQLite/PostgreSQL 回归测试  
**阻塞合并：** 否；仍需 GitHub CI 复核  
**涉及文件：**

- `hyperextract/service/repository.py`
- `hyperextract/service/worker.py`
- `hyperextract/service/runner.py`
- `tests/service/test_repository.py`
- `tests/service/test_worker.py`
- 建议补充 PostgreSQL 并发测试

#### 问题

以下 Repository 方法没有接收或验证 `worker_id`：

- `update_progress()`
- `fail()`
- `complete()`

它们只根据 `run_id` 读取并修改任务。Worker 在调用这些方法之前只检查本地 `lease_lost` Event，但 Event 检查与数据库写入之间不存在原子性保证。

已复现以下状态：

```text
new_owner_before worker-new running
stale_complete_succeeded completed {'from': 'stale-worker'} None
```

复现场景：

1. `worker-old` 获得任务租约；
2. 租约过期；
3. 任务被恢复并由 `worker-new` 重新获得；
4. `worker-old` 调用 `complete()`；
5. 数据库接受旧 Worker 的完成写入，并清除新 Worker 的租约。

这违反 API 计划 Task 5 的要求：

> Every mutation of a running task includes `lease_owner == worker_id`.

#### 修复要求

- [ ] `update_progress()` 接收 `worker_id` 或 claim token；
- [ ] `fail()` 接收 `worker_id` 或 claim token；
- [ ] `complete()` 接收 `worker_id` 或 claim token；
- [ ] 所有运行态更新至少包含以下条件：

```text
run_id == expected_run_id
status == "running"
lease_owner == expected_worker_id
```

- [ ] 推荐增加不可复用的 `claim_generation` 或 `lease_token` 作为 fencing token；
- [ ] 更新失败时返回明确的 lease-lost 结果，不允许静默覆盖；
- [ ] `CourseRunExecutor.event_sink()` 必须携带当前 Worker claim 身份；
- [ ] 发布产物前后都要验证 claim 仍然有效；
- [ ] 旧 Worker 丢失租约后不得发布产物、失败任务或完成任务；
- [ ] 增加“旧 Worker 与新 Worker 交错执行”的确定性测试；
- [ ] 增加 PostgreSQL 下的真实并发测试，不能只使用 SQLite。

#### 验收标准

以下行为必须被测试证明：

- [ ] 新 Worker 接管后，旧 Worker 的 `update_progress()` 被拒绝；
- [ ] 新 Worker 接管后，旧 Worker 的 `fail()` 被拒绝；
- [ ] 新 Worker 接管后，旧 Worker 的 `complete()` 被拒绝；
- [ ] 新 Worker 接管后，旧 Worker 不能发布最终 artifacts；
- [ ] 新 Worker 能继续从 checkpoint 执行并完成同一个 `run_id`。

### 3.2 [P2] 完整持久化 Attempt 生命周期

**状态：** 已修复  
**阻塞合并：** 否；仍需 GitHub CI 复核  
**涉及文件：**

- `hyperextract/service/repository.py`
- `hyperextract/service/db_models.py`
- `tests/service/test_repository.py`
- `tests/service/test_worker.py`

#### 问题

`he_run_attempts` 已建立，但目前只在 `fail()` 中插入记录。因此：

- 成功 attempt 没有记录；
- 取消 attempt 没有记录；
- 正在运行的 attempt 没有开始记录；
- 崩溃恢复前的 attempt 没有明确终态；
- `started_at` 使用整个 Run 的 `created_at`，并不代表实际 attempt 启动时间。

这意味着计划 Task 4 所称的 “Persist Attempts” 尚未完整实现。

#### 修复要求

- [ ] claim 成功时创建当前 attempt 记录；
- [ ] completed 时将 attempt 更新为 completed 并填写 `ended_at`；
- [ ] cancelled 时将 attempt 更新为 cancelled 并填写 `ended_at`；
- [ ] failed 时更新已有 attempt，而不是无条件新增；
- [ ] lease 过期时记录 crashed/recovering 等明确终态，或在文档中定义等价状态；
- [ ] resume 后创建新的 attempt number；
- [ ] 保持 `(run_id, attempt)` 唯一；
- [ ] 增加完成、取消、失败、恢复和再次失败的 attempt 历史测试。

### 3.3 [P2] 恢复耗尽错误必须进入可查询错误历史

**状态：** 已修复  
**阻塞合并：** 否；仍需 GitHub CI 复核  
**涉及文件：**

- `hyperextract/service/repository.py`
- `tests/service/test_repository.py`
- `tests/service/test_runs_api.py`

#### 问题

`requeue_expired_leases()` 在达到最大恢复次数时只设置：

```json
{
  "code": "WORKER_RECOVERY_EXHAUSTED",
  "message": "Worker recovery limit was reached"
}
```

但没有插入 `he_run_errors`。实际验证结果为：

```text
status failed
summary {'code': 'WORKER_RECOVERY_EXHAUSTED', ...}
queryable_errors []
```

因此 `GET /v1/runs/{run_id}/errors` 无法返回该失败，与“错误历史稳定、可查询”的完成门槛不符。

#### 修复要求

- [ ] 在恢复耗尽事务中插入 `RunErrorEntity`；
- [ ] 错误 code 使用 `WORKER_RECOVERY_EXHAUSTED`；
- [ ] source 使用稳定值，例如 `worker` 或 `recovery`；
- [ ] 记录当前 attempt；
- [ ] 同时结束对应 attempt；
- [ ] API 测试验证 `/errors` 返回该错误；
- [ ] 确保错误消息经过统一脱敏和长度限制。

### 3.4 [P2] Docker 构建镜像必须固定版本

**状态：** 已修复并完成 linux/amd64 构建  
**阻塞合并：** 否  
**涉及文件：**

- `docker/service.Dockerfile`
- `tests/docker/test_service_docker_files.py`

#### 问题

当前 Dockerfile 使用：

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder
```

这是无版本漂移标签，但 Dockerfile 注释和测试将其描述为 pinned。计划明确要求使用 `ghcr.io/astral-sh/uv:<version>`，Docker Completion Gate 也要求 deterministic image。

当前测试只检查字符串中存在 `ghcr.io/astral-sh/uv:`，不能识别无版本标签。

#### 修复要求

- [ ] 使用真实存在的固定 uv 版本标签，或固定 image digest；
- [ ] 考虑同时固定 `python:3.11-slim` 的具体 patch/digest；
- [ ] 考虑固定 `postgres:17` 的具体版本/digest；
- [ ] 测试必须拒绝 `python3.11-bookworm-slim` 这种无 uv 版本标签；
- [ ] 重新运行 linux/amd64 Docker build 和 smoke test。

### 3.5 [P2] 设置真实的 `umask 0002` 或修正文档

**状态：** 已修复并完成容器级权限验证  
**阻塞合并：** 否  
**涉及文件：**

- `docker/entrypoint.sh`
- `docker/README.md`
- `tests/docker/test_service_docker_files.py`

#### 问题

Docker README 声称：

```text
rely on umask 0002 (set in the image)
```

但 Dockerfile 和 entrypoint 都没有设置 umask。实际构建镜像中验证结果为：

```text
umask = 0022
created file = 0644, owner 10001:10001
```

因此依赖 GID 10001 进行共享卷组写入的 caller 可能无法写入或协作。

#### 修复要求

- [ ] 在 entrypoint 的 `exec "$@"` 之前执行 `umask 0002`；
- [ ] 增加真实容器测试，验证新建文件为 `0664`、目录为 `0775`；
- [ ] 验证 API 与 Worker 创建的目录和 artifacts 权限；
- [ ] 如果不支持组写入，则删除文档中的 `umask 0002` 承诺并明确要求 caller 使用 UID 10001。

## 4. 应当改进的问题

### 4.1 [P3] Smoke test 没有可靠证明 Worker 重启成功

**涉及文件：**

- `scripts/service-compose-smoke.sh`
- `tests/docker/test_service_docker_files.py`

重启后脚本立即通过 `/health/ready` 判断成功。但 readiness 接受 `2 * heartbeat_seconds` 内的 heartbeat，因此可能读取重启前的旧 Worker heartbeat。Worker 即使重启后立即退出，API 仍可能短暂返回 ready。

- [ ] 重启前记录最新 heartbeat；
- [ ] 重启后等待新的 `worker_id` 或更新后的 heartbeat；
- [ ] 检查 Worker 容器状态持续为 running；
- [ ] 再验证 API readiness 和共享卷 sentinel。

### 4.2 [P3] `HE_IMAGE` 被声明但未被 Compose 使用

`docker/.env.example` 和 `docker/README.md` 声明 `HE_IMAGE` 可以覆盖预构建镜像，但 `service.compose.yml` 只有 `build:`，没有使用 `${HE_IMAGE}`。

- [ ] 在服务中使用 `image: ${HE_IMAGE:-hyper-extract-service:dev}`；或
- [ ] 删除 `HE_IMAGE` 配置和相关文档，避免伪配置。

### 4.3 [P3] 计划中的 Ruff 命令在干净 uv 环境中不可直接复现

执行 `uv sync --extra all` 后，`uv run ruff` 找不到 Ruff。CI 单独使用 `uv tool install ruff`，本次审查使用 `uvx ruff` 后 lint/format 通过。

- [ ] 将 Ruff 加入 dev dependency；或
- [ ] 将计划和开发文档统一为 `uvx ruff` / `uv tool run ruff`；
- [ ] 确保本地命令与 CI 完全一致。

### 4.4 [P3] Smoke test 没有执行真实 Provider 验收

计划明确将真实 Provider 验收定义为手工、opt-in 流程，因此这不是确定性 CI 失败。但在正式发布前仍需执行一次：

- [ ] 使用一个小型、不可变的 Package 1.1；
- [ ] 注入仅属于 Worker 的真实模型密钥；
- [ ] validate package；
- [ ] create run；
- [ ] poll 至 completed；
- [ ] 验证 `_SUCCESS`；
- [ ] 验证 manifest 中每个 SHA-256；
- [ ] 验证 `run-summary.json.extraction_brief`；
- [ ] 检查 API 容器环境中不存在模型密钥。

## 5. 计划任务符合度

| 任务 | 结论 | 说明 |
|---|---|---|
| API Task 1 | 完成 | API package boundary 与共享 Runtime 已建立 |
| API Task 2 | 完成 | Package 1.0/1.1、布局和 URI 校验已接入 |
| API Task 3 | 完成 | public descriptor 与 runtime secret resolution 已拆分 |
| API Task 4 | 修复后完成 | Attempt 和 recovery error history 已补齐 |
| API Task 5 | 修复后完成 | 运行态写入、续租和产物发布均受 live lease owner 保护 |
| API Task 6 | 基本完成 | 产物恢复与计划覆盖的脱敏场景已实现 |
| API Task 7 | 完成 | readiness 与中英文服务文档已更新 |
| Docker Task 1 | 修复后完成 | 非 root、lockfile、固定 uv/Python 版本均已验证 |
| Docker Task 2 | 完成 | Alembic gate、持久 PostgreSQL、无生产 create_all |
| Docker Task 3 | 修复后完成 | 网络、密钥、卷、HE_IMAGE 和 umask 行为一致 |
| Docker Task 4 | 修复后完成 | 健康、停止策略与 owner-fenced 扩容已覆盖 |
| Docker Task 5 | 修复后完成 | Smoke 验证 fresh Worker ID、readiness 和共享卷持久性 |

## 6. 已执行验证

### 6.1 Python 测试

执行：

```bash
OPENAI_API_KEY="" uv run pytest -m "not integration" -q
```

结果：

```text
495 passed, 5 skipped, 6 deselected
```

### 6.2 PostgreSQL 并发测试

使用临时 PostgreSQL 17 容器执行：

```bash
HE_TEST_POSTGRES_URL="postgresql+psycopg://..." \
OPENAI_API_KEY="" \
uv run pytest tests/service/test_repository_postgres.py -q
```

结果：

```text
1 passed
```

注意：现有测试的两个线程使用相同候选 `run_id`。建议后续改用不同候选 `run_id`、相同 Idempotency-Key，以更直接证明 Idempotency-Key 唯一约束决定唯一赢家。

### 6.3 Lint 与格式

执行：

```bash
uv run ruff check hyperextract
uv run ruff format --check hyperextract
```

结果：通过。

### 6.4 严格文档构建

执行：

```bash
uv run mkdocs build --strict
```

结果：通过。

中英文课程流水线页面现已同时加入 nav，并补齐英文版本。

### 6.5 Compose 配置

执行：

```bash
docker compose \
  --env-file docker/.env.example \
  -f docker/service.compose.yml \
  -f docker/service.compose.dev.yml \
  config --quiet
```

结果：通过。

### 6.6 Compose smoke

执行：

```bash
sh scripts/service-compose-smoke.sh
```

结果：通过，包括：

- 镜像构建；
- PostgreSQL 启动；
- Alembic migration gate；
- API/Worker 启动；
- readiness；
- 共享 `/exchange` sentinel；
- API/Worker restart；
- sentinel 持久性；
- 隔离项目清理。

### 6.7 GitHub CI 状态

截至审查时：

- Tests workflow 等待 maintainer approval；
- Lint workflow 等待 maintainer approval；
- GitHub 尚无正式成功的 CI job 结论。

相关链接：

- [Tests workflow](https://github.com/yifanfeng97/Hyper-Extract/actions/runs/29331940653)
- [Lint workflow](https://github.com/yifanfeng97/Hyper-Extract/actions/runs/29331940734)

## 7. 建议处理顺序

按以下顺序修复，可以减少返工：

1. [x] 修复 P1 lease fencing 和所有运行态 owner-checked mutation；
2. [x] 增加旧/新 Worker 交错执行的 PostgreSQL 测试；
3. [x] 补齐 attempt 生命周期；
4. [x] 将 recovery exhaustion 写入 error history；
5. [x] 固定 Docker 基础镜像版本；
6. [x] 实际设置并验证 `umask 0002`；
7. [x] 加强 Worker restart smoke 验证；
8. [x] 处理 `HE_IMAGE` 和 Ruff 命令一致性；
9. [ ] 合并或 rebase 最新 `main`；
10. [x] 运行完整确定性 gate；
11. [ ] 由 maintainer 批准并确认 GitHub Tests/Lint；
12. [ ] 发布前执行一次手工真实 Provider 验收。

## 8. 重新审查门槛

完成修复后，至少运行：

```bash
OPENAI_API_KEY="" uv run pytest tests/service tests/docker -q
OPENAI_API_KEY="" uv run pytest -m "not integration" -q
HE_TEST_POSTGRES_URL="$HE_SERVICE_DATABASE_URL" \
  OPENAI_API_KEY="" \
  uv run pytest tests/service/test_repository_postgres.py -q
uv run ruff check hyperextract
uv run ruff format --check hyperextract
uv run mkdocs build --strict
docker compose \
  --env-file docker/.env.example \
  -f docker/service.compose.yml \
  -f docker/service.compose.dev.yml \
  config --quiet
sh scripts/service-compose-smoke.sh
```

最终合并条件：

- [x] 不存在已知的旧 Worker 覆盖新 Worker 状态路径；
- [x] 所有运行态 Repository mutation 都受 claim/lease fencing；
- [x] Attempt 生命周期完整；
- [x] 所有已知 terminal failure 均可通过 `/errors` 查询；
- [x] 镜像构建固定版本且可复现；
- [x] 共享卷权限实现与文档一致；
- [x] Docker smoke 证明新 Worker heartbeat；
- [ ] GitHub Tests 与 Lint 成功；
- [ ] PR 与最新 `main` 集成后重新通过全部 gate。

## 9. 推送后重新审查（2026-07-14）

### 9.1 远端状态

- 修复提交：`b36f3c21e5c7e90f0550b0ea47ff59aeb55c8d3c`；
- 提交说明：`fix(service): fence worker leases and harden deployment`；
- 已推送至：`origin/feature/internal-api-service`；
- MR #61 已识别为 19 个提交，最新 head 为 `b36f3c2`；
- 本地工作树与远端分支一致且无未提交改动。

### 9.2 最新 main 集成复核

重新获取 `yifanfeng97/Hyper-Extract:main` 后，最新提交为
`db741358720837449e36569b11029956b8209e2b`。MR 分支当前落后 main 3 个提交、领先
19 个提交。

使用 `git merge-tree --write-tree upstream/main HEAD` 验证：无文本冲突。随后从该
合并树创建一次性 detached worktree，并对“最新 main + MR”执行完整 gate：

```text
501 passed, 6 skipped, 6 deselected
ruff check: passed
ruff format --check: passed (103 files already formatted)
mkdocs build --strict: passed
```

因此，分支虽然尚未实际 rebase/merge 最新 main，但当前合并结果已通过本地集成验证。

### 9.3 二次代码审查结论

未发现新的 P0/P1/P2 阻断缺陷。此前发现的 lease fencing、stale Worker publication、
attempt/error history、Docker pin、共享卷 umask、镜像复用和 Worker restart smoke
问题均已修复并有自动化覆盖。

当前剩余事项：

1. [ ] 由上游 maintainer 批准 fork PR 的 GitHub Actions；最新 Tests #86 和 Lint #78
   均为 `Action required`，工作流尚未实际运行；
2. [ ] 在合并前把最新 main 合入或 rebase 到 MR 分支（当前无冲突，且合并树 gate 已通过）；
3. [ ] 发布前使用真实 Provider 做一次人工验收；确定性测试未覆盖真实模型响应、限流、
   凭据和网络行为。

重新审查结论：**代码实现已达到可合并质量；在 GitHub Tests/Lint 正式通过前，不建议
点击合并。**
