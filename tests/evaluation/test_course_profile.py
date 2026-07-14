import json

from hyperextract.evaluation.course_profile import evaluate_course_profile


def _graph():
    return {
        "schema_name": "HyperExtractCourseGraph",
        "schema_version": "1.0",
        "run_id": "run-1",
        "profile_version": "1.0.0",
        "outline_nodes": [
            {
                "id": "root",
                "name": "Course",
                "node_type": "book",
                "depth": 0,
                "parent_id": None,
                "order": 0,
                "source_refs": [],
            },
            {
                "id": "section-a",
                "name": "Section A",
                "node_type": "section",
                "depth": 1,
                "parent_id": "root",
                "order": 1,
                "source_refs": [],
            },
        ],
        "knowledge_nodes": [
            {
                "id": "kp-a",
                "name": "Alpha concept",
                "level": "point",
                "parent_outline_id": "section-a",
                "summary": "Alpha definition",
                "evidence": "Alpha evidence",
                "source_refs": [{"ref": "source.md#L1-L2"}],
                "profile_version": "1.0.0",
                "run_id": "run-1",
                "aliases": [],
                "confidence": 0.9,
            },
            {
                "id": "kp-b",
                "name": "Beta alias",
                "level": "point",
                "parent_outline_id": "section-a",
                "summary": "Beta definition",
                "evidence": "Beta evidence",
                "source_refs": [{"ref": "source.md#L3-L4"}],
                "profile_version": "1.0.0",
                "run_id": "run-1",
                "aliases": [],
                "confidence": 0.9,
            },
        ],
        "structural_edges": [],
        "semantic_edges": [
            {
                "source_id": "kp-a",
                "target_id": "kp-b",
                "edge_type": "prerequisite",
                "evidence": "Alpha is required before Beta",
                "source_refs": [{"ref": "source.md#L1-L4"}],
                "confidence": 0.9,
                "status": "pending",
            }
        ],
    }


def _dataset():
    return {
        "schema_name": "HyperExtractCourseGoldDataset",
        "schema_version": "1.0",
        "dataset_id": "sample",
        "version": "1.0.0",
        "source": {"document_package_sha256": "a" * 64},
        "thresholds": {
            "required_recall": 0.85,
            "effective_precision": 0.9,
            "extractable_outline_coverage": 0.85,
            "forbidden_leakage_rate": 0,
            "duplicate_rate": 0.05,
            "relation_precision": 0.8,
            "annotator_agreement": 0.8,
        },
        "nodes": [
            {
                "id": "gold-a",
                "label": "required",
                "canonical_name": "Alpha concept",
                "aliases": [],
                "outline_id": "section-a",
                "evidence": "Alpha evidence",
                "rationale": "Explicitly taught",
                "annotations": [
                    {"annotator": "reviewer-a", "label": "required"},
                    {"annotator": "reviewer-b", "label": "required"},
                ],
            },
            {
                "id": "gold-b",
                "label": "acceptable",
                "canonical_name": "Beta concept",
                "aliases": ["Beta alias"],
                "outline_id": "section-a",
                "evidence": "Beta evidence",
                "rationale": "Useful standalone concept",
                "annotations": [
                    {"annotator": "reviewer-a", "label": "acceptable"},
                    {"annotator": "reviewer-b", "label": "acceptable"},
                ],
            },
            {
                "id": "gold-noise",
                "label": "forbidden",
                "canonical_name": "Index entry",
                "aliases": [],
                "outline_id": None,
                "evidence": "Index entry, 123",
                "rationale": "Index noise",
                "annotations": [
                    {"annotator": "reviewer-a", "label": "forbidden"},
                    {"annotator": "reviewer-b", "label": "forbidden"},
                ],
            },
        ],
        "relations": [
            {
                "id": "relation-a-b",
                "label": "required",
                "source": "gold-a",
                "target": "gold-b",
                "relation_type": "prerequisite",
                "evidence": "Alpha is required before Beta",
                "rationale": "Explicit dependency",
            }
        ],
    }


def test_evaluator_matches_aliases_outlines_relations_and_annotations(tmp_path):
    graph_path = tmp_path / "course-graph.json"
    dataset_path = tmp_path / "gold.json"
    graph_path.write_text(json.dumps(_graph()), encoding="utf-8")
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")

    report = evaluate_course_profile(dataset_path, graph_path)

    assert report.metrics.required_recall == 1
    assert report.metrics.effective_precision == 1
    assert report.metrics.forbidden_leakage_rate == 0
    assert report.metrics.outline_accuracy == 1
    assert report.metrics.extractable_outline_coverage == 1
    assert report.metrics.evidence_coverage == 1
    assert report.metrics.relation_precision == 1
    assert report.metrics.relation_recall == 1
    assert report.metrics.annotator_agreement == 1
    assert report.passed is True


def test_evaluator_reports_unknown_nodes_as_false_positives_and_forbidden_leaks(
    tmp_path,
):
    graph = _graph()
    graph["knowledge_nodes"].append(
        {
            **graph["knowledge_nodes"][0],
            "id": "kp-noise",
            "name": "Index entry",
        }
    )
    graph["knowledge_nodes"].append(
        {
            **graph["knowledge_nodes"][0],
            "id": "kp-unknown",
            "name": "Invented item",
        }
    )
    graph_path = tmp_path / "course-graph.json"
    dataset_path = tmp_path / "gold.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")

    report = evaluate_course_profile(dataset_path, graph_path)

    assert report.metrics.effective_precision == 0.5
    assert report.metrics.forbidden_leakage_rate == 1
    assert report.passed is False
    assert "Index entry" in report.forbidden_leaks
    assert "Invented item" in report.unmatched_predictions


def test_evaluator_prefers_scoped_positive_alias_over_generic_forbidden_name(
    tmp_path,
):
    graph = _graph()
    graph["knowledge_nodes"] = [
        {
            **graph["knowledge_nodes"][0],
            "name": "Working method",
        }
    ]
    graph["semantic_edges"] = []
    dataset = _dataset()
    dataset["nodes"] = [
        {
            **dataset["nodes"][0],
            "canonical_name": "Ways of working capability",
            "aliases": ["Working method"],
        },
        {
            **dataset["nodes"][2],
            "canonical_name": "Working method",
        },
    ]
    dataset["relations"] = []
    graph_path = tmp_path / "course-graph.json"
    dataset_path = tmp_path / "gold.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    report = evaluate_course_profile(dataset_path, graph_path)

    assert report.metrics.required_recall == 1
    assert report.metrics.forbidden_leakage_rate == 0
