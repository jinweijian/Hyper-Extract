from pathlib import Path

import pytest
from pydantic import ValidationError

from hyperextract.profiles.course import (
    compile_course_profile,
    load_course_profile,
)


def test_builtin_course_profile_is_strict_and_compiles_all_stages():
    profile = load_course_profile()
    compiled = compile_course_profile(profile)

    assert profile.name == "course-knowledge-default"
    assert profile.version == "1.1.0"
    assert len(profile.content_hash) == 64
    assert "可独立" in compiled.nodes
    assert "同一次输出" in compiled.chunk
    assert "正文实质定义" in compiled.chunk
    assert "逐一判断抽取或跳过" in compiled.chunk
    assert "优先使用 confusable" in compiled.chunk
    assert "prerequisite" in compiled.local_edges
    assert "仅主题相近" in compiled.global_edges
    assert "完全相同" in compiled.dedup
    assert compiled.content_hash == profile.content_hash


def test_profile_hash_is_content_addressed_not_path_addressed(tmp_path):
    source = Path(
        "hyperextract/profiles/defaults/course-knowledge-default.yaml"
    ).read_text(encoding="utf-8")
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(source, encoding="utf-8")
    second.write_text(source, encoding="utf-8")

    assert (
        load_course_profile(first).content_hash
        == load_course_profile(second).content_hash
    )


def test_profile_rejects_unknown_fields_and_invalid_relation_direction(tmp_path):
    source = Path(
        "hyperextract/profiles/defaults/course-knowledge-default.yaml"
    ).read_text(encoding="utf-8")
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(source + "\nunknown_setting: true\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown_setting"):
        load_course_profile(unknown)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        source.replace(
            "prerequisite:\n    directed: true",
            "prerequisite:\n    directed: false",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="prerequisite"):
        load_course_profile(invalid)


def test_profile_rejects_semantic_conflicts(tmp_path):
    source = Path(
        "hyperextract/profiles/defaults/course-knowledge-default.yaml"
    ).read_text(encoding="utf-8")
    invalid = tmp_path / "conflict.yaml"
    invalid.write_text(
        source.replace("table_of_contents: skip", "table_of_contents: extract"),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="table_of_contents"):
        load_course_profile(invalid)
