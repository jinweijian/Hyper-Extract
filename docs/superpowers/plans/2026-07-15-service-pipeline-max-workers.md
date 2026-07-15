# HE Service Pipeline Max Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 HE API/Worker 服务通过 `HE_SERVICE_PIPELINE_MAX_WORKERS` 配置单个课程任务并行处理的 chunk 数量，默认保持为 `2`。

**Architecture:** `ServiceSettings` 负责读取并校验正整数；`CourseRunExecutor.pipeline_options()` 将设置传入 `PipelineOptions.max_workers`。模型 Profile 的 `recommended_concurrency` 继续作为供应商侧上限，因此模型请求的有效并发为两者较小值。

**Tech Stack:** Python 3.11+、dataclasses、pytest、Docker Compose、MkDocs。

## Global Constraints

- 只修改 HE 服务路径，不修改 CLI `--max-workers`。
- 环境变量名固定为 `HE_SERVICE_PIPELINE_MAX_WORKERS`。
- 默认值固定为 `2`，必须是大于零的整数。
- 不允许调用方通过 `POST /v1/runs` 覆盖该值。
- 不处理本轮 review 中的 Course Graph、metadata 或跨仓库 fixture 问题。

---

### Task 1: 配置读取和 Runner 传递

**Files:**
- Modify: `hyperextract/service/settings.py`
- Modify: `hyperextract/service/runner.py`
- Test: `tests/service/test_settings_upload.py`
- Test: `tests/service/test_runner.py`

**Interfaces:**
- Produces: `ServiceSettings.pipeline_max_workers: int`
- Consumes: `CourseRunExecutor.pipeline_options(...).max_workers`

- [x] **Step 1: 写入失败测试**

验证默认值为 `2`、环境变量可以覆盖为 `4`、零值被拒绝，以及 Runner 使用 Settings 中的值。

- [x] **Step 2: 运行定向测试并确认因字段不存在或仍为硬编码而失败**

Run: `OPENAI_API_KEY="" uv run pytest tests/service/test_settings_upload.py tests/service/test_runner.py -q`

- [x] **Step 3: 实现最小配置传递**

在 `ServiceSettings` 增加 `pipeline_max_workers`，使用现有 `_env_int()` 读取，并将 Runner 的 `max_workers=2` 改为 `max_workers=self.settings.pipeline_max_workers`。

- [x] **Step 4: 运行定向测试确认通过**

Run: `OPENAI_API_KEY="" uv run pytest tests/service/test_settings_upload.py tests/service/test_runner.py -q`

### Task 2: Docker 和运维文档

**Files:**
- Modify: `docker/.env.example`
- Modify: `docker/service.compose.yml`
- Modify: `docker/README.md`
- Modify: `docs/zh/guides/internal-service.md`
- Modify: `docs/en/guides/internal-service.md`

**Interfaces:**
- Produces: Compose environment `HE_SERVICE_PIPELINE_MAX_WORKERS=${HE_SERVICE_PIPELINE_MAX_WORKERS:-2}`

- [x] **Step 1: 在示例配置和 Worker 环境中声明参数**

- [x] **Step 2: 说明该参数控制单任务 chunk 并发，实际模型请求并发还受 Profile `recommended_concurrency` 限制**

- [x] **Step 3: 运行完整验证**

Run:

```bash
OPENAI_API_KEY="" uv run pytest -q
uv run ruff check hyperextract
uv run ruff format --check hyperextract
uv run mkdocs build --strict
docker compose --env-file docker/.env.example -f docker/service.compose.yml config
git diff --check
```
