from types import SimpleNamespace

from hyperextract.providers.failures import canonicalize_provider_error


class HTTPError(Exception):
    def __init__(self, message, status, *, headers=None, body=None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})
        self.body = body


def test_rate_limit_categories_do_not_treat_quota_as_retryable_429():
    quota = canonicalize_provider_error(
        HTTPError(
            "insufficient quota",
            429,
            body={"error": {"code": "insufficient_quota"}},
        ),
        request_id="r1",
    )
    tokens = canonicalize_provider_error(
        HTTPError("TPM token rate limit", 429), request_id="r2"
    )
    assert quota.category == "quota.exhausted"
    assert tokens.category == "rate_limit.tokens"


def test_retry_after_and_secret_redaction_are_preserved():
    failure = canonicalize_provider_error(
        HTTPError("key=super-secret", 429, headers={"Retry-After": "9"}),
        request_id="r1",
        secret_values=("super-secret",),
    )
    assert failure.retry_after_seconds == 9
    assert "super-secret" not in failure.raw_message
