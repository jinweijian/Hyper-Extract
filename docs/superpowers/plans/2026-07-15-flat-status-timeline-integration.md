# HE 与 GraphAlchemy 扁平状态时间线实施计划

> **For agentic workers:** 按任务顺序实施，优先编写失败测试，再完成最小实现。HE 的确定性测试必须显式设置 `OPENAI_API_KEY=""`，避免加载本地 `.env` 后调用真实模型。本文是跨仓库计划；每完成一项同时更新本文件复选框。

**状态：** 已实施（2026-07-15）
**优先级：** P0
**涉及仓库：**

- HE：`/Users/king/website/Hyper-Extract`
- GraphAlchemy：`/Users/king/website/graphalchemy`

**关联文档：**

- GraphAlchemy `nitro/docs/knowledge-graph-generator-api.md`
- GraphAlchemy `nitro/docs/external-knowledge-extraction-service.md`
- HE `docs/superpowers/plans/2026-07-15-service-package-upload-progress-result.md`

**目标：** 让 HE 和 GraphAlchemy 同时支持一条固定顺序、固定长度、可恢复、可直接用于前端 Timeline 的结构化状态历史。GraphAlchemy 公共 status 返回完整扁平 `timeline`，能明确区分已完成、当前执行、剩余、失败和跳过步骤；不返回原始日志，不使用嵌套 `activities`。同时先收口当前 result 契约差异：HE 保持流式返回结果文件，GraphAlchemy 负责接收、校验、解析和封装，并由 GraphAlchemy 公共 result 接口返回完整 JSON 内容。

**非目标：** 本计划不增加 SSE、WebSocket、Webhook、回调、原始日志下载、模型请求明细、Prompt 展示或无限增长的事件列表。

**结果交付边界：** 本计划不修改 HE `GET /v1/runs/{run_id}/result` 的文件流语义。HE 可以继续以 `application/json` 流式返回完整 `course-graph.json`；GraphAlchemy 不能把该内部文件流、`Content-Disposition` 或 HE 路径直接透传给公共调用方。GraphAlchemy 必须流式接收并校验文件，解析 JSON、完成公共 Schema 封装后，再由自己的 result 接口以普通 JSON 响应一次性返回全部结果。

---

## 1. 已确认的契约决策

### 1.1 公共时间线是扁平列表

GraphAlchemy 对外只返回一个 `timeline: TimelineStep[]`。Docling、Package、HE 和结果整理步骤全部同级排列，不存在 `GRAPH_GENERATE.activities` 或其他父子结构。

固定顺序：

| 顺序 | 公共 `stage` | 所有者 |
|---:|---|---|
| 1 | `DOCUMENT_PARSE` | GraphAlchemy / Docling |
| 2 | `PACKAGE_BUILD` | GraphAlchemy |
| 3 | `PACKAGE_SUBMIT` | GraphAlchemy / HE create |
| 4 | `DOCUMENT_INGESTING` | HE |
| 5 | `CHUNK_PLANNING` | HE |
| 6 | `EXTRACTING_CHUNK` | HE |
| 7 | `DEDUPLICATING` | HE |
| 8 | `BUILDING_GLOBAL_EDGES` | HE |
| 9 | `QUALITY_CHECKING` | HE |
| 10 | `BUILDING_COMMUNITIES` | HE |
| 11 | `FINALIZING` | HE |
| 12 | `ARTIFACT_PUBLISHING` | HE |
| 13 | `RESULT_PREPARE` | GraphAlchemy |
| 14 | `COMPLETE` | GraphAlchemy |

不得在响应中删除尚未开始或被跳过的阶段。前端依赖固定顺序判断“已处理哪些、当前处理到哪里、还剩哪些”。

### 1.2 TimelineStep 契约

```json
{
  "stage": "EXTRACTING_CHUNK",
  "label": "抽取知识点",
  "status": "PROCESSING",
  "message": "正在分析第 8/36 个内容块",
  "messageSeq": 31,
  "progress": {
    "current": 8,
    "total": 36
  },
  "startedAt": "2026-07-15T02:14:21Z",
  "completedAt": null,
  "attempt": 1
}
```

字段规则：

- `stage`：上述 14 个稳定枚举之一。
- `status`：`PENDING | PROCESSING | COMPLETED | FAILED | SKIPPED`。
- `message`：脱敏业务文案；不能包含 Prompt、模型响应、绝对路径或堆栈。
- `messageSeq`：当前阶段内单调递增；ticker 轮换文案可以增加它，但不能追加新 TimelineStep。
- `progress`：只有存在真实分子/分母时才返回；不能制造假百分比。
- `startedAt`：首次开始时间，恢复时不能覆盖。
- `completedAt`：完成、失败或跳过时写入。
- `attempt`：HE 阶段为正整数；GraphAlchemy 自身阶段为 `null`。

### 1.3 状态推进不变量

- 同一时刻最多一个步骤为 `PROCESSING`。
- 当前步骤之前只能是 `COMPLETED` 或 `SKIPPED`。
- 当前步骤之后只能是 `PENDING`，除非某个可选步骤已提前确定为 `SKIPPED`。
- 失败时当前步骤改为 `FAILED`，后续步骤保持 `PENDING`。
- 恢复执行时已完成步骤不能倒退；当前 HE 步骤的 `attempt` 递增。
- `status=COMPLETED` 时 `COMPLETE` 必须为 `COMPLETED`，不存在 `PROCESSING` 或 `FAILED` 步骤。
- 顶层 `stage/message/messageSeq/progress` 始终等于当前 `PROCESSING` 或 `FAILED` TimelineStep；完成时取 `COMPLETE`。

### 1.4 日志与时间线边界

HE 保留三类数据：

```text
/exchange/runs/<heRunId>/
  state/
    progress.json   # 当前高频快照
    timeline.json   # 固定长度的阶段摘要
  work/.he-run/
    events.jsonl    # 内部完整 Pipeline 审计事件
```

- `progress.json` 和 `timeline.json` 可用于 HE status。
- `.he-run/events.jsonl` 不能被 GraphAlchemy 直接读取，也不能原样通过 API 返回。
- timeline 更新失败只能影响展示，不能导致知识抽取任务失败。
- 原始事件明细如果未来需要开放，应增加分页运维接口，不纳入本计划。

---

## 2. 第一处理项：先修复审核发现的契约与架构缺口

后续 HE timeline 和 GraphAlchemy 聚合实现开始前，必须先完成本节。不能在这些边界仍未确定时直接实现 Task 1–12。

### Task 0：锁定 result、双状态文件、发布降级和兼容策略

**涉及文件：**

- HE：`hyperextract/service/worker.py`
- HE：`hyperextract/service/repository.py`
- HE：`hyperextract/service/api/schemas/responses.py`
- HE：`hyperextract/service/api/routes/runs.py`
- GraphAlchemy：`nitro/server/client/HeClient.ts`
- GraphAlchemy：`nitro/server/dto/KnowledgeGraphAiPipelineDto.ts`
- GraphAlchemy：`nitro/server/service/KnowledgeGraphAiPipelineService.ts`
- GraphAlchemy：`nitro/server/mapper/PipelineTaskMapper.ts`
- GraphAlchemy：`nitro/server/routes/api/v1/knowledge/graph/result.get.ts`
- GraphAlchemy：`nitro/docs/knowledge-graph-generator-api.md`

#### 0.1 先统一 result 交付契约

- [x] 保持 HE `GET /v1/runs/{run_id}/result` 的现有语义：校验成功产物后，以文件流返回完整 `course-graph.json`；不要求 HE 返回 GraphAlchemy 公共信封。
- [x] HE 新增脱敏的 `GET /v1/runs/{run_id}/result-metadata`：从已经验证并发布的 artifact manifest、`_SUCCESS`、run summary、performance report 和 quality report 组装 Profile、Brief、完成时间、性能和质量摘要；不返回输入/输出路径、模型用量、Prompt、日志或 Worker 信息。
- [x] GraphAlchemy 使用流式下载写入唯一 `.part` 文件，同时计算字节数和 SHA-256；校验 HE `ETag` 后原子发布到任务自己的 result 目录。
- [x] GraphAlchemy 获取 HE result metadata，并交叉校验 `run_id`、artifact 大小/哈希和 Profile 版本；必要 metadata 缺失或不一致时以 `RESULT_METADATA_UNAVAILABLE` 失败，不能发布部分结果。
- [x] GraphAlchemy 在 `RESULT_PREPARE` 阶段解析完整 JSON，校验 `HyperExtractCourseGraph` Schema、ID 引用和必要数组；解析或校验失败时不得把任务标记为 `COMPLETED`。
- [x] GraphAlchemy 生成独立的公共结果 JSON，至少包含 `runId/status/code/result`，其中 `result.graph` 是从 HE 文件流解析出的完整图谱对象，不能截断节点或边数组。
- [x] `result.metadata.subjectName/source` 来自 GraphAlchemy 任务记录；artifact 的 `sizeBytes/sha256/mediaType` 来自本次流式接收并与 HE metadata 交叉校验；Profile、Brief、完成时间、performance 和 quality 来自 HE 脱敏 metadata；statistics 根据转换后图谱数组计算。
- [x] `performance.resumed` 只有在 HE 能表达“实际发生过 checkpoint 恢复”时才返回；仅开启 resume 模式不能记为 `true`，语义未修正前应省略该字段。
- [x] GraphAlchemy 公共 `GET /api/v1/knowledge/graph/result?runId=...` 使用普通 `application/json` 响应返回完整公共信封；不设置 `Content-Disposition`，不使用 `sendStream()` 直接透传 HE 文件，不暴露本地路径或 HE URI。
- [x] GraphAlchemy status 完成态只返回 `result.url/contentType/schemaName/schemaVersion` 等查询元数据，不在轮询响应内嵌图谱正文。
- [x] 为 HE 流式结果接收、ETag 不一致、JSON 损坏、Schema/引用错误、公共 result 完整返回和大数组不截断编写测试。

#### 0.2 消除 progress 与 timeline 双写漂移

- [x] `progress.json` 继续承担高频 ticker 文案和数量快照；`timeline.json` 只在阶段开始、完成、失败、跳过和恢复时更新。
- [x] ticker 不直接重写 `timeline.json`。HE status 读取时将当前合法 `progress.json` 覆盖到 timeline 当前步骤的 `message/message_seq/progress`，然后生成公共响应。
- [x] 顶层 `activity/message/message_seq/progress` 与合并后的当前 TimelineStep 必须来自同一次合并结果，禁止分别读取后产生漂移。
- [x] 如果未来选择合并文件替代双文件，则必须用一个原子状态信封同时承载阶段摘要和当前快照；不能保留两个独立高频写入源。

#### 0.3 补齐 Artifact 发布的数据库降级事实

- [x] 调用 `ArtifactPublisher.publish()` 前，在当前 lease 所有权校验下将数据库 stage 更新为 `publish`，并将 `ARTIFACT_PUBLISHING` timeline 步骤置为 running。
- [x] 发布成功、manifest/哈希校验完成后先收口 timeline，再将数据库 run 标记为 completed。
- [x] 发布失败时数据库错误、timeline failed 和公共错误码必须指向同一发布阶段。
- [x] timeline 缺失或损坏时，HE status 必须能仅根据数据库 `stage=publish` 安全降级为 `ARTIFACT_PUBLISHING`，不能错误停留在 `FINALIZING`。

#### 0.4 定义版本协商和灰度开关

- [x] HE timeline 信封及公共响应增加明确的 `timeline_schema_version`，并在 capabilities 或契约发现接口声明支持的 timeline 版本。
- [x] GraphAlchemy 增加类似 `HE_REQUIRE_TIMELINE=false` 的灰度配置：关闭时兼容旧 HE 顶层进度；开启时缺失或非法 timeline 视为稳定的内部协议错误。
- [x] 明确发布顺序：先发布兼容客户端，再发布 HE timeline，观察完成后才开启严格校验；必须有可回滚开关，不能只依赖人工约定。

#### 0.5 固定旧任务迁移和未知 activity 降级

- [x] 旧 GraphAlchemy phase 固定映射为：`DOCUMENT_PARSE -> DOCUMENT_PARSE`、`DOCUMENT_CHUNK -> PACKAGE_BUILD`、`HE_UPLOAD -> PACKAGE_SUBMIT`、`RESULT_FETCH -> RESULT_PREPARE`、`COMPLETE -> COMPLETE`。
- [x] 旧 `HE_EXTRACT` 任务优先用 HE 顶层 `activity` 推断当前 HE 步骤；无法识别时保持最后一个合法 HE 步骤为 `PROCESSING`，不能生成没有当前项的运行中 timeline。
- [x] 新版 HE 出现未知 activity 时记录安全 warning，保留最后一个合法 `PROCESSING` 步骤及 heartbeat，不把未知项插入固定列表，也不能把运行中任务误判为失败或全部 pending。
- [x] 每个降级生成的时间字段不存在时返回 `null`，不能用查询时间伪造历史开始或完成时间。

#### 0.6 建立单一跨仓库契约来源

- [x] HE 负责提供规范的 timeline JSON Schema 和 running/completed/failed/recovered fixture。
- [x] GraphAlchemy 测试消费从 HE 同步的同一版本 fixture，并用 Schema 版本或校验和检测复制漂移；不能由两个仓库分别手工维护语义相似但无关联的样例。
- [x] 将 result 流、HE 脱敏 result-metadata 与 GraphAlchemy 公共 JSON 信封分别建立 fixture，明确三者不是同一个契约。

**Task 0 完成标准：** result 内外边界、timeline 合并算法、Artifact 发布降级、版本协商、旧任务映射和共享 Schema 均有失败测试及明确契约；后续任务不再依赖未定义行为。

---

## 3. HE 实施任务

### Task 1：定义 HE timeline 模型与固定阶段计划

**文件：**

- 新增：`hyperextract/service/timeline.py`
- 修改：`hyperextract/service/progress.py`
- 修改：`hyperextract/service/storage.py`
- 测试：`tests/service/test_progress.py`
- 测试：新增 `tests/service/test_timeline.py`

**步骤：**

- [x] 先为固定阶段顺序、默认 `pending` 状态、状态迁移和序列化编写失败测试。
- [x] 定义 `HE_TIMELINE_ACTIVITIES`：
  `DOCUMENT_INGESTING`、`CHUNK_PLANNING`、`EXTRACTING_CHUNK`、`DEDUPLICATING`、`BUILDING_GLOBAL_EDGES`、`QUALITY_CHECKING`、`BUILDING_COMMUNITIES`、`FINALIZING`、`ARTIFACT_PUBLISHING`。
- [x] 定义内部 `TimelineState` envelope，至少包含 `schema_version`、`run_id`、`worker_id`、`attempt`、`sequence` 和固定 `steps`。
- [x] 定义 `TimelineStep`，字段与 HE snake_case API 对应：`activity/status/message/message_seq/progress/started_at/completed_at/attempt`。
- [x] 实现纯函数 reducer：接收已存在 timeline 和一个安全 Pipeline 事件，返回新 timeline。
- [x] 实现原子 `write_timeline()`：唯一临时文件、`flush`、`fsync`、`os.replace`。
- [x] 实现容错 `read_timeline()`：文件不存在、空文件、半写 JSON、未知版本时返回 `None`。
- [x] 在 `SharedVolumeStore` 增加 `timeline_path(run_id)`。
- [x] 确认 timeline 长度始终为 9，不因 heartbeat 或 chunk 数增长。

**完成标准：** reducer 和文件读写单元测试通过；同一 activity 不会产生重复步骤。

### Task 2：补齐 HE Pipeline 的真实阶段事件

**文件：**

- 修改：`hyperextract/documents/course_pipeline.py`
- 测试：`tests/documents/test_course_pipeline.py`
- 测试：`tests/service/test_pipeline_control.py`

**步骤：**

- [x] 为每个当前真实阶段补齐一致的 `started` 和 `completed` 事件：`ingest`、`chunk_plan`、`local_extract`、`deduplicate`、`global_edges`、`quality`、`communities`、`finalize`。
- [x] 阶段进入时先 emit `started`，长任务中 emit `progress`，成功后 emit `completed`。
- [x] 阶段异常时保证 Worker 能得知最后一个活动，不能在 status 中停留在上一个已完成阶段。
- [x] 不新增当前 Pipeline 实际不存在的 `context_planning`、`validate`、`merge` 等公共步骤。
- [x] 保留 chunk 级事件用于 `EXTRACTING_CHUNK.progress`，但不为每个 chunk 创建 TimelineStep。
- [x] 测试完整成功事件顺序、失败事件位置和 resume 时已完成阶段恢复。

**完成标准：** Pipeline 事件顺序与 HE 9 阶段计划一致，且每个长阶段开始时 status 已能切换到正确步骤。

### Task 3：在 Runner 中维护阶段 timeline 与高频 progress

**文件：**

- 修改：`hyperextract/service/runner.py`
- 修改：`hyperextract/service/progress.py`
- 修改：`hyperextract/service/timeline.py`
- 测试：`tests/service/test_runner.py`

**步骤：**

- [x] 在 `event_sink` 中继续写 `progress.json`；只有 started/completed/failed/skipped/recovery 等阶段迁移事件才调用 reducer 更新 `timeline.json`。
- [x] stage/activity 映射固定为：
  `ingest -> DOCUMENT_INGESTING`、`chunk_plan -> CHUNK_PLANNING`、`local_extract -> EXTRACTING_CHUNK`、`deduplicate -> DEDUPLICATING`、`global_edges -> BUILDING_GLOBAL_EDGES`、`quality -> QUALITY_CHECKING`、`communities -> BUILDING_COMMUNITIES`、`finalize -> FINALIZING`。
- [x] ticker 只更新 `progress.json` 的 `message/message_seq/progress`，不写 `timeline.json`、不创建新步骤、不提前完成步骤。
- [x] `event_sink` 与 ticker 对当前 progress 状态使用同一个进程内锁；timeline reducer 只处理低频阶段迁移，避免形成两个高频状态源。
- [x] 文件写失败只记录安全 warning，不抛出到 Pipeline。
- [x] 新任务初始化 9 个 `pending` HE 步骤；Worker 领取后首个步骤进入 `running`。

**完成标准：** Runner 测试能够观察 timeline 从 ingest 推进到 finalize；ticker 多次触发只改变 progress 快照，timeline 步骤数量和生命周期状态保持稳定。

### Task 4：支持 Artifact 发布、失败和 Worker 恢复

**文件：**

- 修改：`hyperextract/service/worker.py`
- 修改：`hyperextract/service/artifacts.py`（仅在需要安全回调时）
- 修改：`hyperextract/service/timeline.py`
- 测试：`tests/service/test_worker.py`
- 测试：`tests/service/test_artifacts.py`

**步骤：**

- [x] 在调用 `ArtifactPublisher.publish()` 前，在当前 lease 校验下先把数据库 stage 改为 `publish`，再把 `ARTIFACT_PUBLISHING` 改为 `running`。
- [x] 原子发布和 manifest 校验完成后改为 `completed`，然后才完成数据库 run。
- [x] 发布失败时将 `ARTIFACT_PUBLISHING` 标记为 `failed`，后续由现有错误分类返回 `ARTIFACT_STATE_INCONSISTENT`。
- [x] Worker 恢复时读取既有 timeline，保留 `completed` 步骤，当前步骤 `attempt` 递增。
- [x] 使用数据库当前 lease owner 防止旧 Worker 的迟到写入覆盖新 attempt。
- [x] 已发布产物恢复路径直接将必要步骤和 `ARTIFACT_PUBLISHING` 收口为 completed，不重新运行模型。
- [x] 取消、恢复耗尽和租约丢失分别覆盖测试。

**完成标准：** 崩溃恢复后时间线单调推进，旧 Worker 无法污染当前展示。

### Task 5：HE status API 返回 timeline

**文件：**

- 修改：`hyperextract/service/api/schemas/responses.py`
- 修改：`hyperextract/service/api/routes/runs.py`
- 测试：`tests/service/test_runs_api.py`
- 测试：`tests/service/test_api_structure.py`

**步骤：**

- [x] 新增 `TimelineStepResponse` 和 `RunResponse.timeline`。
- [x] HE status 中 timeline 使用 snake_case，状态为 `pending/running/completed/failed/skipped`。
- [x] queued 任务返回 9 个 pending 步骤；running 返回当前 timeline；completed/failed/cancelled 仍保留历史 timeline。
- [x] running 状态读取文件时验证 `run_id/worker_id/attempt` 与当前 lease 一致。
- [x] terminal 状态 lease 已释放时，验证 `run_id` 和最终 attempt，不因没有 lease 丢弃完成历史。
- [x] timeline 文件缺失或损坏时返回安全的固定计划，并用数据库 stage 推断当前项；不能返回 500。
- [x] 读取合法 `progress.json` 后，只覆盖 timeline 当前项的动态文案和数量；顶层进度与当前项必须从同一合并结果派生。
- [x] 响应携带 `timeline_schema_version`，契约发现或 capabilities 同步声明支持版本。
- [x] 公共响应删除内部 `worker_id`、绝对路径、details 和原始日志。
- [x] 测试 queued、running、completed、failed、cancelled、恢复、旧 owner、损坏文件和未知 timeline 版本。

**完成标准：** `GET /v1/runs/{run_id}` 总能返回固定 9 项 timeline，且 API Schema 禁止意外字段。

---

## 4. GraphAlchemy / Nitro 实施任务

### Task 6：定义公共 14 阶段类型与兼容初始化

**文件：**

- 修改：`nitro/server/dto/KnowledgeGraphAiPipelineDto.ts`
- 新增或修改：`nitro/server/support/PipelineTimeline.ts`
- 修改：`nitro/server/support/PipelineTaskFileStore.ts`
- 测试：新增 `nitro/test/PipelineTimeline.test.ts`
- 测试：`nitro/test/PipelineTaskMapper.test.ts`

**步骤：**

- [x] 定义公共 `PipelineStage` 14 项枚举和唯一固定顺序数组。
- [x] 定义 `TimelineStep` 类型，不包含 `activities`。
- [x] `PipelineTask` 持久化完整 timeline；新任务初始化所有步骤。
- [x] 实现纯函数：`startStage`、`updateStage`、`completeStage`、`failStage`、`skipStage`、`currentStage`。
- [x] 每次更新校验最多一个 `PROCESSING`，禁止已完成阶段倒退。
- [x] 读取旧 `task.json` 时若没有 timeline，严格按照 Task 0 定义的 phase 映射生成兼容时间线，并在下次写入时升级。
- [x] 未知新字段要容忍，未知 stage 不得插入公共固定列表。

**完成标准：** timeline reducer 单元测试覆盖正常推进、失败、恢复、跳过、重复事件和旧任务兼容。

### Task 7：扩展 HeClient timeline 契约

**文件：**

- 修改：`nitro/server/client/HeClient.ts`
- 测试：`nitro/test/HeClient.test.ts`

**步骤：**

- [x] 在 `HeRunStatus` 增加 HE timeline 类型和 `timeline_schema_version`。
- [x] 将 HE `activity` 映射到同名公共 stage；将 lowercase 状态映射为公共 uppercase 状态。
- [x] 将 `message_seq/started_at/completed_at/attempt` 转换为公共命名。
- [x] 对 HE timeline 缺失提供兼容：继续使用顶层 `activity/message/progress` 更新对应单个公共阶段。
- [x] 对 HE 新增未知 activity 容错：记录安全 warning，不把未知项插入固定列表；保留最后一个合法 `PROCESSING` 步骤和 heartbeat，不能出现运行中却没有当前步骤的 timeline。
- [x] 用 `HE_REQUIRE_TIMELINE` 灰度配置区分旧 HE 兼容模式和新版严格校验模式。
- [x] 拒绝重复 activity、非法状态、负 progress 或多个 running 项，返回稳定内部协议错误。
- [x] 测试完整、缺失、未知、重复、恢复 attempt 和 terminal timeline。

**完成标准：** HeClient 能稳定解析新版 HE，也能在滚动发布期间兼容旧版 HE。

### Task 8：Nitro 编排过程维护自身阶段

**文件：**

- 修改：`nitro/server/service/KnowledgeGraphAiPipelineService.ts`
- 修改：`nitro/server/support/PipelineTaskFileStore.ts`
- 测试：`nitro/test/KnowledgeGraphAiPipelineService.test.ts`

**步骤：**

- [x] create 接受任务时开始 `DOCUMENT_PARSE`。
- [x] Docling 成功后完成 `DOCUMENT_PARSE` 并开始 `PACKAGE_BUILD`。
- [x] Package 指纹和归档生成后完成 `PACKAGE_BUILD` 并开始 `PACKAGE_SUBMIT`。
- [x] HE 返回 `202` 后完成 `PACKAGE_SUBMIT`；等待/运行 HE 时合并 HE timeline。
- [x] HE 完成后开始 `RESULT_PREPARE`；流式下载、ETag/SHA-256、JSON Schema 和引用校验通过后，解析图谱并生成独立公共结果 JSON，完成后才推进阶段。
- [x] 最终将 `COMPLETE` 标记为 completed，再把公共任务置为 `COMPLETED`。
- [x] 任一异常将当前具体步骤标记为 failed，后续保持 pending。
- [x] HE 恢复时合并 attempt，不让旧轮询响应覆盖更新的 messageSeq。
- [x] 每个任务的文件写入继续串行化；使用唯一临时文件和原子 rename。

**完成标准：** 服务测试能观察从创建到结果完成的全部 14 项状态变化，失败阶段准确。

### Task 9：公共 status DTO 与路由返回扁平 timeline

**文件：**

- 修改：`nitro/server/mapper/PipelineTaskMapper.ts`
- 修改：`nitro/server/dto/KnowledgeGraphAiPipelineDto.ts`
- 修改：`nitro/server/routes/api/v1/knowledge/graph/status.get.ts`
- 测试：`nitro/test/PipelineTaskMapper.test.ts`
- 新增或修改：status 路由测试

**步骤：**

- [x] DTO 必返 `timeline`，不得返回 `activities`、`heRunId`、内部 phase 或路径。
- [x] 顶层 `stage/message/messageSeq/progress` 从 timeline 当前项派生，避免两套状态漂移。
- [x] `PROCESSING`、`COMPLETED`、`FAILED` 三态保持现有契约。
- [x] completed status 保留完整 timeline，并返回 GraphAlchemy 公共 result 查询 URL；该 URL 返回完整 JSON 信封，不是 HE 文件流或下载附件。
- [x] failed status 保留已完成历史、failed 当前项和 pending 后续项。
- [x] `Cache-Control: no-store` 保持不变。
- [x] 测试固定顺序、固定 14 项、无嵌套、字段脱敏和 JSON 示例一致性。

**完成标准：** 前端只依赖一个 status 响应即可绘制完整 Timeline。

---

## 5. 跨项目契约与端到端验证

### Task 10：建立单一来源的共享契约 Fixture

**文件：**

- 新增：HE `tests/service/fixtures/run-status-timeline.json`（或现有 fixture 目录）
- 新增：GraphAlchemy `nitro/test/fixtures/he-run-status-timeline.json`
- 修改：双方 timeline 相关测试

**步骤：**

- [x] 由 HE 固定一份规范 running status JSON 和 timeline JSON Schema，包含 9 个 HE timeline 步骤。
- [x] GraphAlchemy 同步消费同一版本 fixture，使用 Schema 版本或校验和阻止两份副本静默漂移，并验证生成 14 个公共步骤。
- [x] 固定 completed、failed、recovered 三组 fixture。
- [x] 增加契约检查，防止 HE 改名或删字段后 GraphAlchemy 测试仍然通过。
- [x] Fixture 不包含真实密钥、模型响应、本机路径或大段正文。

**完成标准：** 两个仓库对 timeline 字段、状态枚举和顺序形成自动化契约保护。

### Task 11：端到端场景验证

**场景：**

- [x] 正常任务：轮询期间每个阶段单调推进，最终 COMPLETE。
- [x] 长内容块：ticker 只更新 EXTRACTING_CHUNK 文案和 progress，timeline 长度不变。
- [x] Worker 恢复：attempt 增加，已完成步骤不倒退。
- [x] HE 阶段失败：GraphAlchemy 对应公共 stage 为 FAILED，后续 PENDING。
- [x] Artifact 发布失败：ARTIFACT_PUBLISHING 为 FAILED，公共任务失败。
- [x] 结果下载/校验失败：HE 阶段已完成，RESULT_PREPARE 为 FAILED。
- [x] 旧 HE 无 timeline：Nitro 通过顶层 activity 兼容，不崩溃。
- [x] 旧 GraphAlchemy task 无 timeline：读取后能生成兼容时间线。
- [x] timeline.json 损坏：HE status 降级但任务继续执行。
- [x] 未知 HE activity：Nitro 忽略并保留稳定公共结构。
- [x] HE result 文件流：Nitro 流式接收、校验、解析并原子保存公共结果；GraphAlchemy result 返回完整 JSON 信封且不带附件头。
- [x] result 元数据来源：每个公开 metadata 字段都有实际权威来源；缺失数据不会被伪造为成功结果。

**建议命令：**

```bash
cd /Users/king/website/Hyper-Extract
OPENAI_API_KEY="" uv run pytest \
  tests/service/test_progress.py \
  tests/service/test_timeline.py \
  tests/service/test_runner.py \
  tests/service/test_worker.py \
  tests/service/test_runs_api.py
uv run ruff check hyperextract
uv run ruff format --check hyperextract

cd /Users/king/website/graphalchemy/nitro
pnpm test
pnpm typecheck
pnpm build
```

需要真实 PostgreSQL 的租约/旧 Worker 防污染测试必须配置隔离的 `HE_TEST_POSTGRES_URL`；不能因为本地缺少数据库就把该验收项视为已通过。

---

## 6. 文档与发布顺序

### Task 12：同步文档与滚动发布

**文件：**

- GraphAlchemy `nitro/docs/knowledge-graph-generator-api.md`
- GraphAlchemy `nitro/docs/external-knowledge-extraction-service.md`
- HE `docs/zh/guides/internal-service.md`
- HE 对应英文文档和 `mkdocs.yml`（如新增页面）

**步骤：**

- [x] HE 文档补充 `RunResponse.timeline`、字段表、状态迁移、恢复语义和示例。
- [x] GraphAlchemy 文档保持 14 阶段扁平列表，不出现 `activities`。
- [x] 明确原始日志与公共 timeline 的边界。
- [x] 明确 status payload 固定有界，不会随 chunk 数无限增长。
- [x] 先以 `HE_REQUIRE_TIMELINE=false` 发布兼容旧 HE 的 GraphAlchemy，再发布带 `timeline_schema_version` 的 HE；观察兼容期后才开启严格校验，并验证关闭开关可回滚。
- [x] HE 文档执行 `uv run mkdocs build --strict`。
- [x] 双方执行 `git diff --check`。

---

## 7. 完成门槛

以下条件全部满足后，计划才能标记为“已实施”：

- [x] Task 0 的 result 边界、状态合并、发布降级、版本协商、旧任务映射和共享 Schema 均已通过测试。
- [x] HE result 接口保持完整 JSON 文件流交付，GraphAlchemy 能流式接收并校验。
- [x] HE result-metadata 只返回脱敏摘要；GraphAlchemy 对 runId、artifact 哈希/大小和 Profile 版本完成交叉校验。
- [x] GraphAlchemy 公共 result 接口返回完整 JSON 信封，不返回附件文件、不透传 HE 流或内部路径。
- [x] HE status 在所有生命周期状态下返回固定 9 项 timeline。
- [x] HE timeline 原子持久化、可恢复，并能抵御旧 Worker 迟到写入。
- [x] Artifact 发布阶段同时进入数据库 stage 和 HE timeline，timeline 损坏时仍能正确降级展示。
- [x] HE 暴露 `timeline_schema_version`，GraphAlchemy 的兼容/严格模式均有自动化测试。
- [x] GraphAlchemy status 固定返回 14 项扁平 timeline。
- [x] GraphAlchemy 公共 DTO 不包含 `activities` 或 HE 内部字段。
- [x] 顶层状态与 timeline 当前项完全一致。
- [x] 正常、失败、恢复、取消、旧版本兼容和损坏降级测试全部通过。
- [x] HE PostgreSQL 租约并发测试实际运行通过，而非跳过。
- [x] HE lint、format、确定性测试、MkDocs strict build 通过。
- [x] GraphAlchemy test、typecheck、build 通过。
- [x] 两个仓库 `git diff --check` 通过。
