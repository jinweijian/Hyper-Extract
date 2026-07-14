# Course Extraction Profiles And Quality Evaluation

`CourseExtractionProfile` is the single source of truth for course knowledge-point definitions, granularity, relations, content policies, and quality gates. It is independent of any textbook, parser, or downstream product.

## Validate And Inspect

```bash
he profile validate hyperextract/profiles/defaults/course-knowledge-default.yaml
he profile render hyperextract/profiles/defaults/course-knowledge-default.yaml --stage nodes
he profile render hyperextract/profiles/defaults/course-knowledge-default.yaml --stage global-edges
```

Profiles use a strict schema. Unknown fields, invalid relation directions, and conflicting content policies fail before any model call.

## Run Extraction

```bash
he parse course.hepkg \
  -m course_knowledge_graph \
  --input-format document-package \
  --profile profile.yaml \
  -o output \
  --resume \
  --no-index
```

The checkpoint records the profile name, version, content hash, and compiled prompt hash. A profile change cannot silently reuse old model results.

## Evaluate A Gold Dataset

```bash
he evaluate course-profile \
  --dataset gold.json \
  --graph output/course-graph.json \
  --profile profile.yaml \
  --output output/course-evaluation.json
```

Evaluation does not call a model or an embedder. The report covers required recall, effective precision, forbidden leakage, outline accuracy, extractable-outline coverage, evidence coverage, duplicates, key-relation precision/recall, and annotator agreement.

