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
