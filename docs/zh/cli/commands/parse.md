# he parse

从文档中提取知识并保存到知识库。

---

## 概要

```bash
he parse INPUT [OPTIONS]
```

## 参数

| 参数 | 描述 |
|----------|-------------|
| `INPUT` | 输入文件路径、目录或 `-` 表示标准输入 |

## 选项

| 选项 | 简写 | 描述 |
|--------|-------|-------------|
| `--output` | `-o` | 输出目录（必填） |
| `--template` | `-t` | 要使用的模板（省略以进行交互式选择） |
| `--method` | `-m` | 方法模板（例如 `light_rag`、`graph_rag`） |
| `--lang` | `-l` | 语言：`zh` 或 `en`（知识模板必填） |
| `--force` | `-f` | 强制覆盖现有输出 |
| `--no-index` | — | 跳过构建搜索索引 |
| `--input-format` | — | 输入格式：`auto`、`text`、`docling-json` 或 `document-package` |
| `--resume / --no-resume` | — | 恢复匹配的结构化文档任务（默认恢复） |
| `--chunk-target-tokens` | — | 章节感知块的目标 token 数 |
| `--chunk-max-tokens` | — | 章节感知块的最大 token 数 |
| `--max-workers` | — | 最大分块并发数 |
| `--retry-attempts` | — | 临时 API 错误的最大尝试次数 |
| `--request-timeout` | — | 单次模型请求超时秒数 |
| `--heartbeat-interval` | — | 长请求心跳间隔秒数 |
| `--model-context-tokens` | — | 用于请求预算的模型上下文窗口 |
| `--output-reserve-tokens` | — | 为单次结构化响应预留的 token 数 |
| `--semantic-dedup / --no-semantic-dedup` | — | 是否用向量和模型合并同义知识点 |
| `--community-reports / --no-community-reports` | — | 是否生成模型社区摘要 |

---

## 示例

### 基本用法

从单个文件提取：

```bash
he parse document.md -t general/biography_graph -o ./output/ -l zh
```

### 交互式模板选择

省略 `-t` 以从可用模板中选择：

```bash
he parse document.md -o ./output/ -l zh

# 您将看到：
# Select a template:
#   [1] general/biography_graph
#   [2] general/graph
#   [3] finance/earnings_summary
#   ...
# Enter number or search keyword: 
```

### 处理目录

从目录中的所有 `.md` 和 `.txt` 文件提取：

```bash
he parse ./documents/ -t general/concept_graph -o ./output/ -l zh
```

文件按字母顺序组合后进行提取。

### 使用方法而非模板

使用底层提取方法：

```bash
he parse document.md -m light_rag -o ./output/
```

方法使用各自注册的提示语言。

### 处理课程长文档包

调用方可使用 Docling、本地解析器或远程文档服务生成标准 Document Package。包内必须包含 `manifest.json`、`outline.json`、`provenance.jsonl` 和 manifest 声明的内容文件。

```bash
he parse ./book.hepkg \
  -m course_knowledge_graph \
  -o ./book-course-graph \
  --input-format document-package \
  --resume \
  --chunk-target-tokens 4000 \
  --chunk-max-tokens 6000 \
  --max-workers 2 \
  --retry-attempts 4 \
  --no-index
```

HE 会在模型调用前验证包协议、路径安全、哈希、目录树和来源映射。任务状态保存在 `.he-run/`；进程中断后执行同一命令会跳过已完成的节点与关系抽取。包内容、模型、提示词或切片参数变化时会拒绝错误续跑。

旧有 Docling JSON 可使用 `--input-format docling-json` 直读，建议仅用于迁移和兼容验证。

### 强制覆盖

覆盖现有输出目录：

```bash
he parse document.md -t general/biography_graph -o ./output/ -l zh -f
```

### 跳过索引构建

如果您不需要搜索/聊天，可以加快提取速度：

```bash
he parse document.md -t general/biography_graph -o ./output/ -l zh --no-index
```

之后使用 `he build-index` 构建索引。

### 从标准输入读取

```bash
cat document.md | he parse - -t general/biography_graph -o ./output/ -l zh
```

---

## 输出结构

```
./output/
├── data.json           # 提取的知识（实体、关系等）
├── metadata.json       # 提取元数据
│   ├── template        # 使用的模板
│   ├── lang           # 语言
│   ├── created_at     # 创建时间戳
│   └── updated_at     # 最后更新时间戳
└── index/             # 向量搜索索引（如已构建）
    ├── index.faiss
    └── docstore.json
```

`course_knowledge_graph` 还会输出 `outline.json`、`source-map.json`、`merge-log.json`、`quality-report.json`、`community_data.json`、`course-graph.json` 和 `run-summary.json`。

---

## 语言支持

模板支持多种语言：

```bash
# 英文
he parse doc.md -t general/biography_graph -o ./output/ -l zh -o ./output/

# 中文
he parse doc.md -t general/biography_graph -l zh -o ./output/
```

选择与文档匹配的语言以获得最佳效果。

---

## 常见用例

### 研究论文

```bash
he parse paper.md -t general/concept_graph -o ./paper_kb/ -l zh
```

### 传记

```bash
he parse biography.md -t general/biography_graph -o ./output/ -l zh
```

### 法律合同

```bash
he parse contract.md -t legal/contract_obligation -o ./contract_kb/ -l zh
```

### 财务报告

```bash
he parse earnings.md -t finance/earnings_summary -o ./finance_kb/ -l zh
```

---

## 错误处理

### "输出目录已存在"

输出目录存在且不为空。解决方案：

1. 使用 `-f` 强制覆盖
2. 选择不同的输出路径
3. 先删除现有目录

### "模板未找到"

指定的模板不存在。解决方案：

1. 列出可用模板：`he list template`
2. 使用交互式选择（省略 `-t`）
3. 检查模板路径拼写

### "需要语言"

知识模板需要语言参数。方法不需要：

```bash
# 模板 - 需要 -l
he parse doc.md -t general/biography_graph -o ./output/ -l zh

# 方法 - 不需要 -l
he parse doc.md -m light_rag -o ./out/
```

---

## 最佳实践

1. **选择正确的模板** — 匹配您的文档类型
2. **使用正确的语言** — 提高提取质量
3. **组织输出** — 使用描述性目录名
4. **批量处理时跳过索引** — 使用 `--no-index`，最后统一构建

---

## 另请参见

- [`he feed`](feed.md) — 增量添加文档
- [`he build-index`](build-index.md) — 构建搜索索引
- [`he list`](list.md) — 列出可用模板
- [模板库](../../templates/index.md)
