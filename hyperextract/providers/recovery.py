from __future__ import annotations

import random
from dataclasses import dataclass

from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    RecoveryDecision,
)
from hyperextract.providers.profiles import ProfileRecovery

RECOVERY_POLICY_VERSION = "1"


@dataclass
class RecoveryState:
    transient_attempts: int = 0
    validation_attempts: int = 0
    repair_attempts: int = 0
    fallback_attempts: int = 0
    rate_limit_attempts: int = 0
    rate_limit_elapsed_seconds: float = 0


class RecoveryPolicy:
    """Deterministic mapping from canonical failures to bounded actions."""

    version = RECOVERY_POLICY_VERSION

    def __init__(
        self,
        config: ProfileRecovery | None = None,
        *,
        random_source: random.Random | None = None,
    ) -> None:
        self.config = config or ProfileRecovery()
        self._random = random_source or random.Random()

    def decide(
        self,
        failure: CanonicalModelFailure,
        state: RecoveryState,
        *,
        fallback_available: bool = False,
        target: str = "request",
    ) -> RecoveryDecision:
        category = failure.category
        if category in {"authentication", "quota.exhausted", "quota.permission"}:
            return RecoveryDecision(
                action="fail", target="run", reason=category, consume_attempt=False
            )
        if category in {"unsupported_parameter", "protocol"}:
            return RecoveryDecision(
                action="fail", target="run", reason=category, consume_attempt=False
            )
        if category == "unsupported_capability":
            if (
                fallback_available
                and state.fallback_attempts < self.config.fallback_attempts
            ):
                return RecoveryDecision(
                    action="fallback",
                    target="request",
                    reason=failure.reason,
                )
            return RecoveryDecision(
                action="fail",
                target="request",
                reason="capability_fallback_exhausted",
                consume_attempt=False,
            )
        if category.startswith("rate_limit."):
            if (
                state.rate_limit_attempts >= self.config.rate_limit_attempts
                or state.rate_limit_elapsed_seconds
                >= self.config.max_rate_limit_elapsed_seconds
            ):
                return RecoveryDecision(
                    action="fail",
                    target="request",
                    reason="rate_limit_budget_exhausted",
                    consume_attempt=False,
                )
            return RecoveryDecision(
                action="retry",
                target="request",
                reason=category,
                delay_seconds=self._delay(failure, state.rate_limit_attempts + 1),
            )
        if category == "transient":
            if state.transient_attempts >= self.config.transient_retry_attempts:
                return RecoveryDecision(
                    action="fail",
                    target="request",
                    reason="transient_retry_budget_exhausted",
                    consume_attempt=False,
                )
            return RecoveryDecision(
                action="retry",
                target="request",
                reason=failure.reason,
                delay_seconds=self._delay(failure, state.transient_attempts + 1),
            )
        if category == "context_window":
            return RecoveryDecision(
                action="replan", target="chunk", reason=failure.reason
            )
        if category == "output_truncated":
            return RecoveryDecision(
                action="split", target="batch", reason=failure.reason
            )
        if category in {"invalid_json", "output_validation"}:
            if state.repair_attempts < self.config.validation_repair_attempts:
                return RecoveryDecision(
                    action="repair", target="batch", reason=failure.reason
                )
            return RecoveryDecision(
                action="fail",
                target="batch",
                reason="validation_repair_budget_exhausted",
                consume_attempt=False,
            )
        if category == "invalid_item":
            action = self.config.invalid_list_item_policy
            return RecoveryDecision(
                action="quarantine" if action == "quarantine" else "fail",
                target="item" if action == "quarantine" else "batch",
                reason=failure.reason,
            )
        if category == "embedding_batch_failure":
            return RecoveryDecision(
                action="split", target="batch", reason=failure.reason
            )
        if category in {"embedding_alignment", "embedding_dimension"}:
            return RecoveryDecision(action="fail", target="run", reason=failure.reason)
        fail_target = (
            target if target in {"request", "batch", "chunk", "run"} else "request"
        )
        return RecoveryDecision(
            action="fail",
            target=fail_target,
            reason=failure.reason,
            consume_attempt=False,
        )

    def _delay(self, failure: CanonicalModelFailure, attempt: int) -> float:
        cap = min(
            self.config.max_delay_seconds,
            self.config.base_delay_seconds * (2 ** max(0, attempt - 1)),
        )
        jitter = self._random.uniform(0, cap) if cap else 0
        if failure.retry_after_seconds is not None:
            return max(failure.retry_after_seconds, jitter)
        return jitter
