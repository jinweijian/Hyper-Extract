import json

from typer.testing import CliRunner

from hyperextract.cli.cli import app


runner = CliRunner()


def test_profile_validate_emits_machine_readable_identity():
    result = runner.invoke(
        app,
        [
            "profile",
            "validate",
            "hyperextract/profiles/defaults/course-knowledge-default.yaml",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["name"] == "course-knowledge-default"
    assert payload["version"] == "1.1.0"
    assert len(payload["content_hash"]) == 64


def test_profile_render_prints_requested_compiled_stage():
    result = runner.invoke(
        app,
        [
            "profile",
            "render",
            "hyperextract/profiles/defaults/course-knowledge-default.yaml",
            "--stage",
            "global-edges",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "仅主题相近" in result.output
    assert "{candidates}" in result.output
    assert "文档上下文" not in result.output
