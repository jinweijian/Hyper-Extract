from types import SimpleNamespace

import pytest

from hyperextract.providers.normalization import (
    TruncatedJSONError,
    extract_json_value,
    normalize_generation_payload,
)


def test_normalizes_separate_reasoning_field():
    payload = SimpleNamespace(content='{"ok":true}', reasoning_content="hidden")
    result = normalize_generation_payload(
        payload, reasoning_content_mode="separate_field"
    )
    assert result.final_text == '{"ok":true}'
    assert result.reasoning_text == "hidden"


def test_normalizes_content_blocks():
    result = normalize_generation_payload(
        [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "visible"},
        ],
        reasoning_content_mode="content_blocks",
    )
    assert result.final_text == "visible"
    assert result.reasoning_text == "hidden"


def test_extract_json_tracks_strings_and_escapes():
    assert extract_json_value('before {"text":"a } \\" b","ok":true} after') == {
        "text": 'a } " b',
        "ok": True,
    }


def test_incomplete_json_is_truncation():
    with pytest.raises(TruncatedJSONError):
        extract_json_value('{"items":[1,2]')
