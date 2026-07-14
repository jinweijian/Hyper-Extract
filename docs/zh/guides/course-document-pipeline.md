# 课程长文档知识图谱流水线

`course_knowledge_graph` 用于把解析器无关的 Document Package 处理为保留全局章节结构、来源定位和教学关系的课程知识图谱。它是方法级能力，不依赖具体产品、业务仓库或 Docling 部署方式。

## 输入

推荐由调用方使用 Docling、本地解析器或远程文档服务生成目录与内容，再转换为 Document Package：

```text
book.hepkg/
  manifest.json
  outline.json
  provenance.jsonl
  content/
    0001.md
    0002.md
```

`manifest.json` 使用 `HyperExtractDocumentPackage` `1.0` 或 `1.1`，声明文档、生产者、目录文件、来源文件以及每个内容文件的顺序、类型、目录归属、SHA-256、字节数和 `extract` 策略；Package 1.1 还包含 `extraction-brief.yaml`。`outline.json` 使用 `HyperExtractOutline` `1.0`。HE 会在模型调用前校验版本、路径边界、符号链接、哈希、大小、重复 ID、父节点、目录环和内容引用；未声明文件不参与处理。

Docling JSON 直读仍作为兼容入口，但它把解析器适配逻辑放进 HE，新的生产接入应优先使用 Document Package。

## 运行

```bash
uv run he parse ./book.hepkg \
  -m course_knowledge_graph \
  -o ./book-course-graph \
  --input-format document-package \
  --resume \
  --chunk-target-tokens 4000 \
  --chunk-max-tokens 6000 \
  --max-workers 2 \
  --retry-attempts 4 \
  --request-timeout 900 \
  --heartbeat-interval 30 \
  --no-index
```

可用命名路由在同一 `.env` 中配置多个 OpenAI 兼容服务。例如设置 `HYPER_EXTRACT_LLM_PROFILE=MIMIMAX` 后，HE 会读取 `MIMIMAX_MODEL`、`MIMIMAX_API_KEY` 和 `MIMIMAX_BASE_URL`。Embedding 可独立设置 `HYPER_EXTRACT_EMBEDDING_PROFILE=EMBEDDING`。对于不支持原生 JSON Schema 的模型，使用 `HYPER_EXTRACT_STRUCTURED_OUTPUT_MODE=text_json` 避免自动能力探测产生额外请求。

不要在恢复任务时增加 `--force`。`--force` 会清空 `.he-run` 检查点并从头开始。输入文件、模型、提示词或关键切片参数发生变化时，流水线会拒绝复用不匹配的现场。

## 阶段

1. `ingest`：严格校验 Document Package，读取全局大纲、正文策略与来源定位。
2. `chunk_plan`：按章节边界规划大块，并给每块注入全局大纲、当前章节范围和已识别术语。
3. `local_extract`：先抽知识点，再在同块知识点之间抽局部教学关系。
4. `deduplicate`：全书精确去重、向量召回同义候选、模型判定同义项。
5. `global_edges`：补充跨章节的前置、相关、衍生和易混关系。
6. `quality`：检查章节覆盖、关系分布和悬空边。
7. `communities`：运行 Louvain 社区检测，并可选地按社区生成主题摘要。
8. `finalize`：写入 Hyper-Extract 原生数据和课程图谱产物。

## 检查点与监控

所有运行状态都在输出目录中持久化：

```text
.he-run/
  run.json
  events.jsonl
  chunks/<chunk-id>/input.json
  chunks/<chunk-id>/nodes.json
  chunks/<chunk-id>/local-edges.json
  chunks/<chunk-id>/graph.json
  stages/dedup-decisions/
  stages/global-edge-batches/
  stages/community-reports/
run-summary.json
```

长请求会定期向终端和 `events.jsonl` 写入 heartbeat。临时网络错误、限流、5xx、超时和结构化 JSON 截断会指数退避重试；持续的上下文长度错误会按段落边界继续拆分当前块。正常结束、失败和可捕获的中断都会写入 `run-summary.json`。

查看最新状态：

```bash
tail -f ./book-course-graph/.he-run/events.jsonl
cat ./book-course-graph/.he-run/run.json
cat ./book-course-graph/run-summary.json
```

## 输出

- `course-graph.json`：章节骨架、知识点、教学关系和质量摘要。
- `outline.json`：输入包中的完整章节层级与来源位置。
- `source-map.json`：知识点到来源引用和章节的映射。
- `merge-log.json`：精确与语义去重记录。
- `quality-report.json`：覆盖率、关系分布和悬空边。
- `community_data.json`：知识社区及主题摘要。
- `data.json`、`metadata.json`：兼容 Hyper-Extract 的原生知识库格式。
- `run-summary.json`：最终状态、阶段、耗时和错误。
