# ExtractionBrief 抽取意图契约

`ExtractionBrief` 是由调用方提供、仅作用于本次运行的语义意图契约。它说明为什么抽取、如何理解文档、希望采用什么颗粒度和术语，以及各处理阶段有哪些业务要求。它不是原始正文、模型参数，也不能替代 Extraction Profile。

## Package 边界

生产调用方必须使用 Document Package `1.1`，并把 YAML 放在 Package 目录内。HE 不接受包外绝对路径，也不在 API 请求中接受临时的 system prompt 字符串。

```text
book.hepkg/
  manifest.json
  extraction-brief.yaml
  outline.json
  provenance.jsonl
  content/*.md
```

`manifest.json` 必须声明并保护该文件：

```json
{
  "schema_version": "1.1",
  "extraction_brief": {
    "path": "extraction-brief.yaml",
    "sha256": "<YAML 原始字节的 SHA-256>",
    "bytes": 2048
  }
}
```

模型初始化前，HE 会校验路径、扩展名、大小、字节数、SHA-256、YAML 语法和 `HyperExtractExtractionBrief` Schema。规范化 Brief 会进入 Package 指纹、Prompt 指纹和断点身份。

## 通用结构

```yaml
schema_name: HyperExtractExtractionBrief
schema_version: "1.0"
metadata:
  id: example-extraction
  version: "1.0"
  description: 本次抽取意图说明
task:
  objective: 抽取有原文证据、可以独立使用的知识
  output_usage: [知识导航]
  target_audience: [领域用户]
domain:
  name: 示例领域
  description: 可选的领域背景
  language: zh
source:
  document_type: 手册
  title: 示例手册
  role: 主要知识来源
  authority: 官方出版物
  interpretation: manifest 提供的目录是原文结构，必须保留
extraction_policy:
  granularity: 一个可以独立解释的知识单元
  focus: [有定义的概念, 方法]
  exclusions: [页眉页脚]
  preserve_source_hierarchy: true
  evidence_required: true
relation_policy:
  priorities: [前置关系]
  allowed: []
  forbidden: []
  require_evidence: true
terminology:
  canonical_names: {}
  aliases: {}
  naming_rules: [优先使用原文正式术语]
stage_instructions:
  node_extraction: []
  local_relation_extraction: []
  deduplication: []
  global_relation_extraction: []
  community: []
  evaluation: []
additional_instructions: []
extensions:
  com.example.domain: {}
```

扩展字段必须使用反向域名命名空间。当前实现会把完整扩展内容传入所有已编译模型阶段的 system message，但不会在核心代码中解释课程、法律、医疗等业务含义；不要在扩展中放密钥或不应写入运行快照的敏感数据。

## Prompt 编译

Package 包含 Brief 时，HE 使用两条消息：

1. **System message**：HE 输出与证据约束、Extraction Profile 规则、当前阶段需要的 Brief 投影。
2. **User message**：Package 目录上下文、已知节点或候选关系，以及当前原文。

节点阶段不会收到只属于全局关系阶段的要求；去重阶段重点接收术语规范；关系阶段接收关系策略。`additional_instructions` 和 `extensions` 当前进入所有已编译阶段。Brief 可以收窄或澄清业务目标，但不能覆盖输出 Schema、证据要求，也不能要求模型生成原文不存在的事实。`evaluation` 已进入 Brief Schema 和渲染器，但当前 Course API Pipeline 尚未调用该模型阶段；API Worker 还配置了 `community_reports=false`，因此当前服务路径通常不会执行社区摘要 Prompt。

运行目录 `.he-run/` 会保存规范化 JSON、YAML 快照和各模型阶段的编译后 Prompt 模板。修改 Brief 会改变 Package 与 Prompt 指纹，因此 `--resume` 不会错误复用采用旧意图生成的结果。
