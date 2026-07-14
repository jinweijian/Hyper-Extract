import json

from typer.testing import CliRunner

from hyperextract.cli.cli import app
from tests.evaluation.test_course_profile import _dataset, _graph


runner = CliRunner()


def test_evaluate_course_profile_writes_report_and_passes(tmp_path):
    graph = tmp_path / "course-graph.json"
    dataset = tmp_path / "gold.json"
    output = tmp_path / "evaluation.json"
    graph.write_text(json.dumps(_graph()), encoding="utf-8")
    dataset.write_text(json.dumps(_dataset()), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "evaluate",
            "course-profile",
            "--dataset",
            str(dataset),
            "--graph",
            str(graph),
            "--profile",
            "hyperextract/profiles/defaults/course-knowledge-default.yaml",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["metrics"]["required_recall"] == 1


def test_evaluate_course_profile_returns_nonzero_when_gate_fails(tmp_path):
    graph_data = _graph()
    graph_data["knowledge_nodes"] = []
    graph_data["semantic_edges"] = []
    graph = tmp_path / "course-graph.json"
    dataset = tmp_path / "gold.json"
    graph.write_text(json.dumps(graph_data), encoding="utf-8")
    dataset.write_text(json.dumps(_dataset()), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "evaluate",
            "course-profile",
            "--dataset",
            str(dataset),
            "--graph",
            str(graph),
        ],
    )

    assert result.exit_code == 2
    assert "FAIL" in result.output
