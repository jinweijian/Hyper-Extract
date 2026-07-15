# HE 模型供应商适配与结构化输出韧性实施计划

**状态：** 待实施  
**优先级：** P0  
**目标版本：** 后续小版本分阶段交付  
**关联计划：** `2026-07-13-p0-2-model-compatibility-context.md`、`2026-07-13-p1-2-course-pipeline-performance.md`

## 1. 背景

Hyper-Extract 当前已经具备以下基础能力：

- 通过 `create_client()` 和 `ModelProfileRegistry` 创建不同供应商的 LLM 与 Embedding 客户端。
- 通过 `StructuredOutputInvoker` 支持 `native`、`tool`、`json_object`、`text_json` 和 `auto` 等结构化输出方式。
- 能处理 `<think>`、Markdown JSON fence、正文前后说明、输出截断和部分供应商错误。
- 课程文档管线支持重试、心跳、分块、断点恢复和模型用量记录。

但是，现有实现仍然把以下不同性质的问题混在一起处理：

1. 供应商通信协议差异，例如 OpenAI Chat、Anthropic 原生接口和 OpenAI 兼容中转接口。
2. 模型能力差异，例如是否支持 JSON Schema、Tool Calling、Thinking、温度参数和特定 token 参数。
3. 响应载荷差异，例如最终文本位于 `content`、内容块数组、`reasoning_content` 或 XML 标签之后。
4. 输出质量差异，例如一个列表中只有一条关系缺少 `target`，其余关系均有效。
5. 运行恢复差异，例如限流应退避重试、截断应缩小批次、坏项应隔离，而鉴权错误应立即失败。

当前通过不断补充分支和把某类错误整体设为 `retryable` 的做法只能临时止血。上线接入更多模型后，这种方式会带来以下风险：

- 新模型需要修改业务管线代码，形成大量 `if provider/model` 判断。
- 不支持的参数可能运行数小时后才暴露。
- 系统性 Schema 不兼容被误判为偶发错误，产生大量无效重试和费用。
- 单条坏数据可能终止整本书，或者被静默丢弃而无法审计。
- CLI、Service 和 Python API 可能各自形成不同的模型配置与行为。

因此，本计划将 P0-2 中的模型兼容能力升级为一套供应商无关、能力可声明、行为可探测、失败可恢复且结果可审计的通用模型执行层。

## 2. 目标与非目标

### 2.1 目标

- HE 业务管线只依赖统一模型请求和响应契约，不直接依赖厂商参数。
- LLM 与 Embeddings 使用各自的请求、响应和能力契约，但共用 Profile、Gateway 生命周期、错误归一化、调度和可观测性边界。
- 常见 OpenAI 兼容模型只需新增或调整 Model Profile，不修改抽取代码。
- 模型能力和参数不匹配在任务启动前被发现。
- 单个无效列表项不会直接终止整批或整本书，也不会被静默吞掉。
- 每类错误都有确定的恢复动作、作用范围和最大尝试次数。
- CLI、Service 和 Python API 共用同一套 Profile、Adapter、Probe 和恢复策略。
- checkpoint 能准确区分模型、能力配置、提示词、Schema 和恢复策略版本。
- OpenAI、MiniMax、硅基流动三条真实路由通过统一契约验证。

### 2.2 非目标

- 本计划不优化课程知识点定义、关系语义和抽取 Profile 的业务质量。
- 本计划不提供任意供应商之间的自动模型降级；跨模型切换必须由调用方明确配置。
- 本计划不通过猜测补全缺失的知识点名称、关系端点或证据。
- 本计划不把 API Key 写入 Profile、运行快照或日志。
- 本计划不在第一阶段替换 LangChain 全部基础设施，只在其外部建立稳定边界。

## 3. 设计原则

1. **能力优先于厂商名称。** 运行行为由能力声明决定，不能只根据 `provider == minimax` 判断。
2. **显式映射优先于透传。** 只发送 Profile 允许的参数，未知参数在启动前拒绝。
3. **配置与探测结合。** 静态 Profile 是事实基线，Probe 用于验证实际端点，不在生产请求中反复试错。
4. **不伪造知识事实。** 缺失关系端点时只能重新请求或隔离，不能从相似名称推断。
5. **部分成功必须可审计。** 保留合法项，坏项进入 rejection artifact，并记录原因和来源请求。
6. **恢复动作必须有边界。** 区分 item、batch、chunk 和 run，避免一个局部错误扩大为全局重跑。
7. **运行身份与可用性证据分离。** Profile、最终生效能力、Schema、Prompt 和策略版本进入确定性任务指纹；Probe 时间和 TTL 只决定启动资格，不改变运行身份。
8. **兼容现有公共 API。** `create_client()`、`he parse` 和 Service 请求保持可用，逐步委托给新执行层。
9. **生成与向量化契约分离。** 两者可以共用调度和失败模型，但不能用同一 Adapter 接口掩盖完全不同的批处理、位置对齐和部分成功语义。

## 4. 目标架构

```text
Extraction Method / Document Pipeline
              |
              v
 Provider-neutral GenerationRequest / EmbeddingRequest
              |
              v
       ModelExecutionGateway
       |        |         |
       |        |         +-- RecoveryPolicy
       |        +------------ CapabilityRegistry / ProbeResult
       +--------------------- ProviderAdapter
                                  |
                                  v
                           Remote model API
                                  |
                                  v
                         ResponseNormalizer
                                  |
                                  v
                 Schema Validation / Item Quarantine
                                  |
                                  v
                         ModelResult / Artifacts
```

建议新增模块：

```text
hyperextract/providers/
  contracts.py              统一请求、响应、能力和恢复决策模型
  profiles.py               Model Profile Schema、加载、校验与指纹
  registry.py               Profile 与 Adapter 注册表
  gateway.py                统一执行入口
  normalization.py          响应文本和结构归一化
  recovery.py               标准失败到恢复决策的确定性矩阵
  probe.py                  能力探测与结果缓存
  adapters/
    base.py                 Adapter 协议
    openai_chat.py          OpenAI Chat 及兼容接口
    openai_embeddings.py    OpenAI Embeddings 及兼容接口
    anthropic.py            Anthropic 原生接口

hyperextract/cli/commands/
  model.py                  `he model validate/probe/show`
```

现有模块的演进方式：

- `hyperextract/utils/client.py` 保留公共工厂职责，内部委托给 Provider Registry；`CompatibleEmbeddings` 中的分词、batch、空输入和位置对齐逻辑逐步迁入 Embedding Adapter。
- `hyperextract/service/model_profiles.py` 改为使用公共 Profile Schema，不再单独定义一套能力字段。
- `hyperextract/documents/structured_output.py` 只负责 Schema 输出编排，不再自行判断所有厂商行为。
- `hyperextract/documents/model_errors.py` 保留稳定错误类型和兼容导出；供应商异常分类下沉到 Adapter，恢复动作移到 `recovery.py`。
- `hyperextract/documents/course_pipeline.py` 只消费恢复决策，不关心供应商字段。

## 5. 核心契约

### 5.1 生成请求

```python
class GenerationRequest(BaseModel):
    operation: str
    messages: list[ModelMessage]
    output_schema: dict | None = None
    structured_output: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: int | None = None
    request_id: str
    metadata: dict[str, str] = Field(default_factory=dict)
```

业务层只使用规范字段，例如 `max_output_tokens`。Adapter 根据 Profile 将其转换为 `max_tokens`、`max_completion_tokens` 或供应商专用字段。

### 5.2 生成响应

```python
class GenerationResponse(BaseModel):
    request_id: str
    final_text: str
    reasoning_text: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None
    raw_response_ref: str | None = None
```

原始响应只能以脱敏 artifact 形式保存。业务管线不能直接读取供应商响应对象。

### 5.3 Embeddings 请求、响应与位置对齐

```python
class EmbeddingRequest(BaseModel):
    inputs: list[str]
    dimensions: int | None = None
    request_id: str
    metadata: dict[str, str] = Field(default_factory=dict)


class EmbeddingItemResult(BaseModel):
    input_index: int
    vector: list[float] | None = None
    status: Literal["completed", "quarantined"]
    error_reason: str | None = None


class EmbeddingResponse(BaseModel):
    request_id: str
    items: list[EmbeddingItemResult]
    input_tokens: int | None = None
    provider_request_id: str | None = None
```

Embedding Adapter 必须保证 `input_index` 与原输入稳定对应。响应数量、顺序或向量维度异常属于协议错误，不能静默错位。整批失败时可按策略二分 batch；定位到单个坏项后才允许 quarantine。

现有 `CompatibleEmbeddings` 的空字符串 zero-vector 回填只能作为显式兼容策略保留。默认不得静默生成零向量；使用 `zero_vector` 时必须写入 validation warning 和 artifact。

### 5.4 能力声明

```python
class ModelCapabilities(BaseModel):
    transport: Literal["openai_chat", "anthropic_messages"]
    structured_output_modes: list[OutputMode]
    preferred_structured_output_mode: OutputMode
    reasoning_content_mode: Literal[
        "none", "inline_tags", "separate_field", "content_blocks"
    ]
    output_token_parameter: str
    supported_parameters: set[str]
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    recommended_concurrency: int = 1


class EmbeddingCapabilities(BaseModel):
    transport: Literal["openai_embeddings"]
    accepts_token_ids: bool = False
    max_batch_items: int | None = None
    max_batch_tokens: int | None = None
    max_input_tokens_per_item: int | None = None
    supports_dimensions: bool = False
    empty_input_policy: Literal["reject", "quarantine", "zero_vector"] = "reject"
```

`max_batch_items`、token 上限和输入形式由 Profile/Probe 声明，Adapter 据此完成本地分词、切分和 batch 规划，业务层不再直接依赖 `CompatibleEmbeddings` 的供应商经验值。

### 5.5 恢复决策

```python
class RecoveryDecision(BaseModel):
    action: Literal[
        "retry",
        "fallback",
        "repair",
        "split",
        "replan",
        "quarantine",
        "fail",
        "circuit_break",
    ]
    target: Literal[
        "item", "batch", "chunk", "request", "run", "rate_limit_group"
    ]
    reason: str
    delay_seconds: float = 0
    consume_attempt: bool = True
```

不得再使用单一 `retryable: bool` 表达全部运行策略。

`action × target` 必须在 Schema 或运行时执行穷举校验，不允许形成任意笛卡尔积：

| action | 合法 target |
|---|---|
| `retry` | `request` |
| `fallback` | `request` |
| `repair` | `item`, `batch` |
| `split` | `batch`, `chunk` |
| `replan` | `chunk` |
| `quarantine` | `item`, `batch`, `chunk` |
| `fail` | `request`, `batch`, `chunk`, `run` |
| `circuit_break` | `rate_limit_group` |

实现时优先使用带 discriminator 的联合决策模型；如果保留单一模型，必须用 model validator 拒绝无意义组合，例如 `split × item` 或 `circuit_break × item`。

## 6. Model Profile 扩展

继续使用现有 TOML Profile，并增加能力、参数映射、限制和恢复策略。密钥只引用环境变量。

```toml
[profiles.minimax-m27]
transport = "openai_chat"
llm = "vllm:MiniMax-M2.7-highspeed@https://api.minimaxi.com/v1"
llm_api_key_env = "MINIMAX_API_KEY"
embedder = "vllm:Qwen/Qwen3-Embedding-8B@https://api.siliconflow.cn/v1"
embedder_api_key_env = "EMBEDDING_API_KEY"
llm_rate_limit_group = "minimax-production-account"
embedder_rate_limit_group = "siliconflow-embedding-account"

[profiles.minimax-m27.capabilities]
structured_output_modes = ["text_json"]
preferred_structured_output_mode = "text_json"
reasoning_content_mode = "separate_field"
output_token_parameter = "max_tokens"
supported_parameters = ["max_output_tokens", "timeout_seconds"]
context_tokens = 65536
max_output_tokens = 8192
recommended_concurrency = 4
structured_output_fallback_order = ["text_json"]

[profiles.minimax-m27.embedding_capabilities]
transport = "openai_embeddings"
accepts_token_ids = false
max_batch_items = 10
max_input_tokens_per_item = 8191
supports_dimensions = false
empty_input_policy = "quarantine"

[profiles.minimax-m27.recovery]
validation_repair_attempts = 1
validation_retry_attempts = 3
transient_retry_attempts = 4
invalid_list_item_policy = "quarantine"
invalid_item_ratio_threshold = 0.2
```

兼容要求：

- 现有简单 Profile 仍可加载，缺失能力字段时使用保守默认值并输出 deprecation warning。
- 现有 `HYPER_EXTRACT_LLM_PROFILE` 环境变量继续支持。
- `MIMIMAX_*` 等历史变量仅保留一个小版本的弃用兼容；新 Profile、文档和示例统一使用正确拼写，并按 6.1 节完成迁移。
- Profile 公共描述和指纹必须包含能力与恢复策略，但不能包含 Key。
- `llm_rate_limit_group` / `embedder_rate_limit_group` 表示共享供应商账户或端点配额的调度边界。多个 Profile 使用同一额度时必须配置同一 group，不能假设 Profile 名称不同就拥有独立配额。
- LLM 与 Embeddings 可以引用不同端点、密钥环境变量和 rate-limit group；二者的能力字段必须独立校验。group 名称是非敏感部署标识，不能由 API Key 派生后写入日志或指纹。

### 6.1 默认 OpenAI 兼容路由与 MiniMax 配置迁移

当前根目录 `.env.example`、Service 内置 Profile、请求 Schema 和 Worker 回退逻辑把一次 MiniMax 课程抽取基线固化成了产品默认配置。MiniMax 实际通过 OpenAI Chat 兼容接口调用，通用客户端并不需要识别 MiniMax 厂商名称，因此默认配置应表达通信协议而不是某个测试供应商。

默认提取 LLM 使用以下环境变量：

```dotenv
OPENAI_MODEL=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
```

`OPENAI_MODEL` 是 Hyper-Extract 为 OpenAI 兼容路由定义的模型选择变量，不假设 OpenAI SDK 会自动读取它。CLI、Service 和 Python API 的公共配置解析层必须显式解析该变量。这里的 `OPENAI_*` 表示 OpenAI API 协议兼容，不要求端点或模型来自 OpenAI 官方。

由于默认路由的真实端点未知，`openai-compatible-default` 使用最保守的能力声明：

```toml
[profiles.openai-compatible-default.capabilities]
structured_output_modes = ["text_json"]
preferred_structured_output_mode = "text_json"
structured_output_fallback_order = ["text_json"]
```

默认路由不得根据模型名、Base URL 或供应商名称推断 `native`、`json_schema`、Tool Calling、Thinking 或供应商专用参数。更激进的能力只能由显式命名 Profile 声明，或由允许 Probe 覆盖能力的显式配置结合未过期 Probe 结果启用；Probe 不得在没有配置授权时静默改写默认 Profile。

Embedding 可能使用不同模型、端点、密钥和限流额度，继续保持独立配置：

```dotenv
EMBEDDING_MODEL=
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
```

迁移要求：

- 将 Service 默认 Profile 从 `minimax-course-default` 改为厂商无关的 `openai-compatible-default`。
- `openai-compatible-default` 从 `OPENAI_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` 解析提取 LLM，不在 Python 代码中写死模型名或第三方 URL。
- 删除 `service/model_profiles.py` 中当前的 MiniMax 内置 Profile 字典。Registry 只允许保留厂商无关的 `openai-compatible-default` 种子配置；MiniMax 作为外部 TOML 示例和集成测试 fixture 提供，不再作为可隐式选择的内置项。
- 根目录 `.env.example` 只展示通用默认路由；MiniMax 和硅基流动组合移到部署级 TOML 示例或专项集成测试说明。
- Docker 示例中的 MiniMax Profile 使用显式名称，例如 `minimax-m27`，并使用正确拼写的 `MINIMAX_API_KEY`；它是可选部署示例，不是 Service 默认值。
- 删除 `schemas.py` 和 `runner.py` 对 `minimax-course-default` 的默认回退。默认值只能在公共 Profile Registry 中定义一次，避免 API Schema、Worker 和 Registry 漂移。
- `OPENAI_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` 只定义默认单路由。调用方显式选择命名 Profile 时，以该 Profile 的模型、端点和密钥环境变量引用为准，不能被全局默认值静默覆盖。
- API Key 仍只由 Worker 解析；API 进程、公共 Profile 描述、任务请求、日志和指纹不得包含密钥值。

历史兼容按一个小版本迁移：

1. 读取旧 `minimax-course-default` 或 `MIMIMAX_*` 时输出一次 deprecation warning。新旧变量的一致性按单个 Profile 的一次解析判断：同一 Profile 要么完整使用新变量组，要么完整回退到旧变量组，缺字段或跨组拼接时直接返回配置错误；不同 Profile 可以分别引用新旧变量，因此允许同一个进程环境同时存在两组变量。
2. 文档、`.env.example`、Docker 示例和测试 fixture 全部切换到新默认名及正确拼写。
3. 下一小版本删除 `MIMIMAX_*` 回退；如部署仍需要 MiniMax，使用显式 `minimax-m27` TOML Profile 和 `MINIMAX_*` 环境变量。

配置解析与测试必须覆盖以下边界：

- 只设置三项 `OPENAI_*` 即可创建默认提取 LLM。
- `OPENAI_API_KEY` 有值但 `OPENAI_MODEL` 缺失时，在创建模型客户端和任何网络请求之前返回稳定的配置错误。
- 测试是否使用 mock 不能再由 `OPENAI_API_KEY` 是否非空单独决定。普通单元测试始终使用 mock；真实调用必须由 integration marker 或显式测试 fixture 选择，并同时校验所需凭据。
- 默认 Profile 只声明 `text_json`；未知端点不会收到未经声明的 native JSON Schema 或 Tool Calling 请求。
- 独立 Embedding 路由不会错误继承提取模型名或密钥；只有 Profile 明确声明共用时才允许复用。
- 显式命名 Profile 不受默认 `OPENAI_*` 污染。
- Profile 指纹包含最终模型和 Base URL，但不包含 API Key。
- 仓库中除迁移兼容代码和历史计划说明外，不再出现 `MIMIMAX` 拼写。

## 7. 参数映射策略

### 7.1 白名单

Adapter 只接收统一契约字段，并根据 `supported_parameters` 决定是否发送。调用方传入不支持参数时：

- 必填参数不支持：启动前失败。
- 可选参数不支持：只有 Profile 明确声明 `omit_if_unsupported` 时才能省略，并记录事件。
- 禁止把任意 `extra_body` 从业务层直接透传到所有供应商。

### 7.2 Token 参数

统一使用 `max_output_tokens`，由 Adapter 映射到：

- OpenAI Chat 的 `max_tokens` 或模型要求的 `max_completion_tokens`。
- Anthropic 的 `max_tokens`。
- 兼容服务声明的自定义字段。

### 7.3 Thinking 与温度

- Thinking 开关只能通过 Capability/Profile 声明，不能靠检查模型名称猜测。
- 不支持温度的模型不得收到 `temperature`。
- Thinking 内容和最终内容必须在 Normalizer 中分离，不能送入 JSON Schema 校验。

## 8. 结构化输出与坏项隔离

### 8.1 归一化顺序

1. 从供应商响应提取最终内容、reasoning、finish reason 和 usage。
2. 根据能力声明剥离 `<think>` 或内容块中的 reasoning。
3. 提取第一个完整 JSON 对象或数组。
4. 执行 Profile 声明的字段别名转换。
5. 进行整体 Schema 校验。
6. 对列表型结果执行 item 级校验。
7. 产出合法结果、rejection artifact 和 ValidationSummary。

### 8.2 列表型部分成功

以关系列表为例：

- `source` 或 `target` 缺失时，该项进入 quarantine。
- 不允许根据名称相似度补全端点。
- 其他合法关系继续进入后续管线。
- 坏项比例小于阈值时，该批次标记为 `completed_with_rejections`。
- 坏项比例超过阈值时，先携带原始无效 JSON 执行 repair，再重新请求该批次。
- 超过最大尝试次数后，根据 Profile 选择隔离该批或终止当前 chunk。

建议 artifact：

```text
.he-run/
  raw-responses/<request-id>.json
  validation/<request-id>.json
  rejections/<request-id>.jsonl
```

每条 rejection 至少包含：

- request ID、stage、chunk ID、batch ID；
- Schema 路径，例如 `items.4.target`；
- 错误类别和脱敏后的原始 item；
- 最终动作，例如 quarantined、repaired 或 failed；
- Profile、模型和 Prompt 指纹。

每个 batch/chunk 还必须生成 rejection summary，至少包含：

- 已知但受影响的端点 ID 与各自 rejection 数量；
- 缺失或无法解析的端点描述，不能根据相似名称推断；
- 受影响的 chunk、batch 和请求；
- quarantine 后新增的孤立节点或连通分量告警；
- `graph_connectivity_incomplete` 布尔状态。

关系被隔离不等于节点事实本身不可信。下游应使用 `connectivity_status = "incomplete_due_to_rejections"` 或等价质量元数据表达图连接完整性风险，不能笼统篡改节点事实为不可信，也不能合成桥接边。

## 9. 恢复策略矩阵

| 错误 | 默认动作 | 范围 | 备注 |
|---|---|---|---|
| 鉴权失败 | fail | run | 不重试 |
| 不支持参数 | fail | run | Probe 或启动校验阶段发现 |
| 不支持结构化能力 | fallback/fail | request/run | 只允许 Profile 明确声明的有序 fallback |
| 429 | retry | request | 尊重 `Retry-After`，指数退避 |
| 超时、连接重置、5xx | retry | request | 保持相同内容边界 |
| 上下文超限 | split/replan | chunk | 不作为普通网络重试 |
| 输出截断 | split | batch | 缩小节点批次或正文块 |
| JSON 不完整 | repair 或 retry | batch | 保存原始响应 |
| Schema 整体不兼容 | fail | batch/run | 防止反复付费 |
| 单个列表项无效 | quarantine | item | 达到比例阈值时升级为 batch repair |
| Embedding 整批失败 | split | batch | 二分 batch，定位坏项后再隔离 |
| Embedding 单项无效 | quarantine/fail | item | 保持输入位置对齐并输出 artifact |
| Embedding 响应错位或维度异常 | fail | request/run | 视为协议错误，不允许错位写入索引 |
| 多次连续供应商故障 | circuit_break | rate_limit_group | 阻止共享额度上的并发请求继续放大故障 |

恢复策略必须记录在事件日志和 Model Usage 中。`retry_attempts` 应拆分为 transient、validation 和 repair 三类预算。

错误分类和恢复决策必须分层：

1. Adapter 结合 SDK 异常、HTTP 状态、供应商错误码、响应体和响应头，生成稳定 `CanonicalModelFailure`。
2. Normalizer/Schema Validator 生成结构化输出、位置对齐和校验类失败，不解析供应商异常字符串。
3. Recovery Policy 只根据标准失败、Profile、当前 target 和剩余预算生成 `RecoveryDecision`。
4. Pipeline 执行领域相关的 split、replan、quarantine 和 checkpoint，不重复分类供应商错误。

`documents/model_errors.py` 中的字符串匹配分类在迁移期保留兼容入口，所有调用切换到 Adapter 后删除分类实现，避免管线层与 Adapter 双重分类。

### 9.1 速率限制的分类

不能把所有 HTTP 429 都当作相同的临时错误。Adapter 应结合响应码、错误码、响应体和响应头区分：

| 类型 | 示例 | 默认策略 |
|---|---|---|
| 请求频率限制 | RPM 超限、并发请求过多 | 等待窗口恢复并降低并发 |
| Token 频率限制 | TPM 超限 | 等待 token reset，减少发起速度或批次预算 |
| 短时容量不足 | provider overloaded、模型繁忙 | 指数退避，必要时熔断 |
| 账户额度耗尽 | insufficient quota、余额不足 | 立即失败，不重试 |
| 模型或租户硬限制 | 当前模型无权限、套餐限制 | 立即失败，不重试 |

供应商 Adapter 应把上述结果归一为稳定原因，例如：

```text
rate_limit.requests
rate_limit.tokens
rate_limit.concurrency
rate_limit.capacity
quota.exhausted
quota.permission
```

### 9.2 Rate-limit group 级集中调度

限流必须由每个 `rate_limit_group` 共用的调度器处理，不能让 worker 独立退避：

1. 同一 `rate_limit_group` 的请求进入统一队列；多个 Profile 共享账户额度时进入同一个 group。
2. 调度器同时使用并发信号量和可选的 RPM/TPM token bucket。
3. 任一请求收到可恢复 429 后，暂停该 group 发起新请求。
4. 已经在途的请求允许完成，不主动取消。
5. 到达恢复时间后只释放少量请求进行探测，避免所有 worker 同时唤醒。
6. Probe 成功后逐步恢复并发；再次 429 则继续降并发并延长等待。

不同 `rate_limit_group` 必须相互隔离。一个 MiniMax 账户被限流时，不应阻塞使用独立额度的硅基流动或 OpenAI group；反之，指向同一账户额度的两个 Profile 不能因为名称不同而错误隔离。

调度作用域必须按运行形态明确：

- CLI 和直接 Python API 默认使用进程内队列、信号量和 token bucket。
- Service 的 `effective_concurrency`、token bucket、暂停窗口、累计限流状态和 circuit breaker 必须在同一 group 的全部 Worker 进程之间共享。
- Service 可使用 PostgreSQL、Redis 或等价协调后端，但不能用各进程独立的 `asyncio.Semaphore` 冒充分布式限流。
- 在跨进程协调实现前，Service 只正式支持单 Worker；启动时发现多 Worker 必须拒绝或输出明确的不安全配置告警。

### 9.3 等待时间计算

等待时间优先级：

1. 优先使用标准 `Retry-After`。
2. 其次读取 Profile/Adapter 声明的 reset headers，例如 request/token reset。
3. 没有服务端提示时使用带 full jitter 的指数退避。

建议默认值：

```text
base_delay_seconds = 2
max_delay_seconds = 120
max_rate_limit_attempts = 8
max_rate_limit_elapsed_seconds = 1800
```

有明确 `Retry-After` 时，实际等待不能短于服务端要求；可在其后增加少量随机抖动。没有响应头时使用：

```text
delay = random(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
```

速率限制使用独立预算，不消耗 JSON repair、结构校验或普通 5xx 的重试次数。

### 9.4 自适应并发

`max_workers` 表示调用方允许的上限，不等于始终生效的并发数。调度器按 `rate_limit_group` 维护 `effective_concurrency`：

- 初始值为 `min(user_max_workers, profile.recommended_concurrency)`。
- 首次 429 后按比例下降，建议 `4 -> 2`。
- 恢复后再次 429，继续下降为 `2 -> 1`。
- 连续成功达到稳定窗口后，每次只增加 1，直到配置上限。
- TPM 超限时除了降并发，还应降低请求发起速率；不能擅自截断业务正文。

建议采用 AIMD：发生限流时乘法下降，稳定成功时加法恢复。并发调整只影响尚未发出的请求，不改变 checkpoint、chunk 或 batch 边界。

### 9.5 请求恢复边界

- 429 后重试完全相同的 request/batch，不重新切块、不重新抽取已成功内容。
- 请求应携带稳定 request ID；供应商支持时传递 idempotency key。
- checkpoint 在请求成功并通过结构校验后才把该批次标记完成。
- 任务等待限流恢复时，运行状态使用 `throttled`，心跳继续输出下次尝试时间和当前有效并发。
- 超过限流时间或次数预算后，将任务标记为可恢复失败；再次启动时从未完成批次继续。
- 只有调用方显式配置并验证过候选模型时才允许切换 Provider，禁止 HE 自动换模型生成同一任务的剩余部分。

### 9.6 速率限制可观测性

每次限流至少记录：

- Model Profile、operation、request ID、provider request ID；
- 归一化限流原因；
- HTTP 状态和脱敏错误码；
- `Retry-After`/reset 时间，但不保存敏感响应头；
- 限流前后有效并发；
- 本次等待时间、累计等待时间和尝试次数；
- 队列长度、在途请求数以及最终恢复或失败状态。

全书运行摘要应单独报告 `rate_limit_events`、`rate_limit_wait_seconds`、`concurrency_reductions` 和 `quota_failures`，便于区分模型生成慢、HE 编排慢和供应商限流等待。

## 10. 能力探测与 CLI

新增命令：

```bash
he model validate --profile minimax-m27
he model probe --profile minimax-m27
he model show --profile minimax-m27
```

### 10.1 validate

只做本地静态验证，不调用模型：

- Profile Schema 是否有效；
- 必需环境变量是否存在，但不打印值；
- 参数映射是否完整；
- structured output mode 是否属于能力集合；
- context/output token 预算是否自洽；
- 并发和重试值是否合法；
- Embedding batch、输入 token、空输入策略和位置对齐约束是否合法；
- `RecoveryDecision` 的 action/target 组合与 fallback 顺序是否合法。

### 10.2 probe

执行小额真实性请求：

1. 普通文本响应。
2. 最小 JSON 对象。
3. 含列表的 JSON Schema。
4. Thinking 内容分离。
5. finish reason 和 usage 解析。
6. 声明参数是否被端点接受。
7. Embedding 字符串输入与声明的 token-id 输入能力。
8. Embedding batch 上限、长输入和向量维度。
9. 空输入策略与输入/响应位置对齐。

Probe 结果写入：

```text
~/.he/probes/<profile-fingerprint>.json
```

生产任务可要求 Probe 未过期，例如 24 小时，否则拒绝启动或输出明确警告。Probe 结果不应在每个业务请求中自动刷新。

默认行为按 Profile 类型区分：

- `openai-compatible-default` 以快速上手为目标。Probe 缺失或过期时输出明确警告并继续使用保守的 `text_json` 能力，不拒绝启动。
- 显式生产 Profile 可以配置 `probe_required = true`；缺少有效 Probe 时拒绝启动。
- Probe 缺失时不得通过尝试 native JSON Schema 或 Tool Calling 来进行隐式线上探测。

Probe 只覆盖代表性请求，不能证明任意业务 Schema 都兼容。运行中出现 Probe 未覆盖的 `capability_mismatch` 时：

1. 仅当 Profile 声明有序 `structured_output_fallback_order` 时，才允许对尚未成功的同一请求切换到下一模式，例如 `native_json_schema -> text_json`。
2. fallback 使用独立预算，不消耗 transient retry 或 validation repair 次数。
3. 没有已声明 fallback、fallback 已耗尽或请求已经写入成功 checkpoint 时，直接失败，禁止在运行中自动换模型。
4. 实际 mode、失败原因和 fallback 链写入 checkpoint、事件与 Model Usage。
5. 业务请求中的一次失败只形成 run-local capability observation，不自动改写静态 Profile 或全局 Probe 缓存。

## 11. Checkpoint 与可观测性

确定性的 `execution_fingerprint` 新增：

- Model Profile 完整公共指纹；
- 最终生效 Capability Profile 指纹；
- Adapter 名称和版本；
- Response Normalizer 版本；
- Recovery Policy 版本；
- 输出 Schema 指纹；
- Prompt 指纹。

Probe 证据单独记录：

- `probe_evidence_hash`：检查集合、端点身份和结果摘要的确定性哈希；
- `probed_at` 与 `expires_at`：只用于启动资格、健康状态和告警，不进入 `execution_fingerprint`；
- capability observations：运行时遇到的未覆盖兼容性事实和实际 fallback 链。

如果 Probe 只验证静态能力，重新 Probe 得到相同结果不能改变执行指纹。如果 Probe 被允许覆盖 Profile 能力，则只有覆盖后的最终生效能力进入执行指纹，时间戳仍不得进入。运行可复现性使用字节级确定性指纹判断；Probe 是否仍在有效期内是独立的准入判断，不能使用“24 小时内近似相等”的指纹语义。

任务指标新增：

- 每种恢复动作次数；
- repair 成功率；
- item rejection 数和比例；
- 每个 operation 的验证失败率；
- 429、5xx、超时和熔断次数；
- 实际并发、排队时间和 Provider 延迟；
- 因能力不支持而省略的可选参数。

运行摘要必须区分：

- `completed`；
- `completed_with_rejections`；
- `failed`；
- `interrupted`。

## 12. 分阶段实施

### Iteration 1: 统一契约与 Profile 能力层

**目标：** 参数差异在请求前解决，新模型接入不修改课程管线。

- [ ] 新增 `providers/contracts.py`，分别定义 Generation、Embedding、能力、标准失败和恢复决策模型。
- [ ] 新增公共 Profile Schema，迁移 `service/model_profiles.py`。
- [ ] 新增 `openai-compatible-default`，统一解析 `OPENAI_MODEL`、`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
- [ ] 为默认 Profile 定义仅含 `text_json` 的保守 capabilities，禁止按模型名或 URL 猜测 native 能力。
- [ ] 移除根 `.env.example`、API Schema 和 Worker 默认路径中的 MiniMax 固化配置，将 MiniMax 保留为显式部署 Profile。
- [ ] 删除 `service/model_profiles.py` 的 MiniMax 内置字典，只保留厂商无关的默认种子配置。
- [ ] 为 `minimax-course-default` 与 `MIMIMAX_*` 增加单版本 deprecation 迁移，并使用测试锁定新旧变量优先级。
- [ ] 将测试 mock/real API 选择从 `OPENAI_API_KEY` 是否非空解耦；真实调用必须由 integration marker 或显式 fixture 启用。
- [ ] 为旧 Profile 提供兼容加载和 warning。
- [ ] 实现参数白名单与统一 token 参数映射。
- [ ] 实现 OpenAI Chat Adapter，并覆盖 OpenAI 兼容端点。
- [ ] 实现 OpenAI Embedding Adapter，迁移 `CompatibleEmbeddings` 的预分词、batch 上限、输入切分、空输入和位置对齐逻辑。
- [ ] 将原始供应商异常分类下沉到 Adapter，输出 `CanonicalModelFailure`；`recovery.py` 不解析原始异常字符串。
- [ ] 穷举校验 `RecoveryDecision` 的 action/target 合法组合。
- [ ] 让 `create_client()` 和 Service Registry 共用注册表。
- [ ] 将公共 Profile 指纹写入 checkpoint。
- [ ] 增加静态 `he model validate`。
- [ ] 在实验开关下用单次 `StructuredOutputInvoker` 或简单 AutoList 跑通 Gateway pilot，不先替换 Course Pipeline。

**验收：**

- 默认安装只配置三项 `OPENAI_*` 即可解析提取 LLM，且默认 Profile、API Schema 与 Worker 不再出现 MiniMax 厂商名。
- `OPENAI_API_KEY` 有值但 `OPENAI_MODEL` 缺失时，默认路由在客户端创建和网络调用前返回稳定配置错误；对应单元测试不会因 Key 非空切换到真实 API。
- `openai-compatible-default` 的 `preferred_structured_output_mode` 为 `text_json`，不声明 native 能力；只有显式 Profile 或经配置授权且通过 Probe 的能力覆盖才能启用更激进模式。
- 默认路由缺少有效 Probe 时明确告警并继续使用保守能力，不影响快速上手；配置了 `probe_required = true` 的生产 Profile 则拒绝启动。
- 新旧环境变量是否混用按 Profile 解析结果判断，同一环境中的不同 Profile 可以分别使用新旧变量组。
- 独立 Embedding 配置与默认提取 LLM 配置互不覆盖。
- MiniMax 与硅基流动仅使用不同 TOML Profile 即可创建请求。
- Embedding 的 batch、token、空输入和位置对齐行为只由 Profile 与 Adapter 决定，业务层不含供应商特判。
- 不支持参数在模型调用前返回稳定错误。
- CLI、Service 和 Python API 解析出相同公共 Profile 指纹。
- Gateway pilot 覆盖成功、标准失败、fallback 和 retry 预算，且不改变默认执行路径。

### Iteration 2: 响应归一化与结构化输出韧性

**目标：** 模型响应差异不泄漏到业务层，局部坏数据不终止整本书。

- [ ] 将 `_message_text` 和 JSON 提取迁入公共 Normalizer。
- [ ] 支持 string、content blocks、reasoning field、inline thinking 和 JSON fence fixtures。
- [ ] StructuredOutputInvoker 改为调用 Gateway。
- [ ] 正确实现多次 repair，repair prompt 必须包含脱敏后的原始无效 JSON。
- [ ] 实现列表 item 级校验和 quarantine。
- [ ] 保存 raw response、validation 和 rejection artifacts。
- [ ] rejection summary 汇总受影响端点、未知端点、孤立节点/连通分量告警和 `graph_connectivity_incomplete`。
- [ ] 用 `RecoveryDecision` 替代全局 `retryable` 判断。
- [ ] 移除“所有 OutputValidationError 一律可重试”的临时行为。

**验收：**

- 一条缺少 `target` 的边被隔离，其他合法边保留。
- 下游能看见连接完整性告警，但不会生成桥接边或把关系失败误写成节点事实失败。
- 坏项超过阈值时按策略 repair/retry，日志可解释每次动作。
- 鉴权、参数错误和系统性 Schema 错误不会盲目重试。

### Iteration 3: Probe、限流与熔断

**目标：** 上线前验证真实端点，并在高并发时控制故障范围。

- [ ] 实现 `he model probe/show`。
- [ ] Probe 结果缓存、过期和 evidence hash；Probe TTL 不进入 `execution_fingerprint`。
- [ ] 实现运行时 `capability_mismatch` 的显式 fallback/fail，并记录实际模式链。
- [ ] CLI/Python API 按 `rate_limit_group` 使用进程内调度。
- [ ] Service 为同一 `rate_limit_group` 实现跨 Worker 共享并发、token bucket、暂停窗口和熔断；实现前明确限制为单 Worker。
- [ ] 支持 RPM/TPM 和 `Retry-After`。
- [ ] 实现连续失败熔断与半开探测。
- [ ] `max_workers` 与 Profile 推荐并发协商，按 group 输出实际生效值。
- [ ] Service 启动健康检查显示模型可用性，不暴露密钥。

**验收：**

- 并发 1、4、8 的测试能显示实际并发、限流和吞吐。
- 两个 Worker 使用同一 group 时总并发不超过共享上限；不同 group 互不阻塞。
- 429 不造成请求风暴。
- 供应商连续故障后新请求快速失败，恢复后可以半开验证。

### Iteration 4: 真实供应商契约验证与文档

**目标：** 建立后续接入模型的统一准入门槛。

- [ ] 建立离线响应 fixture 契约测试。
- [ ] 建立标记为 integration 的真实性 Probe 测试。
- [ ] 验证 OpenAI 原生结构化输出。
- [ ] 验证 MiniMax `text_json` 和 Thinking 处理。
- [ ] 验证硅基流动 DeepSeek 的兼容接口。
- [ ] 验证 OpenAI 与硅基流动 Embeddings 的 batch、长输入、空输入和位置对齐行为。
- [ ] 用同一章文档执行三模型短流程，不比较措辞，只比较契约与质量门槛。
- [ ] 更新中英文 Provider System、CLI、故障排查文档。
- [ ] 更新 `docker/model-profiles.example.toml`。

**验收：**

- 新 Provider PR 必须附 Profile、离线 fixture 和 Probe 报告。
- 三条真实路由均能完成节点、局部关系、去重和全局关系阶段。
- 文档不再笼统声称“OpenAI compatible 即可”，而是列出已验证能力。

## 13. 测试矩阵

### 13.1 离线契约测试

- 标准 JSON 对象和数组；
- `<think>`、`<thinking>` 和独立 reasoning 字段；
- content block 数组；
- JSON fence 和前后说明；
- 截断 JSON 和 finish reason；
- 缺少必填字段；
- 列表中单条坏项与多条坏项；
- 字段别名；
- 429、401、超时、5xx 和不支持参数；
- repair 成功、repair 失败和超出预算；
- `capability_mismatch` 的显式 fallback、fallback 耗尽和禁止隐式换模型；
- 每种合法 action/target 组合以及非法组合拒绝；
- Embedding batch 拆分、单项 quarantine、空输入策略、响应数量/顺序/维度异常；
- 同一 `rate_limit_group` 跨 Worker 协调和不同 group 隔离；
- checkpoint 恢复后不重复已完成请求。

### 13.2 真实性测试

| 路由 | 结构化模式 | 重点 |
|---|---|---|
| OpenAI | native/json_schema | 严格 Schema、usage、token 参数 |
| MiniMax | text_json | Thinking、长 JSON、坏项恢复、并发 |
| 硅基流动 DeepSeek | text_json/json_object | OpenAI 兼容参数和 reasoning 输出 |

真实性测试必须使用最小输入并显式标记 `integration`，默认单元测试不得调用真实 API。

### 13.3 全书回归

完成前三个迭代后，使用固定 PMBOK Document Package 进行一次全书回归，比较：

- 完成状态与断点恢复；
- 总调用、失败、repair、retry 和 rejection 数；
- 节点、关系和目录覆盖；
- 总时长、各阶段时长和费用；
- 是否存在因结构化输出问题导致的整书失败。

## 14. 迁移与兼容策略

1. 第一阶段默认保持现有执行路径，可通过实验开关启用 Gateway。
2. 先用单次 `StructuredOutputInvoker` 或简单 AutoList 做 Gateway pilot，验证契约和恢复链路，不把它视为首个生产迁移完成。
3. Pilot 稳定后，Course Pipeline 作为首个生产级迁移对象，因为它已经具备 checkpoint、用量和 rejection artifact 基础。
4. Course Pipeline 验证稳定后迁移其他 AutoList、AutoGraph 等通用类型。
5. 旧 `structured_output_mode` 继续映射为能力偏好；能力不支持时仅允许 Profile 明确声明的 fallback，否则启动或请求失败。
6. 旧 checkpoint 可以继续由旧版本恢复；新 Gateway 运行使用新的 pipeline/adapter 版本指纹。
7. 不自动迁移不同模型、不同 Profile 或不同恢复策略的 checkpoint。
8. 临时 `OutputValidationError.retryable = True` 仅用于当前实验，不作为最终实现保留。

## 15. 完成判定

本计划完成必须同时满足：

- [ ] 新增一个常见 OpenAI 兼容模型时只需添加 Model Profile 和测试 fixture。
- [ ] 新增一个 OpenAI 兼容 Embedding 路由时只需声明能力和 Profile，不修改索引业务代码。
- [ ] 参数、结构化能力和 token 限制在启动前可验证。
- [ ] 单条无效边不会终止整本书，也不会被静默丢弃。
- [ ] 每条坏项都能定位到请求、阶段、块、批次、Schema 路径和最终动作。
- [ ] Embedding 坏项不会造成输入与向量错位，zero-vector 不会被静默写入。
- [ ] 限流、超时、截断、校验失败和鉴权错误执行不同的确定性策略。
- [ ] 修改 Profile、Adapter、Schema、Prompt 或恢复策略会改变运行指纹。
- [ ] Probe TTL 过期不会改变执行指纹，但会独立阻止或警告任务启动；运行时 fallback 可审计且可恢复。
- [ ] OpenAI、MiniMax、硅基流动通过统一契约真实性验证。
- [ ] CLI、Service 和 Python API 使用同一模型能力与 Profile 实现。
- [ ] 单元测试默认不产生真实 API 费用。
- [ ] 中英文文档、示例 Profile 和故障排查说明同步完成。

## 16. 建议提交顺序

1. `feat(provider): add canonical model contracts and capability profiles`
2. `feat(provider): add embedding adapter and capability contract`
3. `refactor(provider): route client and service profiles through registry`
4. `feat(provider): normalize failures and add recovery decisions`
5. `feat(provider): quarantine invalid structured output items`
6. `feat(cli): add model validate and probe commands`
7. `feat(provider): add distributed rate limiting and circuit breaker`
8. `test(provider): add provider conformance and integration matrix`
9. `docs(provider): document capability profiles and recovery behavior`

每个提交都应能独立通过单元测试，避免把 Provider 重构、课程质量规则和性能优化混在同一个提交中。
