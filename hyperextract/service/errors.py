from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns that capture provider secrets commonly embedded in error strings:
#   "Bearer sk-secret-value"
#   "Authorization: Bearer eyJ..."
#   "api-key=sk-..."
#   "sk-AbcDef1234567890..."
# The replacement always preserves the *kind* of credential (so operators can
# still see "a bearer token was involved") but never the value itself.
_BEARER_PATTERN = re.compile(
    r"(?i)\b(Bearer|Token|Authorization)\s*[:=]?\s*[A-Za-z0-9\-._~+/]+=*"
)
_API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9\-._~+/]{8,}")
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|secret|password|authorization)"
    r"\s*[:=]\s*['\"]?[^\s'\",;]{4,}"
)

_REDACTED = "[REDACTED]"

# Public error message length cap. Anything longer is truncated; the full
# (still redacted) message is persisted in the diagnostics file under
# ``diagnostics/attempts/`` for operator forensics.
PUBLIC_MESSAGE_MAX = 500


@dataclass(frozen=True)
class ServiceError(Exception):
    status_code: int
    code: str
    message: str
    details: list[dict] = field(default_factory=list)

    def body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def redact_secrets(text: str) -> str:
    """Replace bearer tokens, ``sk-`` API keys, and ``key=value`` pairs.

    The redaction is conservative: it always replaces the secret *value*
    but keeps the surrounding context (e.g. ``Bearer [REDACTED]``) so
    operators can still see what kind of credential leaked into the
    error text.
    """
    if not text:
        return text

    def _bearer_replacement(match: re.Match) -> str:
        # Preserve the leading keyword (Bearer/Token/Authorization) and
        # replace only the credential portion.
        full = match.group(0)
        keyword = match.group(1)
        return full.replace(full[len(keyword):].lstrip(" := "), _REDACTED, 1)

    text = _BEARER_PATTERN.sub(_bearer_replacement, text)
    text = _API_KEY_PATTERN.sub("sk-" + _REDACTED, text)
    text = _KEY_VALUE_PATTERN.sub(
        lambda m: f"{m.group(1)}={_REDACTED}", text
    )
    return text


def _categorize(error: BaseException) -> tuple[str, bool]:
    """Map an exception to a stable ``(code, resumable)`` pair.

    * Authentication/authorization and invalid-input failures are NOT
      resumable — retrying with the same credentials/input will fail again.
    * Transient failures (rate limits, timeouts, retry exhaustion, worker
      recovery) ARE resumable.
    * Anything else is treated as a generic execution failure that is
      resumable, since we cannot prove retrying would not help.
    """
    name = type(error).__name__
    message = str(error).lower()

    # Artifact state inconsistency — operator must inspect, never auto-resume
    # and never overwrite. This wins over the generic ValueError branch.
    if "artifact_state_inconsistent" in message:
        return "ARTIFACT_STATE_INCONSISTENT", False

    # Authentication / authorization
    if (
        "auth" in message
        or "unauthorized" in message
        or "forbidden" in message
        or "permission" in message
        or "invalid api key" in message
        or "invalid_api_key" in message
        or ("api key" in message and "invalid" in message)
        or name in {"AuthenticationError", "PermissionDeniedError"}
    ):
        return "MODEL_AUTHENTICATION_FAILED", False

    # Invalid input / schema validation (excluding ARTIFACT_STATE_INCONSISTENT
    # which is handled above).
    if (
        "invalid input" in message
        or "invalid_request" in message
        or "validation" in message
        or "schema" in message
        or "malformed" in message
        or "bad request" in message
        or name in {"ValidationError", "OutputValidationError"}
    ):
        return "MODEL_INVALID_INPUT", False

    # Retry exhaustion
    if "retry" in message and ("exhaust" in message or "limit" in message):
        return "MODEL_RETRY_EXHAUSTED", True

    # Worker recovery exhaustion
    if "worker_recovery_exhausted" in message:
        return "WORKER_RECOVERY_EXHAUSTED", True

    # Transient provider failures (rate limits, timeouts, 5xx)
    if (
        "rate limit" in message
        or "rate_limit" in message
        or "timeout" in message
        or "timed out" in message
        or "temporary" in message
        or "unavailable" in message
        or "5xx" in message
        or "503" in message
        or "502" in message
        or "500" in message
        or name in {"TimeoutError", "RateLimitError", "APITimeoutError"}
    ):
        return "MODEL_TRANSIENT_FAILURE", True

    return "RUN_EXECUTION_FAILED", True


def normalize_failure(error: BaseException) -> tuple[str, str, bool, str]:
    """Normalize an arbitrary exception into stable failure fields.

    Returns ``(code, public_message, resumable, redacted_full_message)``.

    * ``public_message`` is capped at :data:`PUBLIC_MESSAGE_MAX` characters
      and is the only message surfaced through the API.
    * ``redacted_full_message`` is the full redacted message; the caller
      should persist it under ``diagnostics/attempts/`` for operators.
    """
    code, resumable = _categorize(error)
    redacted_full = redact_secrets(str(error))
    public_message = redacted_full[:PUBLIC_MESSAGE_MAX]
    return code, public_message, resumable, redacted_full
