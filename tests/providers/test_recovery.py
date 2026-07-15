import random

from hyperextract.providers.contracts import CanonicalModelFailure
from hyperextract.providers.profiles import ProfileRecovery
from hyperextract.providers.recovery import RecoveryPolicy, RecoveryState


def _failure(category, reason="test", retry_after=None):
    return CanonicalModelFailure(
        request_id="r1",
        category=category,
        reason=reason,
        retry_after_seconds=retry_after,
    )


def test_authentication_and_quota_fail_run_without_retry():
    policy = RecoveryPolicy()
    for category in ("authentication", "quota.exhausted", "quota.permission"):
        decision = policy.decide(_failure(category), RecoveryState())
        assert (decision.action, decision.target) == ("fail", "run")


def test_capability_fallback_is_explicit_and_budgeted():
    policy = RecoveryPolicy(ProfileRecovery(fallback_attempts=1))
    decision = policy.decide(
        _failure("unsupported_capability"),
        RecoveryState(),
        fallback_available=True,
    )
    assert decision.action == "fallback"
    exhausted = policy.decide(
        _failure("unsupported_capability"),
        RecoveryState(fallback_attempts=1),
        fallback_available=True,
    )
    assert exhausted.action == "fail"


def test_retry_after_is_never_shortened_by_jitter():
    policy = RecoveryPolicy(random_source=random.Random(3))
    decision = policy.decide(
        _failure("rate_limit.tokens", retry_after=17), RecoveryState()
    )
    assert decision.action == "retry"
    assert decision.delay_seconds >= 17


def test_failure_types_have_distinct_recovery_scopes():
    policy = RecoveryPolicy()
    assert policy.decide(_failure("context_window"), RecoveryState()).action == "replan"
    assert (
        policy.decide(_failure("output_truncated"), RecoveryState()).action == "split"
    )
    assert (
        policy.decide(_failure("invalid_item"), RecoveryState()).action == "quarantine"
    )
    assert (
        policy.decide(_failure("embedding_alignment"), RecoveryState()).target == "run"
    )
