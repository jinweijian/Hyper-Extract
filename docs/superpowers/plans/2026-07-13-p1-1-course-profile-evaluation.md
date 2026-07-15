# P1-1 Course Profile And Evaluation Implementation Plan

> **For contributors:** Follow the repository test-first workflow and keep the implementation parser-neutral and product-neutral.

**Goal:** Make course knowledge quality rules versioned, inspectable, enforceable, and measurable with a reusable Course Extraction Profile and Gold Dataset evaluator.

**Architecture:** A strict YAML `CourseExtractionProfile` is compiled into all course extraction prompts and post-processing rules. The course pipeline records the profile identity and hash in its checkpoint fingerprint. A separate evaluator compares Course Graph v1 output with a caller-owned, versioned Gold Dataset and emits deterministic quality metrics and threshold decisions.

**Tech stack:** Python 3.11, Pydantic v2, PyYAML, Typer, pytest.

---

## Task 1: Define And Validate Course Profiles

**Files:**
- Create: `hyperextract/profiles/course.py`
- Create: `hyperextract/profiles/defaults/course-knowledge-default.yaml`
- Create: `tests/profiles/test_course_profile.py`
- Modify: `pyproject.toml`

1. Write tests for strict schema validation, unknown fields, relation direction, allowed enums, deterministic content hashes, and the built-in profile.
2. Implement the schema and loader.
3. Compile node, local-edge, global-edge, dedup, and community prompts from one profile.
4. Run the focused tests and Ruff.

## Task 2: Wire Profiles Into Course Extraction

**Files:**
- Modify: `hyperextract/methods/rag/course_knowledge_graph.py`
- Modify: `hyperextract/documents/course_pipeline.py`
- Modify: `hyperextract/cli/cli.py`
- Modify: `tests/documents/test_course_pipeline.py`
- Modify: `tests/methods/test_course_knowledge_graph.py`

1. Write tests proving custom rules reach all prompt stages and profile identity enters the checkpoint.
2. Add `--profile` to structured course parsing.
3. Record profile name, version, hash, and compiled prompt hash in run configuration and final artifacts.
4. Ensure a profile change rejects incompatible checkpoint reuse.

## Task 3: Add Profile Inspection Commands

**Files:**
- Create: `hyperextract/cli/commands/profile.py`
- Modify: `hyperextract/cli/commands/__init__.py`
- Modify: `hyperextract/cli/cli.py`
- Create: `tests/cli/test_profile_commands.py`

1. Add failing CLI tests for `he profile validate` and `he profile render --stage`.
2. Implement deterministic human-readable and JSON output without initializing model clients.
3. Document the commands in Chinese and English.

## Task 4: Implement Gold Dataset Evaluation

**Files:**
- Create: `hyperextract/evaluation/course_profile.py`
- Create: `hyperextract/cli/commands/evaluate.py`
- Create: `tests/evaluation/test_course_profile.py`
- Create: `tests/cli/test_evaluate_course_profile.py`

1. Define a strict, versioned Gold Dataset schema with required, acceptable, forbidden nodes, aliases, outline IDs, evidence, key relations, and annotator decisions.
2. Implement deterministic alias-aware node and relation matching.
3. Report required recall, effective precision, forbidden leakage, outline accuracy, extractable outline coverage, evidence coverage, duplicate rate, key relation precision/recall, and annotator agreement.
4. Evaluate thresholds and return a non-zero exit code when quality gates fail.

## Task 5: Prepare Caller-Owned Chapter 2 Gold Seed

**Files in product-design:**
- Create: `data/test-fixtures/pmpbok-chapter-2/PMPBOK_CH_2.gold.json`
- Create: `docs/testing/hyper-extract-course-gold-annotation-guide.md`
- Modify: `package.json`

1. Freeze representative Chapter 2 content units and provenance.
2. Add independently justified required, acceptable, and forbidden candidates.
3. Add key prerequisite, derivative, and confusable relations; treat broad `related` edges as invalid unless explicitly evidenced.
4. Add a caller-side validation/evaluation script.

## Task 6: Run Fixed MiniMax Baseline

1. Validate MiniMax text/Thinking JSON compatibility separately from knowledge quality.
2. Run the fixed Chapter 2 package with MiniMax and the built-in profile.
3. Save the report next to the run artifacts.
4. Record baseline gaps before P1-2 performance changes; do not substitute node count for quality.

