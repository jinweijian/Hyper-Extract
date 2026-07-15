# P0-1 Document Package Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Hyper-Extract 在不安装或调用 Docling 的情况下，安全读取保留目录、顺序、内容类型和来源的 Document Package v1，并输出可校验的 Course Graph v1。

**Architecture:** `product-design` 的外部适配器把 Docling Markdown/JSON 转换为中立目录包。Hyper-Extract 通过独立的 Schema、Validator 和 Reader 将包转换为现有 `DocumentOutline` 与 `SourceBlock`，课程 Pipeline 不再直接绑定 Docling Reader；`docling-json` 仅作为迁移期兼容入口。

**Tech Stack:** Python 3.11、Pydantic v2、Typer、pytest、Node.js 24、SHA-256、JSON/JSONL。

## Global Constraints

- HE 核心不得导入 `docling` 或 `docling_core` 才能处理 Document Package。
- `manifest.json`、`outline.json`、所有 content 文件和 `provenance.jsonl` 必须先校验，模型调用只能发生在校验完成后。
- 目录 ID、标题、深度、父节点和顺序必须原样保留。
- 未声明文件忽略；声明的绝对路径、`..` 路径和符号链接拒绝。
- 保留现有 text、template 和 `docling-json` 行为。
- 本轮正式长文档测试使用 `PMPBOK_CH_2.md` 代理夹具，不运行完整 PMBOK。
- 离线测试命令必须显式使用 `OPENAI_API_KEY=""`。

---

### Task 1: External Docling Fixture Adapter

**Files:**
- Create: `/Users/king/website/product-design/scripts/lib/document-package.mjs`
- Create: `/Users/king/website/product-design/scripts/convert-pmpbok-ch2-to-hepkg.mjs`
- Create: `/Users/king/website/product-design/scripts/tests/document-package.test.mjs`
- Modify: `/Users/king/website/product-design/package.json`
- Generate: `/Users/king/website/product-design/data/test-fixtures/pmpbok-chapter-2/PMPBOK_CH_2.hepkg/`

**Interfaces:**
- Produces `buildDocumentPackage(fixtureMarkdown, fixtureManifest) -> {files, manifest}`.
- Package root contains `manifest.json`, `outline.json`, `provenance.jsonl`, and `content/*.md`.
- `manifest.json` uses schema `HyperExtractDocumentPackage` version `1.0`.

- [ ] Write a failing Node test asserting 29 outline nodes plus one root, exact parent hierarchy, five content kinds, SHA-256 values, and `extract=false` for distractors.
- [ ] Run `node --test scripts/tests/document-package.test.mjs`; expect module-not-found failure.
- [ ] Implement heading normalization using fixture `expectedOutline`; preserve non-outline headings as body text.
- [ ] Emit one body content file per outline item and one file per distractor segment.
- [ ] Emit line-range provenance for every content entry.
- [ ] Run `npm run test:he-fixtures`; expect all tests to pass.
- [ ] Run `npm run he:fixture:pmpbok-ch2:package` twice; expect identical manifest and content hashes.

### Task 2: Document Package Schema, Validator, and Reader

**Files:**
- Create: `hyperextract/documents/document_package.py`
- Modify: `hyperextract/documents/models.py`
- Modify: `hyperextract/documents/__init__.py`
- Create: `tests/documents/test_document_package.py`
- Create: `tests/fixtures/document_package.py`

**Interfaces:**
- `DocumentPackageLimits(max_files, max_file_bytes, max_total_bytes)` controls resource limits.
- `validate_document_package(path, limits=None) -> ValidatedDocumentPackage` performs all non-model validation.
- `load_document_package(path, limits=None) -> tuple[DocumentOutline, list[SourceBlock]]` returns only entries with `extract=true` while validating every declared entry.
- `document_package_fingerprint(path) -> str` hashes the normalized manifest and all declared file hashes.

- [ ] Write failing tests for valid package reading and exact outline fidelity.
- [ ] Write failing tests for unsupported version, bad hash, missing file, duplicate outline ID, orphan, cycle, duplicate order, invalid outline reference, unsafe path, symlink, and size limits.
- [ ] Run `OPENAI_API_KEY="" .venv/bin/pytest tests/documents/test_document_package.py -q`; expect failures caused by missing implementation.
- [ ] Implement strict Pydantic manifest/outline/provenance models.
- [ ] Implement safe path resolution and resource accounting before reading content.
- [ ] Implement outline graph validation and source reference hydration.
- [ ] Implement reader conversion to existing internal models without importing Docling.
- [ ] Re-run the focused tests; expect all to pass.

### Task 3: Course Graph v1 Schema

**Files:**
- Create: `hyperextract/documents/course_graph.py`
- Modify: `hyperextract/documents/__init__.py`
- Create: `tests/documents/test_course_graph_schema.py`

**Interfaces:**
- `CourseGraphV1` contains `outline`, `knowledge_nodes`, `structural_edges`, `semantic_edges`, and run metadata.
- `CourseKnowledgeNodeV1` requires `parent_outline_id`, non-empty `evidence`, non-empty `source_refs`, `profile_version`, and `run_id`.
- Structural edge types are `contains` and `describes`; semantic edge types are `prerequisite`, `derivative`, `related`, and `confusable`.

- [ ] Write failing tests for a valid graph and all required-field/enum violations.
- [ ] Write failing tests for dangling outline references, dangling edge endpoints, self-loops, and duplicate edges.
- [ ] Run the focused test file and confirm RED.
- [ ] Implement Pydantic models and model-level cross-reference validation.
- [ ] Re-run and confirm GREEN.

### Task 4: Pipeline and CLI Integration

**Files:**
- Modify: `hyperextract/documents/course_pipeline.py`
- Modify: `hyperextract/cli/cli.py`
- Modify: `tests/documents/test_course_pipeline.py`
- Create: `tests/cli/test_document_package_parse.py`

**Interfaces:**
- `run_course_document(..., input_format: Literal["document-package", "docling-json"])` selects the reader before chunk planning.
- `--input-format document-package` activates Document Package input.
- `--input-format docling-json` remains available and emits a migration warning.

- [ ] Add a failing pipeline test proving a package and equivalent Docling fixture reach the same `DocumentOutline` and chunk plan.
- [ ] Add a failing CLI test proving malformed packages fail before `Template.create` or any model call.
- [ ] Refactor reader selection behind a small ingestion function.
- [ ] Use package fingerprint rather than directory mtime or path for checkpoint identity.
- [ ] Add the CLI input format and migration warning.
- [ ] Re-run focused pipeline and CLI tests.

### Task 5: Documentation and P0-1 Acceptance

**Files:**
- Create: `docs/en/guides/document-package.md`
- Create: `docs/zh/guides/document-package.md`
- Modify: `docs/en/cli/commands/parse.md`
- Modify: `docs/zh/cli/commands/parse.md`
- Modify: `mkdocs.yml`

- [ ] Document every JSON field, content kind, limit, error, and migration path.
- [ ] Document that HE does not run Docling and that adapters can be local, remote, or custom.
- [ ] Add the same navigation entry to English and Chinese docs.
- [ ] Run `OPENAI_API_KEY="" .venv/bin/pytest tests/documents tests/cli -q`.
- [ ] Run `OPENAI_API_KEY="" .venv/bin/pytest -m "not integration" -q`.
- [ ] Run `.venv/bin/ruff check hyperextract` and `.venv/bin/ruff format --check hyperextract`.
- [ ] Run `mkdocs build --strict`.
- [ ] Verify the PMBOK chapter package has 100% outline ID/title/depth/parent/order fidelity and zero model calls during validation.

