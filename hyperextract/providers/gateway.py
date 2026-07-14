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


@dataclass
class GatewayTrace:
    modes: list[OutputMode] = field(default_factory=list)
    decisions: list[RecoveryDecision] = field(default_factory=list)
    failures: list[CanonicalModelFailure] = field(default_factory=list)


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
        current = request.model_copy(deep=True)

        while True:
            if current.structured_output:
                current.structured_output_mode = modes[mode_index]
                trace.modes.append(modes[mode_index])
            try:
                if self.circuit_breaker is not None:
                    self.circuit_breaker.before_request()
                if self.scheduler is None:
                    response = self.adapter.invoke(current)
                else:
                    with self.scheduler.slot(
                        estimated_tokens=_estimate_request_tokens(current)
                    ):
                        response = self.adapter.invoke(current)
                    self.scheduler.succeeded()
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()
                return response
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
                raise GatewayExecutionError(
                    str(error), failure=failure, decision=decision
                ) from error
            except AdapterError as error:
                failure = error.failure
                trace.failures.append(failure)
                if self.circuit_breaker is not None and (
                    failure.category == "transient"
                    or failure.category == "rate_limit.capacity"
                ):
                    self.circuit_breaker.record_failure()
                fallback_available = mode_index + 1 < len(modes)
                state.rate_limit_elapsed_seconds = time.monotonic() - started_rate_limit
                decision = self.recovery_policy.decide(
                    failure,
                    state,
                    fallback_available=fallback_available,
                )
                trace.decisions.append(decision)
                self._emit(current, failure, decision)
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

    def _mode_chain(self, request: GenerationRequest) -> list[OutputMode]:
        if not request.structured_output:
            return ["text_json"]
        preferred = request.structured_output_mode or (
            self.profile.capabilities.preferred_structured_output_mode
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
        failure: CanonicalModelFailure,
        decision: RecoveryDecision,
    ) -> None:
        if self._event_sink is None:
            return
        self._event_sink(
            {
                "profile": self.profile.name,
                "operation": request.operation,
                "request_id": request.request_id,
                "provider_request_id": None,
                "failure": failure.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "scheduler": (
                    self.scheduler.snapshot().__dict__ if self.scheduler else None
                ),
            }
        )


def _estimate_request_tokens(request: GenerationRequest) -> int:
    input_chars = sum(len(message.content) for message in request.messages)
    estimated_input = max(1, input_chars // 4)
    return estimated_input + (request.max_output_tokens or 0)
