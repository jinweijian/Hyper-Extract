import pytest

from hyperextract.providers.scheduling import CircuitBreaker, CircuitOpenError


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
