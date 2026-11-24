"""Fixtures for the crawler tests: no network, no keys, no GPU, no real waiting.

The fakes live in ``crawler_fakes.py``. ``crawler_db`` is a fresh Postgres schema on the
loopback test server (``PIPELINE_TEST_DSN``), as ``tests/pipeline`` uses.

There is deliberately no ``__init__.py`` in this directory: it would make the test
directory a package named ``crawler``, shadowing the real one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from typing import LiteralString, cast

import psycopg
import pytest
from crawler_fakes import FakeReserve, FakeResolver, ReplayWeb, VirtualClock
from psycopg import sql
from psycopg.conninfo import make_conninfo

from crawler.slots import HostSlots
from pipeline.store import SCHEMA_SQL


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def web() -> ReplayWeb:
    return ReplayWeb()


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def slots(clock: VirtualClock) -> HostSlots:
    return HostSlots(FakeReserve(clock), clock, interval_s=5.0, concurrency=8)


@pytest.fixture(scope="session")
def test_dsn() -> str:
    dsn = os.environ.get("PIPELINE_TEST_DSN")
    if dsn:
        return dsn
    if os.environ.get("CI") == "true":
        pytest.fail("PIPELINE_TEST_DSN must be set in CI")
    pytest.skip("PIPELINE_TEST_DSN is not set; skipping database tests")


@pytest.fixture
def crawler_dsn(test_dsn: str) -> Generator[str]:
    """A DSN whose connections see one fresh schema with the pipeline schema applied."""
    schema = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(test_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_conninfo(test_dsn, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(cast("LiteralString", SCHEMA_SQL.read_text(encoding="utf-8")))
        yield dsn
    finally:
        with psycopg.connect(test_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def crawler_db(crawler_dsn: str) -> Generator[psycopg.Connection]:
    with psycopg.connect(crawler_dsn, autocommit=True) as conn:
        yield conn
