# 课程抽取 Profile 与质量评测

`CourseExtractionProfile` 是课程知识点定义、颗粒度、关系、内容策略和质量门禁的单一事实来源。它不依赖具体教材、解析器或调用方产品。

## 校验与查看

```bash
he profile validate hyperextract/profiles/defaults/course-knowledge-default.yaml
he profile render hyperextract/profiles/defaults/course-knowledge-default.yaml --stage nodes
he profile render hyperextract/profiles/defaults/course-knowledge-default.yaml --stage global-edges
```

Profile 使用严格 Schema。未知字段、错误关系方向和冲突内容策略会在模型调用前失败。

## 运行抽取

```bash
he parse course.hepkg \
  -m course_knowledge_graph \
  --input-format document-package \
  --profile profile.yaml \
  -o output \
  --resume \
  --no-index
```

Profile 名称、版本、内容哈希和编译 Prompt 哈希会写入 checkpoint。修改 Profile 后，旧 checkpoint 不会被复用；需要使用新的输出目录或显式 `--force`。

## Gold Dataset 评测

```bash
he evaluate course-profile \
  --dataset gold.json \
  --graph output/course-graph.json \
  --profile profile.yaml \
  --output output/course-evaluation.json
```

评测过程不调用模型或 Embedding。报告包含 required 召回率、有效精确率、禁止项泄漏、目录准确率、可抽取目录覆盖率、证据覆盖率、重复率、关键关系精确率/召回率和标注者一致率。

