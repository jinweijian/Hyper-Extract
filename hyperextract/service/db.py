from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_engine_and_session(database_url: str, *, for_tests: bool = False):
    options = {}
    if for_tests and database_url.startswith("sqlite"):
        options = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    engine = create_engine(database_url, **options)
    from . import db_models  # noqa: F401

    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
