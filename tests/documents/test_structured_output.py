from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from hyperextract.documents.model_errors import (
    AuthenticationModelError,
    ContextWindowExceededError,
    OutputTruncatedError,
    RateLimitModelError,
    TransientModelError,
    classify_model_error,
)
from hyperextract.documents.structured_output import (
    StructuredOutputInvoker,
    extract_json_value,
)


class _Result(BaseModel):
    name: str
    count: int


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            '<think>reasoning</think>\n{"name":"alpha","count":2}',
            {"name": "alpha", "count": 2},
        ),
        ('```json\n{"name":"alpha","count":2}\n```', {"name": "alpha", "count": 2}),
        (
            'Result follows: {"name":"alpha","count":2} done.',
            {"name": "alpha", "count": 2},
        ),
        ('[{"name":"alpha","count":2}]', [{"name": "alpha", "count": 2}]),
    ],
)
def test_extract_json_value_handles_thinking_fences_and_surrounding_text(
    response, expected
):
    assert extract_json_value(response) == expected


def test_structured_output_ignores_reasoning_content_and_validates_final_content():
    model = SimpleNamespace(
        invoke=lambda _input: AIMessage(
            content='{"name":"alpha","count":2}',
            additional_kwargs={"reasoning_content": "private reasoning"},
        )
    )
    result = StructuredOutputInvoker(model, _Result, mode="text_json").invoke("prompt")
    assert result == _Result(name="alpha", count=2)


def test_structured_output_classifies_truncated_json():
    model = SimpleNamespace(invoke=lambda _input: AIMessage(content='{"name":"alpha"'))
    with pytest.raises(OutputTruncatedError):
        StructuredOutputInvoker(
            model, _Result, mode="text_json", repair_attempts=0
        ).invoke("prompt")


def test_structured_output_uses_finish_reason_to_classify_empty_truncation():
    model = SimpleNamespace(
        invoke=lambda _input: AIMessage(
            content="",
            response_metadata={"finish_reason": "length"},
        )
    )
    with pytest.raises(OutputTruncatedError):
        StructuredOutputInvoker(
            model, _Result, mode="text_json", repair_attempts=0
        ).invoke("prompt")


def test_structured_output_does_not_retry_truncated_output_as_repair():
    calls = 0

    def invoke(_input):
        nonlocal calls
        calls += 1
        return AIMessage(
            content="",
            response_metadata={"finish_reason": "length"},
        )

    with pytest.raises(OutputTruncatedError):
        StructuredOutputInvoker(
            SimpleNamespace(invoke=invoke),
            _Result,
            mode="text_json",
            repair_attempts=2,
        ).invoke("prompt")

    assert calls == 1


class _UnsupportedNative:
    def with_structured_output(self, _schema, **_kwargs):
        return SimpleNamespace(
            invoke=lambda _input: (_ for _ in ()).throw(
                RuntimeError("response_format json_schema is not supported")
            )
        )

    def invoke(self, _input):
        return AIMessage(content='{"name":"fallback","count":1}')


def test_auto_mode_falls_back_to_plain_json_only_for_unsupported_capability():
    result = StructuredOutputInvoker(
        _UnsupportedNative(), _Result, mode="auto", repair_attempts=0
    ).invoke("prompt")
    assert result.name == "fallback"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("401 unauthorized"), AuthenticationModelError),
        (RuntimeError("429 rate limit"), RateLimitModelError),
        (TimeoutError("request timed out"), TransientModelError),
        (RuntimeError("503 service unavailable"), TransientModelError),
        (RuntimeError("maximum context length exceeded"), ContextWindowExceededError),
        (RuntimeError("finish_reason=length"), OutputTruncatedError),
    ],
)
def test_model_errors_have_stable_categories(error, expected):
    assert isinstance(classify_model_error(error), expected)
