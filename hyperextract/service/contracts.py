from hyperextract.documents import document_package_schemas


def document_package_contract() -> dict[str, object]:
    return {
        "schema_name": "HyperExtractDocumentPackage",
        "schema_version": "1.0",
        "required_entries": [
            {"path": "manifest.json", "kind": "file"},
            {"path": "outline.json", "kind": "file"},
            {"path": "provenance.jsonl", "kind": "file"},
            {"path": "content/", "kind": "directory"},
        ],
        "schemas": document_package_schemas(),
    }
