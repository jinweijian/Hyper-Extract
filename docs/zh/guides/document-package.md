# Document Package v1

Document Package 是解析器与 Hyper-Extract 之间的稳定协议。解析器可以是本地 Docling、独立部署的 Docling API 或其他文档服务；HE 不启动、安装或调用这些解析器。

## 目录结构

```text
book.hepkg/
  manifest.json
  outline.json
  provenance.jsonl
  content/*.md
```

`manifest.json` 必须声明：

| 字段 | 说明 |
|---|---|
| `schema_name` | 固定为 `HyperExtractDocumentPackage` |
| `schema_version` | 当前为 `1.0` |
| `document` | `id`、`title`、`language` 和可选源文件信息 |
| `producer` | 适配器名称和版本 |
| `outline_path` | 目录 JSON 的包内相对路径 |
| `provenance_path` | JSONL 来源映射的包内相对路径 |
| `contents` | 内容 ID、路径、顺序、类型、目录 ID、SHA-256、字节数和抽取策略 |

内容类型支持 `body`、`table_of_contents`、`appendix`、`references`、`index`、`front_matter`、`back_matter` 和 `other`。只有 `extract=true` 的内容进入切块与模型抽取，但所有声明文件都会先校验。

`outline.json` 的 `schema_name` 为 `HyperExtractOutline`，版本为 `1.0`。每个节点包含 `id`、`title`、`depth`、`parent_id`、`order` 和 `source_refs`。目录由上游解析器确定，HE 原样保留，不用模型重新猜测。

`provenance.jsonl` 每行对应一个 content ID：

```json
{"content_id":"content-2-1","source_refs":[{"ref":"book.md#L20-L42","source_path":"book.md","start_line":20,"end_line":42}]}
```

## 安全与限制

模型初始化前，HE 会拒绝：

- 不支持的 Schema 版本、缺失文件、哈希或字节数不一致。
- 绝对路径、`..` 路径、符号链接和逃逸包根目录的路径。
- 重复内容 ID、路径或顺序。
- 重复目录 ID/顺序、孤儿父节点、目录环和错误深度。
- 正文引用不存在的目录，或 provenance 与 manifest 不一致。
- 超出 `DocumentPackageLimits` 文件数、单文件大小或总大小的包。

未在 manifest 声明的文件默认忽略。

## 运行

```bash
he parse ./book.hepkg \
  -m course_knowledge_graph \
  -o ./book-course-graph \
  --input-format document-package \
  --resume \
  --no-index
```

包的规范化内容指纹进入断点配置。内容、目录或来源发生变化后，旧现场不会被错误复用。`docling-json` 仍可用于迁移，但新的生产接入应由调用方生成 Document Package。
