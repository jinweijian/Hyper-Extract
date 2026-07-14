from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """Thread-safe closed/open/half-open breaker for one rate-limit group."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0, cooldown_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._half_open_in_flight = False

    @contextmanager
    def call(self) -> Iterator[None]:
        self.before_request()
        try:
            yield
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()

    def before_request(self) -> None:
        with self._lock:
            now = self._clock()
            if self._open_until > now:
                raise CircuitOpenError("rate-limit-group circuit is open")
            if self._open_until and self._half_open_in_flight:
                raise CircuitOpenError("rate-limit-group circuit is half-open")
            if self._open_until:
                self._half_open_in_flight = True

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open_in_flight = False
            if self._failures >= self.failure_threshold:
                self._open_until = self._clock() + self.cooldown_seconds

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0
            self._half_open_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._open_until > self._clock():
                return "open"
            if self._open_until:
                return "half_open"
            return "closed"


class TokenBucket:
    """Monotonic token bucket returning the wait needed for a requested amount."""

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = max(1.0, capacity)
        self.refill_per_second = max(0.000001, refill_per_second)
        self._tokens = self.capacity
        self._clock = clock
        self._updated_at = clock()

    def wait_time(self, amount: float) -> float:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._tokens = min(
            self.capacity, self._tokens + elapsed * self.refill_per_second
        )
        self._updated_at = now
        required = min(self.capacity, max(0.0, amount))
        if self._tokens >= required:
            return 0.0
        return (required - self._tokens) / self.refill_per_second

    def consume(self, amount: float) -> None:
        required = min(self.capacity, max(0.0, amount))
        self._tokens = max(0.0, self._tokens - required)


@dataclass(frozen=True)
class SchedulerSnapshot:
    group: str
    configured_concurrency: int
    effective_concurrency: int
    in_flight: int
    queue_length: int
    paused_until: float
    rate_limit_events: int
    concurrency_reductions: int


class RateLimitGroupScheduler:
    """Process-local AIMD concurrency and pause coordination for one quota group."""

    def __init__(
        self,
        group: str,
        *,
        max_concurrency: int,
        recommended_concurrency: int,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.group = group
        self.configured_concurrency = max(1, max_concurrency)
        self.effective_concurrency = max(
            1, min(self.configured_concurrency, recommended_concurrency)
        )
        self._clock = clock
        self._condition = threading.Condition()
        self._in_flight = 0
        self._waiters = 0
        self._paused_until = 0.0
        self._rate_limit_events = 0
        self._concurrency_reductions = 0
        self._successes = 0
        self._request_bucket = (
            TokenBucket(
                requests_per_minute,
                requests_per_minute / 60,
                clock=clock,
            )
            if requests_per_minute
            else None
        )
        self._token_bucket = (
            TokenBucket(tokens_per_minute, tokens_per_minute / 60, clock=clock)
            if tokens_per_minute
            else None
        )

    @contextmanager
    def slot(self, *, estimated_tokens: int = 0) -> Iterator[None]:
        self.acquire(estimated_tokens=estimated_tokens)
        try:
            yield
        finally:
            self.release()

    def acquire(self, *, estimated_tokens: int = 0) -> None:
        with self._condition:
            self._waiters += 1
            try:
                while True:
                    remaining = self._paused_until - self._clock()
                    has_concurrency = self._in_flight < self.effective_concurrency
                    bucket_wait = (
                        self._bucket_wait(estimated_tokens) if has_concurrency else 0
                    )
                    if remaining <= 0 and bucket_wait <= 0 and has_concurrency:
                        self._consume_buckets(estimated_tokens)
                        self._in_flight += 1
                        return
                    waits = [value for value in (remaining, bucket_wait) if value > 0]
                    self._condition.wait(
                        timeout=max(0.001, max(waits)) if waits else None
                    )
            finally:
                self._waiters -= 1

    def _bucket_wait(self, estimated_tokens: int) -> float:
        request_wait = self._request_bucket.wait_time(1) if self._request_bucket else 0
        token_wait = (
            self._token_bucket.wait_time(estimated_tokens)
            if self._token_bucket and estimated_tokens > 0
            else 0
        )
        return max(request_wait, token_wait)

    def _consume_buckets(self, estimated_tokens: int) -> None:
        if self._request_bucket:
            self._request_bucket.consume(1)
        if self._token_bucket and estimated_tokens > 0:
            self._token_bucket.consume(estimated_tokens)

    def release(self) -> None:
        with self._condition:
            self._in_flight = max(0, self._in_flight - 1)
            self._condition.notify_all()

    def rate_limited(self, delay_seconds: float) -> None:
        with self._condition:
            self._rate_limit_events += 1
            previous = self.effective_concurrency
            self.effective_concurrency = max(1, self.effective_concurrency // 2)
            if self.effective_concurrency < previous:
                self._concurrency_reductions += 1
            self._paused_until = max(self._paused_until, self._clock() + delay_seconds)
            self._successes = 0
            self._condition.notify_all()

    def succeeded(self, *, stable_window: int = 20) -> None:
        with self._condition:
            self._successes += 1
            if (
                self._successes >= stable_window
                and self.effective_concurrency < self.configured_concurrency
            ):
                self.effective_concurrency += 1
                self._successes = 0
                self._condition.notify_all()

    def snapshot(self) -> SchedulerSnapshot:
        with self._condition:
            return SchedulerSnapshot(
                group=self.group,
                configured_concurrency=self.configured_concurrency,
                effective_concurrency=self.effective_concurrency,
                in_flight=self._in_flight,
                queue_length=self._waiters,
                paused_until=self._paused_until,
                rate_limit_events=self._rate_limit_events,
                concurrency_reductions=self._concurrency_reductions,
            )


class SchedulerRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: dict[str, RateLimitGroupScheduler] = {}

    def get(
        self,
        group: str,
        *,
        max_concurrency: int,
        recommended_concurrency: int,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
    ) -> RateLimitGroupScheduler:
        with self._lock:
            scheduler = self._groups.get(group)
            if scheduler is None:
                scheduler = RateLimitGroupScheduler(
                    group,
                    max_concurrency=max_concurrency,
                    recommended_concurrency=recommended_concurrency,
                    requests_per_minute=requests_per_minute,
                    tokens_per_minute=tokens_per_minute,
                )
                self._groups[group] = scheduler
            return scheduler


PROCESS_SCHEDULERS = SchedulerRegistry()
