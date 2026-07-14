from types import SimpleNamespace

from typer.testing import CliRunner

from hyperextract.cli.cli import app


def test_malformed_document_package_fails_before_model_creation(tmp_path, monkeypatch):
    package = tmp_path / "bad.hepkg"
    package.mkdir()
    (package / "manifest.json").write_text('{"schema_name":"wrong"}', encoding="utf-8")
    created = []

    monkeypatch.setattr("hyperextract.cli.cli.validate_config", lambda: None)
    monkeypatch.setattr(
        "hyperextract.cli.cli.Template.get",
        lambda _name: SimpleNamespace(name="course_knowledge_graph", language="zh"),
    )
    monkeypatch.setattr(
        "hyperextract.cli.cli.Template.create",
        lambda *_args, **_kwargs: created.append(True),
    )

    result = CliRunner().invoke(
        app,
        [
            "parse",
            str(package),
            "--method",
            "course_knowledge_graph",
            "--output",
            str(tmp_path / "output"),
            "--input-format",
            "document-package",
            "--no-index",
        ],
    )

    assert result.exit_code == 1
    assert "Invalid Document Package" in result.output
    assert created == []
