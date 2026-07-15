import pytest

from hyperextract.documents.model_errors import ModelInvocationError
from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    ProfileConfigurationError,
    RecoveryDecision,
)
from hyperextract.providers.gateway import GatewayExecutionError
from hyperextract.service.errors import normalize_failure


def test_profile_configuration_code_is_preserved_and_not_resumable():
    error = ProfileConfigurationError(
        "profile changed",
        code="MODEL_PROFILE_FINGERPRINT_MISMATCH",
    )

    code, _, resumable, _ = normalize_failure(error)

    assert code == "MODEL_PROFILE_FINGERPRINT_MISMATCH"
    assert resumable is False


def test_wrapped_gateway_authentication_uses_typed_failure_metadata():
    gateway_error = GatewayExecutionError(
        "credential rejected",
        failure=CanonicalModelFailure(
            request_id="r",
            category="authentication",
            reason="invalid_credentials",
        ),
        decision=RecoveryDecision(
            action="fail",
            target="run",
            reason="authentication",
            consume_attempt=False,
        ),
    )
    error = ModelInvocationError("model failed", original=gateway_error)

    code, _, resumable, _ = normalize_failure(error)

    assert code == "MODEL_AUTHENTICATION_FAILED"
    assert resumable is False


def test_gateway_retry_exhaustion_is_resumable_with_stable_code():
    error = GatewayExecutionError(
        "provider unavailable",
        failure=CanonicalModelFailure(
            request_id="r",
            category="transient",
            reason="server_error",
        ),
        decision=RecoveryDecision(
            action="fail",
            target="request",
            reason="transient_retry_budget_exhausted",
            consume_attempt=False,
        ),
    )

    code, _, resumable, _ = normalize_failure(error)

    assert code == "MODEL_RETRY_EXHAUSTED"
    assert resumable is True


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("quota.permission", "MODEL_PERMISSION_DENIED"),
        ("quota.exhausted", "MODEL_QUOTA_EXHAUSTED"),
    ],
)
def test_quota_failures_are_not_reported_as_authentication(category, expected):
    error = GatewayExecutionError(
        category,
        failure=CanonicalModelFailure(
            request_id="r",
            category=category,
            reason=category,
        ),
        decision=RecoveryDecision(
            action="fail",
            target="run",
            reason=category,
            consume_attempt=False,
        ),
    )

    code, _, resumable, _ = normalize_failure(error)

    assert code == expected
    assert resumable is False
