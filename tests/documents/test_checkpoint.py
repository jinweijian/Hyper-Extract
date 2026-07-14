from hyperextract.documents.checkpoint import RunCheckpoint, atomic_write_json


def test_checkpoint_resumes_matching_run(tmp_path):
    first = RunCheckpoint(tmp_path, source_fingerprint="abc", config={"chunk": 4})
    atomic_write_json(
        first.chunk_dir("chunk-1") / "graph.json", {"nodes": [], "edges": []}
    )
    atomic_write_json(
        first.chunk_dir("chunk-1") / "status.json", {"status": "completed"}
    )

    resumed = RunCheckpoint(tmp_path, source_fingerprint="abc", config={"chunk": 4})

    assert resumed.run_id == first.run_id
    assert resumed.chunk_completed("chunk-1")


def test_checkpoint_rejects_changed_configuration(tmp_path):
    RunCheckpoint(tmp_path, source_fingerprint="abc", config={"chunk": 4})

    try:
        RunCheckpoint(tmp_path, source_fingerprint="abc", config={"chunk": 8})
    except ValueError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("Expected mismatched checkpoint to fail")


def test_checkpoint_force_starts_clean_run(tmp_path):
    first = RunCheckpoint(tmp_path, source_fingerprint="abc", config={"chunk": 4})
    atomic_write_json(
        first.chunk_dir("chunk-1") / "graph.json", {"nodes": [], "edges": []}
    )

    restarted = RunCheckpoint(
        tmp_path,
        source_fingerprint="abc",
        config={"chunk": 4},
        force=True,
    )

    assert restarted.run_id != first.run_id
    assert not (restarted.chunk_dir("chunk-1") / "graph.json").exists()
