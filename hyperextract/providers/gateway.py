from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from hyperextract.providers.adapters.base import AdapterError, GenerationAdapter
from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    GenerationRequest,
    GenerationResponse,
    OutputMode,
    RecoveryDecision,
)
from hyperextract.providers.profiles import ModelProfile
from hyperextract.providers.recovery import RecoveryPolicy, RecoveryState
from hyperextract.providers.scheduling import RateLimitGroupScheduler
from hyperextract.providers.scheduling import CircuitBreaker, CircuitOpenError


class GatewayExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure: CanonicalModelFailure,
        decision: RecoveryDecision,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.decision = decision


class GatewayEventSinkError(RuntimeError):
    """A completed model attempt could not be written to its audit log."""


@dataclass
class GatewayTrace:
    modes: list[OutputMode] = field(default_factory=list)
    decisions: list[RecoveryDecision] = field(default_factory=list)
    failures: list[CanonicalModelFailure] = field(default_factory=list)
    attempts: list[GatewayAttempt] = field(default_factory=list)
    recoveries: list[GatewayRecovery] = field(default_factory=list)


@dataclass
class GatewayAttempt:
    """One physical adapter invocation made by the gateway."""

    mode: OutputMode
    elapsed_seconds: float
    response: GenerationResponse | None = None
    error: Exception | None = None


@dataclass
class GatewayRecovery:
    """One canonical failure and the recovery decision made for it."""

    mode: OutputMode
    failure: CanonicalModelFailure
    decision: RecoveryDecision


class ModelExecutionGateway:
    """Provider-neutral generation execution with explicit bounded recovery."""

    version = "1"

    def __init__(
        self,
        adapter: GenerationAdapter,
        profile: ModelProfile,
        *,
        recovery_policy: RecoveryPolicy | None = None,
        scheduler: RateLimitGroupScheduler | None = None,
        sleep: Callable[[float], None] = time.sleep,
        event_sink: Callable[[dict], None] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.adapter = adapter
        self.profile = profile
        self.recovery_policy = recovery_policy or RecoveryPolicy(profile.recovery)
        self.scheduler = scheduler
        self._sleep = sleep
        self._event_sink = event_sink
        self.circuit_breaker = circuit_breaker
        self._local = threading.local()
        self.last_trace = GatewayTrace()

    @property
    def last_trace(self) -> GatewayTrace:
        trace = getattr(self._local, "trace", None)
        if trace is None:
            trace = GatewayTrace()
            self._local.trace = trace
        return trace

    @last_trace.setter
    def last_trace(self, value: GatewayTrace) -> None:
        self._local.trace = value

    def invoke(self, request: GenerationRequest) -> GenerationResponse:
        state = RecoveryState()
        trace = GatewayTrace()
        self.last_trace = trace
        modes = self._mode_chain(request)
        mode_index = 0
        started_rate_limit = time.monotonic()
        current = request.model_copy(
            deep=True,
            update={
                "max_output_tokens": (
                    request.max_output_tokens
                    if request.max_output_tokens is not None
                    else self.profile.max_tokens
                    or self.profile.capabilities.max_output_tokens
                ),
                "timeout_seconds": (
                    request.timeout_seconds
                    if request.timeout_seconds is not None
                    else self.profile.request_timeout
                ),
            },
        )

        while True:
            if current.structured_output:
                current.structured_output_mode = modes[mode_index]
                trace.modes.append(modes[mode_index])
            attempt_mode = current.structured_output_mode or "text_json"
            attempt_started: float | None = None
            try:
                if self.circuit_breaker is not None:
                    self.circuit_breaker.before_request()
                attempt_started = time.monotonic()
                if self.scheduler is None:
                    response = self.adapter.invoke(current)
                else:
                    with self.scheduler.slot(
                        estimated_tokens=_estimate_request_tokens(current)
                    ):
                        response = self.adapter.invoke(current)
                    self.scheduler.succeeded()
                trace.attempts.append(
                    GatewayAttempt(
                        mode=attempt_mode,
                        elapsed_seconds=time.monotonic() - attempt_started,
                        response=response,
                    )
                )
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                self._emit(
                    current,
                    status="completed",
                    mode=attempt_mode,
                    attempt_index=len(trace.attempts),
                    response=response,
                    modes=trace.modes,
                )
                return response
            except GatewayEventSinkError:
                raise
            except CircuitOpenError as error:
                failure = CanonicalModelFailure(
                    request_id=current.request_id,
                    category="circuit_open",
                    reason="shared_provider_failures",
                )
                decision = RecoveryDecision(
                    action="circuit_break",
                    target="rate_limit_group",
                    reason=failure.reason,
                    consume_attempt=False,
                )
                trace.failures.append(failure)
                trace.decisions.append(decision)
                trace.recoveries.append(
                    GatewayRecovery(
                        mode=attempt_mode,
                        failure=failure,
                        decision=decision,
                    )
                )
                self._emit(
                    current,
                    status="rejected",
                    mode=attempt_mode,
                    attempt_index=len(trace.attempts) + 1,
                    failure=failure,
                    decision=decision,
                    modes=trace.modes,
                )
                raise GatewayExecutionError(
                    str(error), failure=failure, decision=decision
                ) from error
            except AdapterError as error:
                if attempt_started is not None:
                    trace.attempts.append(
                        GatewayAttempt(
                            mode=attempt_mode,
                            elapsed_seconds=time.monotonic() - attempt_started,
                            error=error,
                        )
                    )
                failure = error.failure
                trace.failures.append(failure)
                if self.circuit_breaker is not None:
                    if (
                        failure.category == "transient"
                        or failure.category == "rate_limit.capacity"
                    ):
                        self.circuit_breaker.record_failure()
                    else:
                        # A non-provider-health failure still completes a half-open
                        # trial and must not leave the breaker permanently occupied.
                        self.circuit_breaker.record_success()
                fallback_available = mode_index + 1 < len(modes)
                state.rate_limit_elapsed_seconds = time.monotonic() - started_rate_limit
                decision = self.recovery_policy.decide(
                    failure,
                    state,
                    fallback_available=fallback_available,
                )
                trace.decisions.append(decision)
                trace.recoveries.append(
                    GatewayRecovery(
                        mode=attempt_mode,
                        failure=failure,
                        decision=decision,
                    )
                )
                self._emit(
                    current,
                    status="failed",
                    mode=attempt_mode,
                    attempt_index=len(trace.attempts),
                    failure=failure,
                    decision=decision,
                    modes=trace.modes,
                )
                if decision.action == "fallback":
                    state.fallback_attempts += 1
                    mode_index += 1
                    continue
                if decision.action == "retry":
                    if failure.category.startswith("rate_limit."):
                        state.rate_limit_attempts += 1
                        if self.scheduler is not None:
                            self.scheduler.rate_limited(decision.delay_seconds)
                    else:
                        state.transient_attempts += 1
                    self._sleep(decision.delay_seconds)
                    continue
                raise GatewayExecutionError(
                    str(error), failure=failure, decision=decision
                ) from error
            except Exception as error:
                if attempt_started is not None:
                    trace.attempts.append(
                        GatewayAttempt(
                            mode=attempt_mode,
                            elapsed_seconds=time.monotonic() - attempt_started,
                            error=error,
                        )
                    )
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                raise

    def _mode_chain(self, request: GenerationRequest) -> list[OutputMode]:
        if not request.structured_output:
            return ["text_json"]
        requested = request.structured_output_mode
        preferred = (
            self.profile.capabilities.preferred_structured_output_mode
            if requested in {None, "auto"}
            else requested
        )
        chain = [preferred, *self.profile.capabilities.structured_output_fallback_order]
        result: list[OutputMode] = []
        for mode in chain:
            if mode == "auto":
                continue
            if mode not in result:
                result.append(mode)
        return result or ["text_json"]

    def _emit(
        self,
        request: GenerationRequest,
        *,
        status: str,
        mode: OutputMode,
        attempt_index: int,
        failure: CanonicalModelFailure | None = None,
        decision: RecoveryDecision | None = None,
        response: GenerationResponse | None = None,
        modes: list[OutputMode] | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(
                {
                    "profile": self.profile.name,
                    "operation": request.operation,
                    "request_id": request.request_id,
                    "provider_request_id": (
                        response.provider_request_id if response is not None else None
                    ),
                    "status": status,
                    "structured_output_mode": mode,
                    "attempt_index": attempt_index,
                    "fallback_chain": list(modes or []),
                    "finish_reason": (
                        response.finish_reason if response is not None else None
                    ),
                    "failure": (
                        failure.model_dump(mode="json") if failure is not None else None
                    ),
                    "decision": (
                        decision.model_dump(mode="json")
                        if decision is not None
                        else None
                    ),
                    "scheduler": (
                        self.scheduler.snapshot().__dict__ if self.scheduler else None
                    ),
                }
            )
        except Exception as error:
            raise GatewayEventSinkError(
                f"Failed to persist model gateway event for {request.request_id}"
            ) from error


def _estimate_request_tokens(request: GenerationRequest) -> int:
    input_chars = sum(len(message.content) for message in request.messages)
    estimated_input = max(1, input_chars // 4)
    return estimated_input + (request.max_output_tokens or 0)
