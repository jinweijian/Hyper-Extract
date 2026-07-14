from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
from typing import Iterable

from hyperextract.providers.contracts import CanonicalModelFailure


def canonicalize_provider_error(
    error: Exception,
    *,
    request_id: str,
    secret_values: Iterable[str] = (),
    category: str | None = None,
    reason: str | None = None,
) -> CanonicalModelFailure:
    """Normalize an SDK/HTTP exception without leaking provider logic upstream."""
    status = _status_code(error)
    body = _error_text(error).lower()
    headers = _headers(error)
    provider_code = _provider_code(error)

    if category is None or reason is None:
        category, reason = _classify(status, provider_code, body, type(error).__name__)
    raw_message = _redact(_error_text(error), secret_values)
    return CanonicalModelFailure(
        request_id=request_id,
        category=category,
        reason=reason,
        http_status=status,
        provider_code=provider_code,
        retry_after_seconds=_retry_after(headers),
        raw_message=raw_message,
    )


def _classify(
    status: int | None, code: str | None, text: str, error_name: str
) -> tuple[str, str]:
    joined = f"{code or ''} {text}".lower()
    if status in {401, 403}:
        return "authentication", "auth_failed"
    if status == 429:
        if any(
            marker in joined
            for marker in ("insufficient_quota", "quota exhausted", "balance")
        ):
            return "quota.exhausted", "account_quota_exhausted"
        if any(
            marker in joined for marker in ("permission", "not allowed", "plan limit")
        ):
            return "quota.permission", "model_or_tenant_limit"
        if any(marker in joined for marker in ("token", "tpm")):
            return "rate_limit.tokens", "tokens_per_minute"
        if any(marker in joined for marker in ("concurrent", "concurrency")):
            return "rate_limit.concurrency", "concurrent_requests"
        if any(marker in joined for marker in ("capacity", "overload", "busy")):
            return "rate_limit.capacity", "provider_capacity"
        return "rate_limit.requests", "requests_per_minute"
    if status == 529:
        return "rate_limit.capacity", "provider_capacity"
    if status is not None and status >= 500:
        return "transient", "server_error"
    if status == 400:
        if any(
            marker in joined
            for marker in ("response_format", "json_schema", "tool_choice", "tools")
        ):
            return "unsupported_capability", "capability_mismatch"
        if any(
            marker in joined
            for marker in ("context length", "maximum context", "too many tokens")
        ):
            return "context_window", "context_limit_exceeded"
        if any(
            marker in joined
            for marker in ("unsupported parameter", "unknown parameter")
        ):
            return "unsupported_parameter", "parameter_rejected"
        return "protocol", "bad_request"
    lowered_name = error_name.lower()
    if "timeout" in lowered_name or "connection" in lowered_name:
        return "transient", "connection_or_timeout"
    return "unknown", "unclassified_provider_error"


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _headers(error: Exception) -> dict[str, str]:
    response = getattr(error, "response", None)
    source = getattr(response, "headers", None) or getattr(error, "headers", None) or {}
    try:
        return {str(key).lower(): str(value) for key, value in source.items()}
    except AttributeError:
        return {}


def _provider_code(error: Exception) -> str | None:
    direct = getattr(error, "code", None)
    if direct:
        return str(direct)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error", body)
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"])
    return None


def _error_text(error: Exception) -> str:
    body = getattr(error, "body", None)
    if body:
        try:
            return f"{error}: {json.dumps(body, ensure_ascii=False, default=str)}"
        except TypeError:
            pass
    return str(error)


def _redact(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from datetime import datetime, timezone

            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
