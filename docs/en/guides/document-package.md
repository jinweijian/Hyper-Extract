# Document Package v1

Document Package is the stable boundary between a document parser and Hyper-Extract. The producer may be local Docling, a remote Docling API, or another parser. Hyper-Extract does not install, start, or call that parser.

The Document Package **directory** is the content contract. For cross-service transport, callers pack it into a `.hepkg.tar.gz` archive whose root **directly** contains `manifest.json`, `outline.json`, `provenance.jsonl`, `extraction-brief.yaml`, and `content/` — with no nested top-level directory. Two distinct hashes apply: `package_fingerprint` (the canonical SHA-256 of the *extracted* package) and `transport_sha256` (the SHA-256 of the tarball byte stream). They are validated independently and must not be substituted for one another.

## Layout

```text
book.hepkg/
  manifest.json
  extraction-brief.yaml  # required by schema 1.1
  outline.json
  provenance.jsonl
  content/*.md
```

`manifest.json` uses `schema_name: HyperExtractDocumentPackage`. Version `1.0` remains readable for compatibility. Version `1.1` additionally requires an `extraction_brief` artifact with a package-relative YAML path, SHA-256, and byte count. Every content entry declares its ID, relative path, order, content kind, outline ID, SHA-256, byte count, and extraction policy.

The package-owned [ExtractionBrief](extraction-brief.md) is the only supported caller system-instruction channel. It is validated and fingerprinted with the document; request-level prompt strings and external brief paths are not accepted.

Supported content kinds are `body`, `table_of_contents`, `appendix`, `references`, `index`, `front_matter`, `back_matter`, and `other`. Only entries with `extract=true` reach chunk planning and model extraction, while every declared file is validated.

`outline.json` uses `HyperExtractOutline` version `1.0`. Nodes contain `id`, `title`, `depth`, `parent_id`, `order`, and `source_refs`. The producer owns outline recognition; HE preserves the supplied hierarchy instead of asking a model to infer it again.

`provenance.jsonl` contains one record per content entry:

```json
{"content_id":"content-2-1","source_refs":[{"ref":"book.md#L20-L42","source_path":"book.md","start_line":20,"end_line":42}]}
```

## Validation

Before model initialization, HE rejects unsupported versions, missing or modified files, unsafe paths, symlinks, duplicate identities or order values, orphaned or cyclic outlines, invalid content references, provenance mismatches, and packages exceeding configured file or byte limits. Undeclared files are ignored.

## Run

```bash
he parse ./book.hepkg \
  -m course_knowledge_graph \
  -o ./book-course-graph \
  --input-format document-package \
  --resume \
  --no-index
```

The normalized package fingerprint, including the v1.1 brief, is part of checkpoint identity. `docling-json` remains available for migration, but new production integrations should produce a Document Package v1.1.
