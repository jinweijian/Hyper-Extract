# P1-2 Course Pipeline Performance Implementation Plan

**Goal:** Reduce model calls, repeated context, global-relation candidates, and elapsed time while preserving P1-1 Gold Dataset quality within two percentage points.

**Baseline:** The PMPBOK Chapter 2 P0-3 MiniMax run produced 44 nodes and 131 semantic edges. Gold evaluation reports 96.67% required recall, 100% candidate precision, 100% extractable-outline coverage, 40% key-relation precision, and zero reviewed annotator pairs. The relation stage is the main quality and cost problem.

## Task 1: Combine Local Extraction

- Compile a single node-plus-local-edge prompt from `CourseExtractionProfile`.
- Add a strict `CourseChunkResult` schema.
- Persist one idempotent chunk result instead of separate node and edge calls.
- Retain the separate mode as an explicit compatibility fallback.

## Task 2: Compress Repeated Context

- Render all top-level headings plus the complete current top-level chapter outline.
- Send current path, source, and body without repeating unrelated chapters' descendants.
- Limit known terminology to a small stable set.
- Continue enforcing the total context budget.

## Task 3: Restrict Global Relations

- Exclude same-section pairs already handled locally.
- Never create automatic candidates across top-level chapters.
- Require a configurable similarity threshold and strict per-node TopK.
- Preserve deterministic ordering and checkpoint keys.

## Task 4: Measure Calls And Tokens

- Record every structured model call, operation, mode, elapsed time, estimated or provider-reported tokens, failures, and repairs.
- Persist usage after every request so interrupted runs retain evidence.
- Include totals and per-stage metrics in the run summary.

## Task 5: Compare Against The Fixed Chapter Proxy

- Run the same MiniMax route, package, Profile, and Gold Dataset.
- Compare model calls, input tokens, elapsed time, candidate count, and quality metrics.
- Reject the optimization if required recall, effective precision, outline coverage, or relation precision drops by more than two percentage points.

