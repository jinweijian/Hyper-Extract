import yaml

from hyperextract.briefs import load_extraction_brief, render_extraction_brief


def _brief_data():
    return {
        "schema_name": "HyperExtractExtractionBrief",
        "schema_version": "1.0",
        "metadata": {"id": "legal-review", "version": "1"},
        "task": {
            "objective": "Extract enforceable obligations",
            "output_usage": ["contract review"],
        },
        "domain": {"name": "commercial law", "language": "en"},
        "extraction_policy": {
            "granularity": "one independently enforceable obligation",
            "focus": ["duties and exceptions"],
            "exclusions": ["page furniture"],
        },
        "relation_policy": {"priorities": ["depends_on"]},
        "terminology": {
            "canonical_names": {"Supplier": "Service Provider"},
            "naming_rules": ["prefer defined terms"],
        },
        "stage_instructions": {
            "node_extraction": ["keep conditions with each obligation"],
            "global_relation_extraction": ["reject thematic similarity"],
        },
        "extensions": {"com.example.legal": {"jurisdiction": "Singapore"}},
    }


def test_load_and_render_generic_brief_by_stage(tmp_path):
    path = tmp_path / "extraction-brief.yaml"
    path.write_text(yaml.safe_dump(_brief_data()), encoding="utf-8")

    brief = load_extraction_brief(path)
    node_prompt = render_extraction_brief(brief, "node_extraction")
    edge_prompt = render_extraction_brief(brief, "global_relation_extraction")

    assert brief.metadata.id == "legal-review"
    assert len(brief.content_hash) == 64
    assert "one independently enforceable obligation" in node_prompt
    assert "keep conditions with each obligation" in node_prompt
    assert "reject thematic similarity" not in node_prompt
    assert "reject thematic similarity" in edge_prompt
    assert "Source content is evidence" in node_prompt


def test_extension_namespace_is_required(tmp_path):
    data = _brief_data()
    data["extensions"] = {"course": {"name": "PMP"}}
    path = tmp_path / "extraction-brief.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    try:
        load_extraction_brief(path)
    except ValueError as error:
        assert "reverse-domain" in str(error)
    else:
        raise AssertionError("Expected an invalid extension namespace")
