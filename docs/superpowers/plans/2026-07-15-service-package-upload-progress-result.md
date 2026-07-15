# HE Package 上传、实时进度与结果交付实施计划

> **For agentic workers:** 按任务顺序实施并使用测试先行；每完成一个任务后更新复选框。所有确定性测试必须显式设置 `OPENAI_API_KEY=""`，避免加载本地 `.env` 后误用真实模型。

**状态：** 已实施  
**优先级：** P0  
**关联计划：** `2026-07-13-internal-api-service.md`、`2026-07-13-internal-service-docker.md`、`2026-07-13-p0-1-document-package-contract.md`

**目标：** 将 GraphAlchemy 与 HE 之间的 Document Package 交付从跨服务共享目录改为 HTTP 上传，同时保留 HE API 与 Worker 在同一 Docker Compose、同一服务器内通过 `/exchange` 共享卷协作。HE API 验包并持久化任务后返回 `202`；Worker 使用共享卷执行任务，通过结构化进度文件提供动态进度，最终由 HE API 通过 HTTP 返回知识图谱结果。

**架构：** GraphAlchemy 仍负责接收原始 PDF/DOCX、调用 Docling、生成 Document Package v1.1，并将 Package 目录封装为 `.hepkg.tar.gz` 上传给 HE。HE API 将上传内容流式写入 `/exchange/uploads`，安全解压到 `/exchange/packages/.staging-*`，完成契约与哈希校验后原子发布正式 Package，再创建 PostgreSQL `queued` 任务。Worker 是 Compose 中常驻进程，从 PostgreSQL 领取任务，在 `/exchange/runs/<run_id>/work` 执行；高频 UI 进度不写数据库，而是原子更新 `/exchange/runs/<run_id>/state/progress.json`。Worker 完成后原子发布 `artifacts/`，HE API 校验 manifest 与 `_SUCCESS` 后流式返回 `course-graph.json`。

**技术栈：** Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy 2.x、PostgreSQL、Docker Compose、POSIX 文件系统、pytest、Document Package v1.1。

---

## 1. 已确认的设计决策

### 1.1 服务边界

```text
最终调用方
  -> GraphAlchemy POST create(file + subjectName)
  -> GraphAlchemy / Docling 生成 Document Package v1.1
  -> GraphAlchemy 压缩为 .hepkg.tar.gz
  -> HE POST /v1/runs 上传 Package
  -> HE API 验包、原子发布、入队并返回 202 + heRunId
  -> HE Worker 从共享卷读取 Package 并执行
  -> HE status 返回结构化动态进度
  -> HE result 流式返回 course-graph.json
  -> GraphAlchemy 校验并保存结果
  -> GraphAlchemy 才对外报告 COMPLETED
```

- GraphAlchemy 与 HE 之间只通过 HTTP 交互，不共享 Docker volume，也不传递可供调用方直接读取的 `file://` 路径。
- HE API 与 HE Worker 属于同一 Compose、同一服务器，继续挂载同一个 `exchange-data:/exchange`。
- PostgreSQL继续保存可靠的任务生命周期、幂等、租约、恢复次数、终态错误和 Worker 心跳。
- 高频展示进度和动态文案不写 PostgreSQL，也不从原始 stdout/文本日志解析。
- 本阶段不引入 S3、MinIO、消息队列、SSE、Webhook 或跨服务器 HE Worker。

### 1.2 `/exchange` 目录布局

```text
/exchange/
  uploads/
    .upload-<uuid>.tar.gz
  packages/
    .staging-<uuid>/
    pkg_<package_fingerprint>.hepkg/
  runs/
    <run_id>/
      state/
        progress.json
      work/
        .he-run/
      diagnostics/
        attempts/
      artifacts/
        course-graph.json
        run-summary.json
        quality-report.json
        performance-report.json
        cost-report.json
        artifact-manifest.json
        _SUCCESS
```

### 1.3 传输封装与内容契约分离

- Document Package v1.1 仍然是目录契约；`.tar.gz` 只作为跨服务传输封装。
- HE 归档上传上限由 `HE_SERVICE_MAX_UPLOAD_BYTES` 按字节配置，默认
  `500000000`（500 MB）；必须在流式读取期间增量执行，不能把限制写死在路由中。
- 归档根目录必须直接包含 `manifest.json`、`outline.json`、`provenance.jsonl`、`extraction-brief.yaml` 和 `content/`，不得额外嵌套一个不确定名称的顶层目录。
- `package_fingerprint` 是解压后 Document Package 的规范指纹。
- `transport_sha256` 是上传 `.tar.gz` 字节流的 SHA-256。
- 两个哈希必须分别校验，不能用 tarball 哈希代替 Package 内部完整性校验。

### 1.4 `202 Accepted` 的准确语义

HE 只有在以下条件全部满足后才能返回 `202`：

1. 上传字节流完整落盘，大小未超限。
2. `transport_sha256` 校验通过。
3. tar 成员通过路径、类型、数量和解压大小安全检查。
4. Package 已安全解压到 staging。
5. Document Package v1.1 Schema、布局、Brief、逐文件哈希和规范指纹校验通过。
6. staging 已在同一文件系统内原子发布到最终 Package 目录。
7. PostgreSQL 中已存在可领取的 `queued` run，或幂等键命中完全相同的既有 run。
8. `/exchange/runs/<run_id>/work`、`state` 和 `diagnostics/attempts` 已准备完成。

`202` 只表示任务已被可靠接受，不表示模型处理已完成。

### 1.5 进度与日志边界

- `.he-run/events.jsonl`、容器 stdout 和 diagnostics 是内部审计/运维材料，不是公共 API 的数据源。
- Worker 从现有 `RunEvent` 流生成一个有界、结构化的最新进度快照 `state/progress.json`。
- `progress.json` 使用临时文件、`flush`、`fsync` 和 `os.replace` 原子更新。
- API 读取快照时必须校验 `run_id`、`attempt` 和内部 `worker_id` 是否与数据库当前租约一致。
- 进度文件缺失、半写、损坏、过期或所有者不匹配时，status 安全降级为通用运行文案，不得使任务失败。

### 1.6 结果交付

- Worker 继续使用现有 staging -> `artifacts/` 原子发布，并写 `artifact-manifest.json` 与 `_SUCCESS`。
- HE API 不返回 `/exchange/...` 文件 URI。
- `GET /v1/runs/{run_id}/result` 只返回 manifest 中固定声明的 `course-graph.json`，不接受任意路径。
- GraphAlchemy 下载、校验并保存结果后，才将自己的公共任务标记为 `COMPLETED`。

---

## 2. 目标 API 契约

### 2.1 创建 HE 任务

```http
POST /v1/runs
Content-Type: multipart/form-data
Idempotency-Key: <1-255 chars>
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `package` | binary | 是 | `.hepkg.tar.gz` 字节流 |
| `contract_version` | string | 是 | 本阶段必须为 `1.1` |
| `package_fingerprint` | string | 是 | 解压后 Package 规范 SHA-256 |
| `transport_sha256` | string | 是 | 上传归档字节流 SHA-256 |
| `options` | JSON string | 否 | 现有 pipeline、execution、client_context；缺省时使用课程图默认配置 |

成功响应：

```http
HTTP/1.1 202 Accepted
Location: /v1/runs/run_xxx
Retry-After: 3
```

```json
{
  "run_id": "run_xxx",
  "status": "queued",
  "stage": "queued",
  "stage_status": "waiting",
  "attempt": 1,
  "activity": "RUN_QUEUED",
  "message": "任务已接受，正在等待执行",
  "message_seq": 1,
  "progress": null,
  "links": {
    "self": "/v1/runs/run_xxx",
    "result": "/v1/runs/run_xxx/result",
    "artifacts": "/v1/runs/run_xxx/artifacts",
    "errors": "/v1/runs/run_xxx/errors"
  }
}
```

错误响应：

| HTTP | 错误码 | 场景 |
|---:|---|---|
| `400` | `INVALID_MULTIPART_REQUEST` | multipart 或字段无法解析 |
| `400` | `PACKAGE_REQUIRED` | 缺少归档文件 |
| `413` | `PACKAGE_UPLOAD_TOO_LARGE` | 上传压缩包超过限制 |
| `422` | `PACKAGE_ARCHIVE_INVALID` | gzip/tar 损坏或成员不合法 |
| `422` | `PACKAGE_EXPANDED_TOO_LARGE` | 解压后大小或文件数超过限制 |
| `422` | `DOCUMENT_PACKAGE_INVALID` | Package Schema 或布局无效 |
| `422` | `DOCUMENT_PACKAGE_HASH_MISMATCH` | 规范指纹不匹配 |
| `422` | `PACKAGE_TRANSPORT_HASH_MISMATCH` | tarball 字节流哈希不匹配 |
| `409` | `IDEMPOTENCY_KEY_CONFLICT` | 相同幂等键对应不同请求 |
| `500` | `PACKAGE_PUBLICATION_FAILED` | staging 无法可靠发布或状态不一致 |

### 2.2 查询 HE 状态

```http
GET /v1/runs/{run_id}
Cache-Control: no-store
```

运行中示例：

```json
{
  "run_id": "run_xxx",
  "status": "running",
  "stage": "local_extract",
  "stage_status": "running",
  "attempt": 1,
  "activity": "EXTRACTING_CHUNK",
  "message": "正在分析第 8/28 个内容块",
  "message_seq": 37,
  "progress": {
    "current": 8,
    "total": 28,
    "percent": 28.57
  },
  "updated_at": "2026-07-15T06:30:08Z",
  "links": {
    "self": "/v1/runs/run_xxx",
    "result": "/v1/runs/run_xxx/result",
    "artifacts": "/v1/runs/run_xxx/artifacts",
    "errors": "/v1/runs/run_xxx/errors"
  }
}
```

进度快照内部可包含 `worker_id`、`run_id` 和 `attempt`，但公共响应不得包含 `worker_id`、租约时间、容器名、内部绝对路径或原始日志。

同一高层阶段必须支持多个 activity 和动态 message，例如：

```text
CONTEXT_PLANNING      正在规划文档的知识抽取顺序
EXTRACTING_CHUNK      正在分析第 8/28 个内容块
VALIDATING_CHUNK      正在校验第 8 个内容块的知识点
MERGING_SECTION       正在合并第 3/12 章的知识关系
DEDUPLICATING         正在消除重复知识点和关系
QUALITY_CHECKING      正在检查知识图谱完整性
ARTIFACT_PUBLISHING   正在发布知识图谱结果
WORKER_RECOVERING     执行进程已恢复，正在从检查点继续处理
```

在长模型调用期间，进度文件可以按 `HE_SERVICE_PROGRESS_SECONDS` 周期更新 `message_seq` 和安全的等待文案，但不得伪造 `current`、`total` 或百分比。

### 2.3 获取主结果

```http
GET /v1/runs/{run_id}/result
```

成功响应：

```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Disposition: attachment; filename="course-graph-run_xxx.json"
Content-Length: <manifest size>
ETag: "<course-graph sha256>"
Cache-Control: private, no-transform
```

接口在发送响应前必须验证数据库终态、`_SUCCESS`、manifest 哈希、结果文件大小与 SHA-256。

| HTTP | 错误码 | 场景 |
|---:|---|---|
| `404` | `RUN_NOT_FOUND` | run 不存在 |
| `409` | `ARTIFACTS_NOT_READY` | run 尚未 completed |
| `500` | `ARTIFACT_STATE_INCONSISTENT` | 数据库终态与产物不一致 |

---

## 3. 实施任务

### Task 1: 用测试锁定 Package 传输与 HTTP 契约

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/requests.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/responses.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runs_api.py`
- Create: `/Users/king/website/Hyper-Extract/tests/service/test_package_upload.py`

- [x] 为 multipart create 编写失败测试，覆盖必填字段、默认 options、严格未知字段和 Idempotency-Key。
- [x] 锁定 `202` 响应不再暴露 `file:///exchange/...`，只返回稳定 HTTP links。
- [x] 锁定 `400/409/413/422` 的错误码和响应 Schema。
- [x] 锁定归档根目录必须直接包含 Package 文件。
- [x] 运行 RED：

```bash
cd /Users/king/website/Hyper-Extract
OPENAI_API_KEY="" uv run pytest tests/service/test_runs_api.py tests/service/test_package_upload.py -q
```

### Task 2: 实现流式上传与安全 tar 解包

**Files:**
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/package_upload.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/settings.py`
- Modify: `/Users/king/website/Hyper-Extract/pyproject.toml`
- Modify: `/Users/king/website/Hyper-Extract/uv.lock`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_package_upload.py`

- [x] 增加 FastAPI multipart 所需的锁定依赖，不使用 plain `pip`。
- [x] 增加上传配置：最大归档字节数、最大解压字节数、最大成员数、读取块大小。
- [x] 上传过程中流式计算 SHA-256，不把整个文件载入内存。
- [x] Python 3.11/3.12 均显式验证每个 tar 成员；不得依赖版本相关的 `extractall()` 默认安全行为。
- [x] 拒绝绝对路径、`..`、空路径、重复目标路径、设备文件、FIFO、软链接、硬链接和超出限制的成员。
- [x] 只创建规则文件和目录，并确保解析后的每个目标都位于 staging 根目录内。
- [x] 任何失败都清理 upload 临时文件和 staging，不删除既有正式 Package。

### Task 3: 扩展共享卷存储边界并原子发布 Package

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/storage.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/runtime.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_storage.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_package_upload.py`

- [x] 将现有 `SharedVolumeStore` 拆出明确的 `upload_root`、`package_root` 和 `run_root`。
- [x] `runtime.prepare()` 创建 `/exchange/uploads`、`packages` 和 `runs`，不在运行时递归 chown。
- [x] Package 最终名称由已验证的规范指纹导出：`pkg_<fingerprint>.hepkg`。
- [x] staging 与最终目录必须位于同一 `package_root`，使用 `os.replace`/原子 rename 发布。
- [x] 同指纹目录已存在时重新校验并安全复用；内容不一致时报 `PACKAGE_STATE_INCONSISTENT`，不得覆盖。
- [x] 上传临时文件只在验证期间保留；成功发布后删除归档。
- [x] 不再接受调用方提交任意 `file://` URI 作为 create 输入。

### Task 4: 改造 `POST /v1/runs` 并保证 `202` 边界

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/requests.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/commands.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/storage.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runs_api.py`

- [x] 解析 multipart 字段并将 `options` 严格验证为现有 pipeline/execution/client_context 子契约。
- [x] 上传、解包、Package 校验和原子发布全部成功后才构造 `RunCommand`。
- [x] `request_fingerprint` 覆盖 Package 规范指纹、contract version、pipeline、Profile、Model Profile 指纹及调用方上下文，不包含临时路径或 tar metadata。
- [x] `request_json` 保存稳定 `resolved_package_ref`，不保存调用方路径和 upload 临时路径。
- [x] Package 发布与数据库 insert 之间发生失败时执行可审计的补偿清理；不得删除被既有 run 引用的内容寻址 Package。
- [x] 幂等键命中相同请求时返回同一 run；命中不同请求时返回 `409`。
- [x] 返回 `202` 前创建 run 的 `state`、`work` 和 `diagnostics/attempts` 目录。

### Task 5: Worker 使用稳定 Package 引用和固定工作根目录

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/runner.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/worker.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_worker.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runner.py`

- [x] Worker 继续作为 Compose 常驻进程，不因单个 create 请求重新启动。
- [x] 从数据库 request snapshot 读取 `resolved_package_ref`，通过 `HE_SERVICE_EXCHANGE_ROOT=/exchange` 安全解析正式 Package。
- [x] 拒绝 `.staging-*`、路径逃逸、缺失 Package 和指纹不匹配。
- [x] 每个 run 只在 `/exchange/runs/<run_id>/work` 写 checkpoint 和中间结果。
- [x] 恢复任务沿用同一 Package 与 work 目录，不重新接收上传内容。

### Task 6: 用结构化文件替代高频数据库进度写入

**Files:**
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/progress.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/runner.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/settings.py`
- Create: `/Users/king/website/Hyper-Extract/tests/service/test_progress.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runner.py`

- [x] 定义严格 `ProgressSnapshot`：`schema_version`、`run_id`、`attempt`、内部 `worker_id`、`sequence`、`stage`、`activity`、`message`、`current`、`total`、`percent`、`updated_at`。
- [x] 复用现有 `RunEvent`，将 pipeline event sink 从“每个事件更新 PostgreSQL”改为“更新 `state/progress.json`”。
- [x] 使用唯一临时文件、`flush`、`fsync`、`os.replace`，沿用 `atomic_write_json` 的安全语义。
- [x] `percent` 只在 `current`、`total` 有效时计算并限制到 `0..100`，不得伪造进度。
- [x] 定义稳定 activity 映射和脱敏动态文案目录；同一 stage 可以产生多个 activity/message。
- [x] 增加独立 `HE_SERVICE_PROGRESS_SECONDS`（建议默认 5 秒），与数据库租约/Worker heartbeat 周期分离。
- [x] 长模型调用期间只轮换安全等待文案并递增 sequence，不改变真实 current/total。
- [x] 写文件失败只影响进度展示并记录运维日志，不得二次使 run 失败。

### Task 7: status 合并数据库生命周期与文件进度

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/responses.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runs_api.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_progress.py`

- [x] 数据库仍是 `queued/running/completed/failed/cancelled`、attempt、lease、恢复和错误码的唯一事实源。
- [x] `running` 时只接受 `run_id`、`attempt`、`worker_id == lease_owner` 的最新快照。
- [x] API 不返回内部 `worker_id`、lease、路径、Prompt、模型原始响应或异常堆栈。
- [x] 快照不存在、JSON 损坏、字段无效、所有者不匹配或超时后返回安全降级 message 和 `progress=null`。
- [x] `queued`、恢复中、取消中和终态分别返回稳定 activity/message。
- [x] 响应增加 `Cache-Control: no-store` 和建议轮询间隔，不因读取进度文件失败返回 `5xx`。
- [x] 原始 `.he-run/events.jsonl` 和容器日志永远不直接成为公共 status 输出。

### Task 8: 增加主结果下载并移除本地 URI 泄漏

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/artifacts.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/responses.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_artifacts.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runs_api.py`

- [x] 保留现有 `artifact-manifest.json`、逐文件 size/SHA-256、原子目录发布和 `_SUCCESS` 正确性契约。
- [x] 新增 `GET /v1/runs/{run_id}/result`，固定查找 manifest 中 `name=course_graph` 的文件。
- [x] 下载前调用完整产物一致性验证，禁止仅凭数据库 `completed` 读取文件。
- [x] 使用流式文件响应，设置 Content-Type、Content-Length、Content-Disposition 和基于 SHA-256 的 ETag。
- [x] 未完成返回 `409`，run 不存在返回 `404`，终态与文件不一致返回 `500 ARTIFACT_STATE_INCONSISTENT`。
- [x] 不接受调用方文件名或相对路径，不通过 API 暴露 model-audit 和 diagnostics。
- [x] 从 `RunResponse.output` 删除不可跨服务使用的 `file://` URI，使用 `links.result` 与 `links.artifacts` 替代。

### Task 9: 更新 HE API 与 Docker 文档

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/docs/en/guides/internal-service.md`
- Modify: `/Users/king/website/Hyper-Extract/docs/zh/guides/internal-service.md`
- Modify: `/Users/king/website/Hyper-Extract/docs/en/guides/document-package.md`
- Modify: `/Users/king/website/Hyper-Extract/docs/zh/guides/document-package.md`
- Modify: `/Users/king/website/Hyper-Extract/docker/README.md`
- Modify: `/Users/king/website/Hyper-Extract/docker/.env.example`
- Modify: `/Users/king/website/Hyper-Extract/docker/service.compose.yml`
- Modify: `/Users/king/website/Hyper-Extract/tests/docker/test_service_docker_files.py`

- [x] 中英文内部 API 文档同步替换 `package_uri` create 示例，新增 multipart curl、归档布局、202 条件、错误码、动态 status 和 result 下载示例。
- [x] Document Package 文档明确“目录是内容契约，tar.gz 是传输封装”，不能把 tarball SHA 当作 Package 指纹。
- [x] Docker README 删除“外部调用方必须挂载 exchange volume”的要求，改为“仅 HE API/Worker 共享 volume；外部调用方通过 HTTP”。
- [x] Compose 继续让 API/Worker 挂载同一 `exchange-data:/exchange`，不把 PostgreSQL 或 exchange 路径暴露给 GraphAlchemy。
- [x] 文档说明生产环境通过私有入口/反向代理访问 HE API，并配置上传体积、请求超时和流式传输；API 仍不持有模型密钥或模型公网出口。
- [x] `.env.example` 增加上传/解压/进度周期配置，保持无真实密钥。
- [x] 英文与中文 nav 保持同步；若不新增页面则不改变 `mkdocs.yml` 路径。

### Task 10: 更新 GraphAlchemy 调用方和公共 API 文档

**Files in GraphAlchemy:**
- Modify: `/Users/king/website/graphalchemy/nitro/server/client/HeClient.ts`
- Modify: `/Users/king/website/graphalchemy/nitro/server/service/KnowledgeGraphAiPipelineService.ts`
- Modify: `/Users/king/website/graphalchemy/nitro/docs/knowledge-graph-generator-api.md`
- Modify: `/Users/king/website/graphalchemy/nitro/docs/external-knowledge-extraction-service.md`
- Modify: `/Users/king/website/graphalchemy/nitro/docs/document-package.md`
- Modify: `/Users/king/website/graphalchemy/docker/conf/nitro.env`
- Modify: `/Users/king/website/graphalchemy/nitro/.env.example`
- Modify: `/Users/king/website/graphalchemy/README.md`
- Modify/Create: GraphAlchemy 对应 route、DTO、任务存储和测试文件

- [x] GraphAlchemy 公共 create 仍接收原始 `file + subjectName`；`.hepkg.tar.gz` 仅是 GraphAlchemy -> HE 的内部传输格式。
- [x] GraphAlchemy 构建 Package 后生成归档、Package 规范指纹与 transport SHA-256，使用 multipart 调用 HE create。
- [x] 保存 `publicRunId <-> heRunId`，不向最终调用方暴露 heRunId、HE Worker、内部 stage 或路径。
- [x] 公共 status 将 GraphAlchemy 预处理阶段和 HE stage/activity 映射为稳定三态、动态 message、messageSeq 和 progress。
- [x] HE 完成后，GraphAlchemy 进入 `RESULT_FETCH`，通过 HE result 下载、校验 ETag/SHA-256并原子保存；完成保存前不得对外返回 `COMPLETED`。
- [x] 公共 API 文档补齐结果交付。推荐新增 `GET /api/v1/knowledge/graph/result?runId=...`；status 的 completed 响应只返回结果元数据与下载 URL，不内联大型知识图谱。
- [x] 删除 `HE_PACKAGE_ROOT`、`HE_PACKAGE_URI_ROOT` 和共享路径映射说明；GraphAlchemy 只保留 HE 服务地址、鉴权/超时和本地任务存储配置。
- [x] 明确 status 提供结构化动态进度而非原始日志，并补充轮询、未知 activity、恢复和结果下载失败的兼容策略。

### Task 11: 端到端验证与完成门槛

**Files:**
- Modify/Create: `/Users/king/website/Hyper-Extract/tests/service/*`
- Modify/Create: `/Users/king/website/Hyper-Extract/tests/docker/*`
- Modify/Create: `/Users/king/website/Hyper-Extract/scripts/service-api-course-test.sh`
- Modify/Create: GraphAlchemy 对应集成测试

- [x] 测试恶意 tar：绝对路径、`..`、软链接、硬链接、设备文件、重复路径、超成员数和压缩炸弹。
- [x] 测试错误 gzip、transport hash 错误、Package fingerprint 错误、manifest 哈希错误和 v1.0/v1.1 版本不匹配。
- [x] 测试 staging 不可见、成功后原子发布、同指纹安全复用、并发上传和失败清理。
- [x] 测试相同幂等键收敛到一个 run，不同请求返回冲突。
- [x] 测试 Worker 只读取正式 Package，恢复时复用 work/checkpoint。
- [x] 测试 progress 原子替换、动态 message、sequence、无虚假百分比、损坏降级和旧 lease_owner 快照拒绝。
- [x] 测试 Worker 恢复后新 owner 接管进度，旧 Worker 晚写不能污染 status。
- [x] 测试 artifacts 未就绪、缺失、manifest 哈希错误、结果 SHA 错误和正常流式下载。
- [x] 测试 Compose 中 API/Worker 看到同一 `/exchange`，GraphAlchemy 容器不需要挂载该 volume。
- [x] 完成 GraphAlchemy -> HE 上传 -> Worker -> 动态 status -> result 下载的无真实模型 smoke，以及一个显式 opt-in 的小 Package 真实模型验收。

验证命令：

```bash
cd /Users/king/website/Hyper-Extract
OPENAI_API_KEY="" uv run pytest tests/service tests/docker -q
uv run ruff check hyperextract
uv run ruff format --check hyperextract
docker compose --env-file docker/.env.example \
  -f docker/service.compose.yml \
  -f docker/service.compose.dev.yml config --quiet
sh scripts/service-compose-smoke.sh
```

GraphAlchemy 验证：

```bash
cd /Users/king/website/graphalchemy/nitro
pnpm test
pnpm typecheck
pnpm build
```

---

## 4. 完成门槛

- [x] GraphAlchemy 与 HE 之间不再依赖共享文件夹、Docker external volume 或 `file://` 路径映射。
- [x] HE API 与 Worker 仍通过 Compose named volume 在同一服务器可靠协作。
- [x] HE create 只在验包、原子发布和数据库入队全部成功后返回 `202`。
- [x] API/Worker 重启后已接受任务、Package、checkpoint、进度和产物仍可恢复。
- [x] PostgreSQL 不承载高频 UI message/progress 写入，租约与任务状态正确性不受影响。
- [x] status 在一个 stage 内能持续返回多个 activity 和动态 message，且不暴露原始日志或敏感信息。
- [x] 旧 Worker、损坏快照和过期进度不能污染公共 status。
- [x] HE result 能可靠返回经过 manifest 与 SHA-256 验证的 `course-graph.json`。
- [x] GraphAlchemy 只有在结果成功下载并保存后才向最终调用方报告 `COMPLETED`。
- [x] HE 中英文 API 文档、Docker 文档和 GraphAlchemy 公共 API 文档与实现一致。
- [x] mock 测试、PostgreSQL 并发测试、Docker smoke、lint、format、GraphAlchemy test/typecheck/build 全部通过。

## 5. 非目标与后续演进

本计划明确不包含：

- S3/MinIO/OSS/COS 对象存储。
- HE API 与 Worker 跨服务器运行。
- 大文件分片续传和预签名上传。
- SSE、WebSocket 或 Webhook 实时推送。
- 原始模型日志、Prompt、诊断文件的公共下载。
- 将 HE PostgreSQL 改为 MySQL。

如果未来 HE API 与 Worker 跨主机，或 Package/产物体积与并发明显增长，应在不改变 Document Package v1.1 内容契约的前提下，将 `upload_root/package_root/artifact_root` 抽象为对象存储；本计划中的 multipart、指纹、package ref、status 和 result HTTP 契约应尽量保持兼容。

## 6. 2026-07-15 代码 Review 验证记录

- HE：`tests/service tests/docker` 共 `179 passed, 2 skipped`；跳过项是需要
  `HE_TEST_POSTGRES_URL` 的真实 PostgreSQL 用例。
- HE：Ruff lint、format check、Compose 静态展开和 `mkdocs build --strict` 通过。
- GraphAlchemy：`29` 项测试、TypeScript 类型检查和 Nitro 生产构建通过。
- 本轮 Review 未重新执行会启动容器的 Docker runtime smoke，也未调用真实模型；这些部署验收
  不能由 mock/静态检查替代，发布前应在隔离环境按 Task 11 的命令复核。
