import pytest

from hyperextract.service.settings import ServiceSettings


def test_upload_limit_defaults_to_configurable_500_mb(monkeypatch):
    monkeypatch.setenv("HE_SERVICE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.delenv("HE_SERVICE_MAX_UPLOAD_BYTES", raising=False)
    assert ServiceSettings.from_env().max_upload_bytes == 500_000_000

    monkeypatch.setenv("HE_SERVICE_MAX_UPLOAD_BYTES", "123456")
    assert ServiceSettings.from_env().max_upload_bytes == 123456


def test_pipeline_max_workers_defaults_to_two_and_is_configurable(monkeypatch):
    monkeypatch.setenv("HE_SERVICE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.delenv("HE_SERVICE_PIPELINE_MAX_WORKERS", raising=False)
    assert ServiceSettings.from_env().pipeline_max_workers == 2

    monkeypatch.setenv("HE_SERVICE_PIPELINE_MAX_WORKERS", "4")
    assert ServiceSettings.from_env().pipeline_max_workers == 4


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_pipeline_max_workers_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("HE_SERVICE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("HE_SERVICE_PIPELINE_MAX_WORKERS", value)

    with pytest.raises(ValueError, match="HE_SERVICE_PIPELINE_MAX_WORKERS"):
        ServiceSettings.from_env()
