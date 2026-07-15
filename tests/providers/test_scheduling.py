import pytest

from hyperextract.providers.scheduling import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
    SchedulerRegistry,
)
from hyperextract.providers.contracts import ProfileConfigurationError


def test_circuit_breaker_opens_then_half_opens_and_recovers():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=10,
        clock=lambda: now[0],
    )
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_request()
    now[0] = 11
    assert breaker.state == "half_open"
    breaker.before_request()
    with pytest.raises(CircuitOpenError):
        breaker.before_request()
    breaker.record_success()
    assert breaker.state == "closed"


def test_circuit_breaker_registry_shares_state_only_within_group():
    registry = CircuitBreakerRegistry()
    first = registry.get("shared")
    second = registry.get("shared")
    isolated = registry.get("isolated")

    assert first is second
    assert first is not isolated


def test_scheduler_registry_rejects_first_configuration_wins_drift():
    registry = SchedulerRegistry()
    registry.get(
        "shared",
        max_concurrency=4,
        recommended_concurrency=2,
        requests_per_minute=60,
    )

    with pytest.raises(ProfileConfigurationError) as error:
        registry.get(
            "shared",
            max_concurrency=4,
            recommended_concurrency=1,
            requests_per_minute=None,
        )

    assert error.value.code == "RATE_LIMIT_GROUP_CONFIG_CONFLICT"
