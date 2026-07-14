from pathlib import Path

import pytest

from hyperextract.service.settings import ServiceSettings


def test_settings_derive_exchange_roots(monkeypatch):
    monkeypatch.setenv("HE_SERVICE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("HE_SERVICE_EXCHANGE_ROOT", "/exchange")
    settings = ServiceSettings.from_env()
    assert settings.package_root == Path("/exchange/packages")
    assert settings.run_root == Path("/exchange/runs")


def test_settings_reject_relative_exchange_root(monkeypatch):
    monkeypatch.setenv("HE_SERVICE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("HE_SERVICE_EXCHANGE_ROOT", "relative")
    with pytest.raises(ValueError, match="absolute"):
        ServiceSettings.from_env()


def test_settings_drops_unused_retention_and_cleanup_fields(monkeypatch):
    # No cleanup process consumes these, so they must not be exposed as active
    # configuration. Operators who set the env vars must not believe they take
    # effect.
    monkeypatch.setenv("HE_SERVICE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("HE_SERVICE_EXCHANGE_ROOT", "/exchange")
    monkeypatch.setenv("HE_SERVICE_ARTIFACT_RETENTION_DAYS", "30")
    monkeypatch.setenv("HE_SERVICE_CLEANUP_INTERVAL_SECONDS", "3600")
    settings = ServiceSettings.from_env()
    assert not hasattr(settings, "artifact_retention_days")
    assert not hasattr(settings, "cleanup_interval_seconds")
