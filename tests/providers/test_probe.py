from datetime import datetime, timedelta, timezone

import pytest

from hyperextract.providers.contracts import ProbeResult, ProfileConfigurationError
from hyperextract.providers.probe import ProbeStore, ensure_probe_eligibility
from hyperextract.providers.profiles import ModelProfile, ProfileCapabilities


def _profile(required=False):
    return ModelProfile(
        name="production",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="TEST_KEY",
        probe_required=required,
        capabilities=ProfileCapabilities(),
    )


def test_probe_timestamp_does_not_change_profile_execution_identity(tmp_path):
    profile = _profile()
    before = profile.public_fingerprint()
    now = datetime.now(timezone.utc)
    ProbeStore(tmp_path).save(
        ProbeResult(
            profile_fingerprint=before,
            probe_evidence_hash="a" * 64,
            checks={"text": True},
            probed_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    assert profile.public_fingerprint() == before


def test_required_profile_rejects_missing_or_expired_probe(tmp_path):
    with pytest.raises(ProfileConfigurationError) as error:
        ensure_probe_eligibility(_profile(required=True), ProbeStore(tmp_path))
    assert error.value.code == "PROBE_REQUIRED"
