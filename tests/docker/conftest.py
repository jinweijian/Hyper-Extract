"""Fixtures for static Docker Compose / Dockerfile tests.

These tests parse the Compose file as text/YAML and assert on topology. They
do NOT run Docker, so they are safe to execute in CI without a Docker daemon.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker" / "service.compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parse ``docker/service.compose.yml`` into a plain dict."""
    return yaml.safe_load(COMPOSE_PATH.read_text())
