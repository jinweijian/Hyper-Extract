"""Stable model error categories used by retries and context replanning."""

from __future__ import annotations


class ModelInvocationError(RuntimeError):
    retryable = False
    category = "model_error"

    def __init__(self, message: str, *, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original


class AuthenticationModelError(ModelInvocationError):
    category = "authentication"


class UnsupportedModelCapabilityError(ModelInvocationError):
    category = "unsupported_capability"


class TransientModelError(ModelInvocationError):
    retryable = True
    category = "transient"


class RateLimitModelError(TransientModelError):
    category = "rate_limit"


class ContextWindowExceededError(ModelInvocationError):
    category = "context_window"


class OutputTruncatedError(ModelInvocationError):
    category = "output_truncated"


class OutputValidationError(ModelInvocationError):
    category = "output_validation"


def classify_model_error(error: Exception) -> ModelInvocationError:
    """Map provider-specific exceptions to pipeline-stable categories."""
    if isinstance(error, ModelInvocationError):
        return error
    text = f"{type(error).__name__}: {error}".lower()
    status = getattr(error, "status_code", None)
    if status in {401, 403} or any(
        marker in text
        for marker in ("401", "403", "unauthorized", "forbidden", "invalid api key")
    ):
        return AuthenticationModelError(str(error), original=error)
    if any(
        marker in text
        for marker in (
            "maximum context",
            "context length",
            "context window",
            "too many tokens",
            "prompt is too long",
        )
    ):
        return ContextWindowExceededError(str(error), original=error)
    if any(
        marker in text
        for marker in (
            "response_format",
            "json_schema is not supported",
            "tool calling is not supported",
            "unsupported capability",
            "does not support tools",
        )
    ):
        return UnsupportedModelCapabilityError(str(error), original=error)
    if status == 429 or "429" in text or "rate limit" in text:
        return RateLimitModelError(str(error), original=error)
    if any(
        marker in text
        for marker in (
            "finish_reason=length",
            "finishreason=length",
            "lengthfinish",
            "output truncated",
        )
    ):
        return OutputTruncatedError(str(error), original=error)
    if isinstance(error, TimeoutError) or any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection error",
            "connection reset",
            "temporarily unavailable",
            "service unavailable",
            "500",
            "502",
            "503",
            "504",
        )
    ):
        return TransientModelError(str(error), original=error)
    return ModelInvocationError(str(error), original=error)
