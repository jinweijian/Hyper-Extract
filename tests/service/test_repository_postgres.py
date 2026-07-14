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

    engine, session_factory = create_engine_and_session(database_url)
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
        command = RunCommand(
            run_id="run_concurrent",
            request_fingerprint=hashlib.sha256(b"request").hexdigest(),
            request_json={"input": {}},
            output_uri="file:///exchange/runs/run_concurrent/",
        )
        idempotency_key = "concurrent-key"

        results: list[tuple[str, bool]] = []
        lock = threading.Lock()

        def worker():
            record, created = repository.create_or_get(command, idempotency_key)
            with lock:
                results.append((record.run_id, created))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 2
        # Both threads must observe the same run_id
        assert results[0][0] == results[1][0] == "run_concurrent"
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
