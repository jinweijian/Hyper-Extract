"""Tests for the structured progress snapshot module (Task 6)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hyperextract.service.progress import (
    ACTIVITY_MESSAGES,
    ProgressSnapshot,
    activity_for_event,
    build_snapshot,
    compute_percent,
    is_owner_valid,
    read_snapshot,
    write_snapshot,
)


def test_compute_percent_only_when_valid():
    assert compute_percent(8, 28) == 28.57
    assert compute_percent(0, 10) == 0.0
    assert compute_percent(10, 10) == 100.0
    assert compute_percent(20, 10) == 100.0  # clamped
    assert compute_percent(None, 10) is None
    assert compute_percent(5, None) is None
    assert compute_percent(5, 0) is None
    assert compute_percent(-1, 10) is None


def test_build_snapshot_uses_default_message_for_activity():
    snap = build_snapshot(
        run_id="run_x",
        attempt=1,
        worker_id="w1",
        sequence=3,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
        current=8,
        total=28,
    )
    assert snap.message == ACTIVITY_MESSAGES["EXTRACTING_CHUNK"]
    assert snap.percent == 28.57
    assert snap.sequence == 3
    assert snap.worker_id == "w1"


def test_build_snapshot_keeps_explicit_message():
    snap = build_snapshot(
        run_id="run_x",
        attempt=1,
        worker_id="w1",
        sequence=4,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
        message="正在分析第 8/28 个内容块",
        current=8,
        total=28,
    )
    assert snap.message == "正在分析第 8/28 个内容块"


def test_write_and_read_snapshot_roundtrip(tmp_path):
    path = tmp_path / "state" / "progress.json"
    snap = build_snapshot(
        run_id="run_rt",
        attempt=1,
        worker_id="w1",
        sequence=5,
        stage="merge",
        activity="MERGING_SECTION",
        current=3,
        total=12,
    )
    write_snapshot(path, snap)
    assert path.is_file()
    restored = read_snapshot(path)
    assert restored is not None
    assert restored.run_id == "run_rt"
    assert restored.worker_id == "w1"
    assert restored.activity == "MERGING_SECTION"
    assert restored.percent == 25.0


def test_write_snapshot_is_atomic(tmp_path):
    """The temp file must not linger after a successful write."""
    path = tmp_path / "state" / "progress.json"
    snap = build_snapshot(
        run_id="run_atom",
        attempt=1,
        worker_id="w1",
        sequence=1,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
    )
    write_snapshot(path, snap)
    leftovers = list((tmp_path / "state").glob(".progress-*"))
    assert leftovers == []


def test_read_snapshot_returns_none_for_missing(tmp_path):
    assert read_snapshot(tmp_path / "nope.json") is None


def test_read_snapshot_returns_none_for_corrupt(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_snapshot(path) is None


def test_read_snapshot_returns_none_for_non_object(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_snapshot(path) is None


def test_is_owner_valid_checks_all_fields():
    snap = build_snapshot(
        run_id="run_o",
        attempt=2,
        worker_id="w1",
        sequence=1,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
    )
    assert is_owner_valid(snap, run_id="run_o", attempt=2, lease_owner="w1") is True
    assert is_owner_valid(snap, run_id="run_o", attempt=2, lease_owner="w2") is False
    assert is_owner_valid(snap, run_id="other", attempt=2, lease_owner="w1") is False
    assert is_owner_valid(snap, run_id="run_o", attempt=1, lease_owner="w1") is False
    assert is_owner_valid(None, run_id="run_o", attempt=2, lease_owner="w1") is False
    assert is_owner_valid(snap, run_id="run_o", attempt=2, lease_owner=None) is False


def test_is_owner_valid_rejects_stale_snapshot():
    stale = ProgressSnapshot(
        run_id="run_o",
        attempt=2,
        worker_id="w1",
        updated_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    )
    assert (
        is_owner_valid(
            stale,
            run_id="run_o",
            attempt=2,
            lease_owner="w1",
            max_age_seconds=10,
        )
        is False
    )


def test_activity_for_event_maps_stages():
    assert activity_for_event("local_extract", "progress") == "EXTRACTING_CHUNK"
    assert activity_for_event("merge", "progress") == "MERGING_SECTION"
    assert activity_for_event("deduplicate", "progress") == "DEDUPLICATING"
    assert activity_for_event("quality", "progress") == "QUALITY_CHECKING"
    assert activity_for_event("publish", "progress") == "ARTIFACT_PUBLISHING"
    assert activity_for_event("context_planning", "started") == "CONTEXT_PLANNING"


def test_activity_for_event_recovery_override():
    assert (
        activity_for_event("local_extract", "progress", recovering=True)
        == "WORKER_RECOVERING"
    )
    assert (
        activity_for_event("local_extract", "progress", recovering=False)
        == "EXTRACTING_CHUNK"
    )


def test_public_view_drops_worker_id():
    snap = build_snapshot(
        run_id="run_p",
        attempt=1,
        worker_id="secret-worker",
        sequence=9,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
        current=1,
        total=2,
    )
    view = snap.public_view()
    assert "worker_id" not in json.dumps(view)
    assert view["message_seq"] == 9
    assert view["progress"]["percent"] == 50.0


def test_write_snapshot_overwrites_previous(tmp_path):
    """Repeated writes replace the file atomically (sequence advances)."""
    path = tmp_path / "state" / "progress.json"
    for seq in range(5):
        write_snapshot(
            path,
            build_snapshot(
                run_id="run_ov",
                attempt=1,
                worker_id="w1",
                sequence=seq,
                stage="local_extract",
                activity="EXTRACTING_CHUNK",
                current=seq,
                total=5,
            ),
        )
    restored = read_snapshot(path)
    assert restored is not None
    assert restored.sequence == 4
