# ExtractionBrief

`ExtractionBrief` is a caller-owned, run-scoped semantic intent contract. It tells HE why the extraction is being performed, how to interpret the source, which granularity and terminology to prefer, and which stage-specific rules apply. It is not raw source content, a model configuration, or a replacement for the extraction profile.

## Package boundary

Production callers must use Document Package `1.1` and place the YAML file inside the package. External paths and request-level prompt strings are intentionally unsupported.

```text
book.hepkg/
  manifest.json
  extraction-brief.yaml
  outline.json
  provenance.jsonl
  content/*.md
```

The manifest declares and protects the artifact:

```json
{
  "schema_version": "1.1",
  "extraction_brief": {
    "path": "extraction-brief.yaml",
    "sha256": "<sha256 of exact YAML bytes>",
    "bytes": 2048
  }
}
```

HE validates the path, suffix, size, byte count, SHA-256, YAML syntax, and `HyperExtractExtractionBrief` schema before model initialization. The normalized brief is included in the package fingerprint, prompt fingerprint, and checkpoint identity.

## Generic schema

```yaml
schema_name: HyperExtractExtractionBrief
schema_version: "1.0"
metadata:
  id: example-extraction
  version: "1.0"
  description: Example caller intent
task:
  objective: Extract independently useful knowledge supported by the source
  output_usage: [navigation]
  target_audience: [domain users]
domain:
  name: example domain
  description: Optional domain framing
  language: en
source:
  document_type: handbook
  title: Example Handbook
  role: primary source
  authority: official publication
  interpretation: Preserve declared headings as source structure
extraction_policy:
  granularity: one independently explainable item
  focus: [defined concepts, methods]
  exclusions: [page furniture]
  preserve_source_hierarchy: true
  evidence_required: true
relation_policy:
  priorities: [prerequisite]
  allowed: []
  forbidden: []
  require_evidence: true
terminology:
  canonical_names: {}
  aliases: {}
  naming_rules: [prefer source-defined terms]
stage_instructions:
  node_extraction: []
  local_relation_extraction: []
  deduplication: []
  global_relation_extraction: []
  community: []
  evaluation: []
additional_instructions: []
extensions:
  com.example.domain: {}
```

Extension keys must use a bounded reverse-domain namespace. HE transports extension values into stage system messages but does not assign domain meaning to them.

## Prompt compilation

When a brief is present, HE builds two-message prompts:

1. **System message:** HE evidence/output contract, extraction profile constraints, and the stage projection of the brief.
2. **User message:** package-derived outline context, known nodes or candidates, and source text.

Node extraction does not receive global-relation-only instructions. Deduplication receives terminology policy, while relation stages receive relation policy. The brief can narrow or clarify extraction, but cannot override the output schema, evidence requirements, or create facts absent from the source.

The run stores normalized and YAML snapshots under `.he-run/`, plus one compiled prompt template per model stage. Changing the brief changes package and prompt fingerprints, so `--resume` cannot silently reuse results produced under different intent.
