"""PostgreSQL concurrency tests for create_or_get().

These tests require a live PostgreSQL instance. Set ``HE_TEST_POSTGRES_URL`` to
a SQLAlchemy URL (e.g. ``postgresql+psycopg://user:pass@localhost/he_test``).
When unset, the module is skipped so the SQLite unit suite stays green.
"""

from __future__ import annotations

import os
import threading

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HE_TEST_POSTGRES_URL"),
    reason="HE_TEST_POSTGRES_URL not configured; PostgreSQL concurrency tests skipped",
)


def _make_repository(database_url: str):
    from hyperextract.service.db import create_engine_and_session
    from hyperextract.service.repository import RunRepository

    engine, session_factory = create_engine_and_session(
        database_url, for_tests=True
    )
    return RunRepository(session_factory), engine


def test_concurrent_create_or_get_returns_single_row():
    """Two threads inserting the same idempotency key must converge on one row.

    The winner is whichever thread wins the INSERT race; the loser must catch
    ``IntegrityError``, roll back, re-read, and return the existing record
    instead of raising.
    """
    import hashlib

    from hyperextract.service.commands import RunCommand

    database_url = os.environ["HE_TEST_POSTGRES_URL"]
    repository, engine = _make_repository(database_url)
    try:
        fingerprint = hashlib.sha256(b"request").hexdigest()
        commands = [
            RunCommand(
                run_id=f"run_concurrent_{index}",
                request_fingerprint=fingerprint,
                request_json={"input": {}},
                output_uri=f"file:///exchange/runs/run_concurrent_{index}/",
                resolved_package_fingerprint="b" * 64,
            )
            for index in range(2)
        ]
        idempotency_key = "concurrent-key"

        results: list[tuple[str, bool]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(command):
            barrier.wait()
            record, created = repository.create_or_get(command, idempotency_key)
            with lock:
                results.append((record.run_id, created))

        threads = [
            threading.Thread(target=worker, args=(command,)) for command in commands
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 2
        # Both threads must observe the same run_id
        assert results[0][0] == results[1][0]
        assert results[0][0] in {command.run_id for command in commands}
        # Exactly one thread reports ``created=True``
        created_flags = [created for _, created in results]
        assert created_flags.count(True) == 1
        assert created_flags.count(False) == 1

        # Database must contain exactly one row for the idempotency key
        from sqlalchemy import select

        from hyperextract.service.db_models import RunEntity

        with repository.session_factory() as session:
            rows = session.scalars(
                select(RunEntity).where(RunEntity.idempotency_key == idempotency_key)
            ).all()
            assert len(rows) == 1
    finally:
        # Clean up so re-runs against the same database don't collide.
        with repository.session_factory.begin() as session:
            from sqlalchemy import delete

            from hyperextract.service.db_models import RunEntity

            session.execute(
                delete(RunEntity).where(RunEntity.idempotency_key == "concurrent-key")
            )
        engine.dispose()


def test_stale_worker_cannot_complete_after_postgres_reclaim():
    from datetime import timedelta

    import pytest

    from hyperextract.service.commands import RunCommand
    from hyperextract.service.db_models import RunEntity, utcnow
    from hyperextract.service.repository import LeaseOwnershipLost

    database_url = os.environ["HE_TEST_POSTGRES_URL"]
    repository, engine = _make_repository(database_url)
    run_id = "run_postgres_stale_owner"
    idempotency_key = "postgres-stale-owner-key"
    try:
        repository.create_or_get(
            RunCommand(
                run_id=run_id,
                request_fingerprint="a" * 64,
                request_json={"input": {}},
                output_uri=f"file:///exchange/runs/{run_id}/",
                resolved_package_fingerprint="b" * 64,
            ),
            idempotency_key,
        )
        repository.claim_next("worker-old", lease_seconds=120)
        with repository.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            row.lease_expires_at = utcnow() - timedelta(seconds=1)

        repository.requeue_expired_leases(max_recoveries=3)
        claimed = repository.claim_next("worker-new", lease_seconds=120)
        assert claimed.run_id == run_id

        with pytest.raises(LeaseOwnershipLost):
            repository.complete(run_id, "worker-old", {"stale": True})
        current = repository.get(run_id)
        assert current.status == "running"
        assert current.lease_owner == "worker-new"
    finally:
        with repository.session_factory.begin() as session:
            from sqlalchemy import delete

            from hyperextract.service.db_models import RunEntity

            session.execute(
                delete(RunEntity).where(RunEntity.idempotency_key == idempotency_key)
            )
        engine.dispose()
