# Course Long-document Knowledge Graph Pipeline

`course_knowledge_graph` converts a parser-neutral Document Package into a
course knowledge graph that preserves the global chapter structure, source
locations, and instructional relationships. It is a method-level capability
and does not depend on a particular product, business repository, or Docling
deployment.

## Input

Use Docling, a local parser, or a remote document service to produce the
outline and content, then publish a Document Package:

```text
book.hepkg/
  manifest.json
  outline.json
  provenance.jsonl
  content/
    0001.md
    0002.md
```

`manifest.json` uses `HyperExtractDocumentPackage` 1.0 or 1.1 and declares the
document, producer, outline, provenance, and the order, kind, outline owner,
SHA-256, byte size, and extraction policy of every content file. Package 1.1
also includes `extraction-brief.yaml`. `outline.json` uses
`HyperExtractOutline` 1.0. Before any model call, Hyper-Extract validates
versions, path boundaries, symbolic links, hashes, sizes, duplicate IDs,
parents, outline cycles, and content references. Undeclared files are ignored.

Direct Docling JSON input remains available for compatibility, but new
production integrations should prefer Document Package so parser adaptation
stays outside Hyper-Extract.

## Running

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

Named routing can configure multiple OpenAI-compatible services in one
`.env`. For example, with `HYPER_EXTRACT_LLM_PROFILE=MIMIMAX`, Hyper-Extract
reads `MIMIMAX_MODEL`, `MIMIMAX_API_KEY`, and `MIMIMAX_BASE_URL`. Embeddings
can independently use `HYPER_EXTRACT_EMBEDDING_PROFILE=EMBEDDING`. For models
without native JSON Schema support, use
`HYPER_EXTRACT_STRUCTURED_OUTPUT_MODE=text_json` to avoid extra capability
probing requests.

Do not add `--force` when resuming. It deletes the `.he-run` checkpoint and
starts over. The pipeline rejects incompatible checkpoints when inputs,
models, prompts, or material chunking options change.

## Stages

1. `ingest`: validate the package and load the global outline and sources.
2. `chunk_plan`: plan chapter-aware chunks with global outline context.
3. `local_extract`: extract concepts, then local instructional relationships.
4. `deduplicate`: combine exact matching, vector candidates, and model review.
5. `global_edges`: add prerequisite, related, derived, and confusion edges.
6. `quality`: check chapter coverage, relationship distribution, and orphans.
7. `communities`: run Louvain detection and optional topic summaries.
8. `finalize`: write native Hyper-Extract data and course graph artifacts.

## Checkpoints and monitoring

Run state is persisted in the output directory:

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

Long requests periodically write heartbeats to the terminal and
`events.jsonl`. Temporary network errors, rate limits, 5xx responses, timeouts,
and truncated structured output use exponential-backoff retries. Persistent
context-length errors split the current chunk further at paragraph boundaries.
Normal completion, failures, and catchable interruptions write
`run-summary.json`.

```bash
tail -f ./book-course-graph/.he-run/events.jsonl
cat ./book-course-graph/.he-run/run.json
cat ./book-course-graph/run-summary.json
```

## Output

- `course-graph.json`: chapter structure, concepts, relationships, and quality.
- `outline.json`: complete source outline and locations.
- `source-map.json`: concept-to-source and concept-to-chapter mapping.
- `merge-log.json`: exact and semantic deduplication decisions.
- `quality-report.json`: coverage, relationship distribution, and orphan data.
- `community_data.json`: knowledge communities and optional topic summaries.
- `data.json`, `metadata.json`: native Hyper-Extract knowledge-base files.
- `run-summary.json`: final status, stages, timing, and errors.
