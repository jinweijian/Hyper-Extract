from hyperextract.documents import document_package_schemas


def document_package_contract() -> dict[str, object]:
    return {
        "schema_name": "HyperExtractDocumentPackage",
        "schema_version": "1.1",
        "supported_versions": ["1.0", "1.1"],
        "required_entries": [
            {"path": "manifest.json", "kind": "file"},
            {"path": "outline.json", "kind": "file"},
            {"path": "provenance.jsonl", "kind": "file"},
            {"path": "content/", "kind": "directory"},
        ],
        "version_requirements": {
            "1.0": {"extraction_brief": "not_required"},
            "1.1": {
                "extraction_brief": "required",
                "location": "manifest.extraction_brief.path",
                "formats": ["yaml", "yml"],
            },
        },
        "schemas": document_package_schemas(),
    }
