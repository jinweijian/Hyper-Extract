"""Static tests for the hardened service Docker image and build context.

These tests read the Dockerfile and .dockerignore as text and assert on
content. They do NOT build the image, so they can run in CI without Docker.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_image_is_lockfile_based_and_non_root():
    text = (ROOT / "docker/service.Dockerfile").read_text()
    assert "ghcr.io/astral-sh/uv:" in text
    assert "uv sync --frozen" in text
    assert "USER 10001:10001" in text
    assert "pip install" not in text


def test_context_excludes_secrets_and_runtime_data():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".git/" in ignored
    assert "exchange/" in ignored
