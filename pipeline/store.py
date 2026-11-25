"""Postgres access: connection, the write lock, and idempotent reconcile.

Every stage is a *reconciler*: it computes the rows it owns for one scope as a
pure function of its inputs, then writes only the difference inside one
transaction. That single mechanism gives idempotence (FR-019: unchanged inputs
write nothing), crash safety (a scope is fully old or fully new), and
deterministic rebuilds, without a workflow engine (plan, idea 2).

All SQL is parameterized (FR-031, CDR-2026-021). Table and column names cannot
be parameters, so they are only ever taken from the live schema
(``information_schema``), never from caller strings, and are quoted with
``psycopg.sql.Identifier``.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

SCHEMA_SQL = Path(__file__).parent / "schema.sql"
MIGRATIONS = Path(__file__).parent / "migrations"

# The version ``schema.sql`` creates, and the migrations bringing an older store to it:
# each (version, file) runs when the store is below that version, in order.
SCHEMA_VERSION = 4
_MIGRATIONS = (
    (3, MIGRATIONS / "003_crawler.sql"),
    (4, MIGRATIONS / "004_crawler_orchestration.sql"),
)

# A stored request time more than this far ahead of now is clock skew, not a pending
# reservation, and counts as now (a reservation is at most a few intervals ahead).
MAX_SLOT_AHEAD_S = 600.0

# One fixed advisory-lock key for the whole pipeline (research R16).
_LOCK_KEY = 0x706970656C696E65  # "pipeline" in ASCII; fits a signed bigint


class IntegrityRefusal(Exception):
    """A rule was broken that the pipeline refuses to continue past (exit code 3)."""


class LockHeld(Exception):
    """Another pipeline command holds the write lock (exit code 4)."""


class ConnectionFailed(Exception):
    """The database could not be reached; the message never contains the DSN."""


def redact(message: str, dsn: str) -> str:
    """Remove a DSN, and any password inside it, from a message (FR-028)."""
    cleaned = message.replace(dsn, "<dsn>")
    for secret in re.findall(r"//[^:/@\s]*:([^@/\s]+)@", dsn):
        cleaned = cleaned.replace(secret, "***")
    for secret in re.findall(r"password=(\S+)", dsn):
        cleaned = cleaned.replace(secret, "***")
    return cleaned


def connect(dsn: str, *, autocommit: bool = True) -> psycopg.Connection:
    """Open a connection, turning failures into a DSN-free ``ConnectionFailed``."""
    try:
        return psycopg.connect(dsn, autocommit=autocommit)
    except psycopg.Error as error:
        raise ConnectionFailed(redact(str(error), dsn)) from None


def schema_version(conn: psycopg.Connection) -> int:
    """The store's schema version: 0 when it predates ``schema_version``."""
    row = conn.execute("SELECT to_regclass('schema_version')").fetchone()
    if row is None or row[0] is None:
        return 0
    found = conn.execute("SELECT version FROM schema_version").fetchone()
    return int(found[0]) if found is not None else 0


def apply_schema(conn: psycopg.Connection) -> bool:
    """Bring the store to ``SCHEMA_VERSION``; return True if anything was applied.

    An empty store gets ``schema.sql``, which is the latest schema. An older store
    (spec 001/002, no ``schema_version`` row) gets ``schema.sql`` for its missing
    tables and then each migration above its version for what that file cannot
    change in place (new columns, replaced constraints), all in one transaction, so
    a failure leaves the store as it was. A current store is left alone.
    """
    version = schema_version(conn)
    if version >= SCHEMA_VERSION:
        return False
    row = conn.execute("SELECT to_regclass('exam_editions')").fetchone()
    is_empty = row is None or row[0] is None
    with conn.transaction():
        # The DDL is our own committed SQL, never user input; psycopg's typing asks
        # for a literal string to keep injectable text out of execute().
        conn.execute(cast("LiteralString", SCHEMA_SQL.read_text(encoding="utf-8")))
        if not is_empty:
            for target, migration in _MIGRATIONS:
                if version < target:
                    sql_text = migration.read_text(encoding="utf-8")
                    conn.execute(cast("LiteralString", sql_text))
    return True


@contextmanager
def write_lock(conn: psycopg.Connection) -> Generator[None]:
    """Hold the pipeline-wide write lock for the whole of a writing command.

    Session-level and non-blocking: a second writer fails fast with ``LockHeld``
    instead of waiting, and Postgres releases the lock if the process dies.
    """
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,)).fetchone()
    if row is None or not row[0]:
        raise LockHeld("another pipeline command is running")
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def pipeline_version(repo_root: Path | None = None) -> str:
    """Return the git SHA of ``HEAD``, suffixed ``-dirty`` if pipeline code changed.

    Only pipeline code and the schema count; stages write the catalog, ledger,
    and fingerprint files during normal work, so those do not make a run dirty.
    A dirty version is refused at snapshot time, so published data never names
    code that was not committed (research R7).
    """
    root = repo_root or Path(__file__).resolve().parent.parent
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "pipeline/*.py", "pipeline/schema.sql"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return f"{sha}-dirty" if status else sha


@dataclass(frozen=True)
class Counts:
    """Rows written by one reconcile."""

    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.deleted + other.deleted,
        )

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.deleted


@dataclass(frozen=True)
class StageSummary:
    """The one summary line every stage ends with (contracts/cli.md)."""

    stage: str
    editions: int = 0
    counts: Counts = Counts()
    failures_added: int = 0
    failures_removed: int = 0

    def line(self) -> str:
        return (
            f"{self.stage}: editions={self.editions} inserted={self.counts.inserted} "
            f"updated={self.counts.updated} deleted={self.counts.deleted} "
            f"failures=+{self.failures_added}/-{self.failures_removed}"
        )


@dataclass(frozen=True)
class _Columns:
    names: tuple[str, ...]
    jsonb: frozenset[str]


def _table_columns(conn: psycopg.Connection, table: str) -> _Columns:
    """Read a table's real columns; this is the only source of identifiers."""
    rows = conn.execute(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    if not rows:
        raise ValueError(f"unknown table: {table!r}")
    return _Columns(
        names=tuple(name for name, _ in rows),
        jsonb=frozenset(name for name, kind in rows if kind == "jsonb"),
    )


def _checked(columns: _Columns, names: Sequence[str]) -> None:
    unknown = [n for n in names if n not in columns.names]
    if unknown:
        raise ValueError(f"unknown columns: {unknown!r}")


def _adapt(columns: _Columns, name: str, value: object) -> object:
    return Jsonb(value) if name in columns.jsonb and value is not None else value


def reconcile(
    conn: psycopg.Connection,
    table: str,
    *,
    key: Sequence[str],
    scope: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    retire: bool = False,
) -> Counts:
    """Make ``table`` within ``scope`` equal ``rows``, writing only differences.

    Inserts missing rows, updates rows whose given columns differ, and removes
    rows no longer produced. With ``retire=True`` a row that is no longer
    produced is marked ``retired`` instead of deleted, because append-only
    tables (keys, translations) reference it; a row produced again is un-retired.
    Everything happens in one transaction, so a failure leaves the scope as it
    was (data-model: one transaction per edition).
    """
    columns = _table_columns(conn, table)
    _checked(columns, [*key, *scope])
    produced = [dict(row) for row in rows]
    if retire:
        for row in produced:
            row["retired"] = False
    for row in produced:
        _checked(columns, list(row))
        for name, value in scope.items():
            if row.get(name, value) != value:
                raise ValueError(f"row is outside the reconcile scope on {name!r}")
            row[name] = value

    table_id = sql.Identifier(table)
    where = (
        sql.SQL(" WHERE ")
        + sql.SQL(" AND ").join(
            sql.SQL("{} = %s").format(sql.Identifier(c)) for c in scope
        )
        if scope
        else sql.SQL("")
    )
    with conn.transaction():
        existing_rows = conn.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in columns.names), table_id
            )
            + where,
            tuple(scope.values()),
        ).fetchall()
        existing = {
            tuple(rec[columns.names.index(k)] for k in key): dict(
                zip(columns.names, rec, strict=True)
            )
            for rec in existing_rows
        }
        seen: set[tuple[object, ...]] = set()
        inserted = updated = deleted = 0
        for row in produced:
            ident = tuple(row[k] for k in key)
            if ident in seen:
                raise ValueError(f"duplicate key in produced rows: {ident!r}")
            seen.add(ident)
            current = existing.get(ident)
            if current is None:
                names = list(row)
                conn.execute(
                    sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        table_id,
                        sql.SQL(", ").join(sql.Identifier(c) for c in names),
                        sql.SQL(", ").join(sql.Placeholder() for _ in names),
                    ),
                    tuple(_adapt(columns, c, row[c]) for c in names),
                )
                inserted += 1
                continue
            changed = [c for c in row if c not in key and current[c] != row[c]]
            if changed:
                conn.execute(
                    sql.SQL("UPDATE {} SET {} WHERE {}").format(
                        table_id,
                        sql.SQL(", ").join(
                            sql.SQL("{} = %s").format(sql.Identifier(c))
                            for c in changed
                        ),
                        sql.SQL(" AND ").join(
                            sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key
                        ),
                    ),
                    (
                        *(_adapt(columns, c, row[c]) for c in changed),
                        *(row[k] for k in key),
                    ),
                )
                updated += 1
        for ident, current in existing.items():
            if ident in seen:
                continue
            if retire:
                if current["retired"]:
                    continue
                statement = sql.SQL("UPDATE {} SET retired = true WHERE {}")
            else:
                statement = sql.SQL("DELETE FROM {} WHERE {}")
            conn.execute(
                statement.format(
                    table_id,
                    sql.SQL(" AND ").join(
                        sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key
                    ),
                ),
                ident,
            )
            deleted += 1
    return Counts(inserted, updated, deleted)


def insert_if_new(
    conn: psycopg.Connection, table: str, rows: Sequence[Mapping[str, object]]
) -> int:
    """Insert rows that do not violate a unique constraint; return how many were new.

    For the append-only tables (keys, translations): re-running adds nothing.
    """
    columns = _table_columns(conn, table)
    added = 0
    with conn.transaction():
        for row in rows:
            names = list(row)
            _checked(columns, names)
            cursor = conn.execute(
                sql.SQL(
                    "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING"
                ).format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in names),
                    sql.SQL(", ").join(sql.Placeholder() for _ in names),
                ),
                tuple(_adapt(columns, c, row[c]) for c in names),
            )
            added += cursor.rowcount
    return added


# --- request spacing across commands (spec 002, P3) -----------------------------


def load_host_requests(conn: psycopg.Connection) -> dict[str, float]:
    """Each host's last request time, as epoch seconds."""
    rows = conn.execute(
        "SELECT host, extract(epoch FROM last_request_at)::float8 FROM host_requests"
    ).fetchall()
    return {str(host): float(at) for host, at in rows}


def record_host_request(conn: psycopg.Connection, host: str, at_epoch: float) -> None:
    """Upsert one host's last request time (parameterized, FR-031).

    The stored time never moves backwards: a wall clock that was set back between
    two commands must not shorten the wait the later time already earned.
    """
    conn.execute(
        "INSERT INTO host_requests (host, last_request_at) "
        "VALUES (%s, to_timestamp(%s)) "
        "ON CONFLICT (host) DO UPDATE SET last_request_at = "
        "GREATEST(host_requests.last_request_at, EXCLUDED.last_request_at)",
        (host, at_epoch),
    )


def reserve_host_slot(
    conn: psycopg.Connection,
    host: str,
    interval_s: float,
    *,
    at_epoch: float | None = None,
) -> tuple[float, datetime]:
    """Atomically reserve the next request slot for ``host``; return (wait_s, slot).

    One statement (spec 003 research R6): the slot is the later of now and the
    host's previous slot plus ``interval_s``, and it is stored before anyone sends,
    so concurrent processes (crawler runs, the downloader) queue behind each other
    instead of reading the same old time. Postgres serialises the upsert on the
    host's row. "Now" is the database clock unless ``at_epoch`` is given (tests and
    callers with an injected wall clock). A stored time more than
    ``MAX_SLOT_AHEAD_S`` ahead is clock skew and counts as now.
    """
    row = conn.execute(
        "WITH now_ AS (SELECT COALESCE(to_timestamp(%(at)s::float8), "
        "clock_timestamp()) AS t) "
        "INSERT INTO host_requests AS h (host, last_request_at) "
        "SELECT %(host)s, t FROM now_ "
        "ON CONFLICT (host) DO UPDATE SET last_request_at = GREATEST("
        "(SELECT t FROM now_), "
        "CASE WHEN h.last_request_at > (SELECT t FROM now_) "
        "+ make_interval(secs => %(ahead)s) "
        "THEN (SELECT t FROM now_) ELSE h.last_request_at END "
        "+ make_interval(secs => %(interval)s)) "
        "RETURNING last_request_at, "
        "extract(epoch FROM last_request_at - (SELECT t FROM now_))::float8",
        {
            "at": at_epoch,
            "host": host,
            "ahead": MAX_SLOT_AHEAD_S,
            "interval": interval_s,
        },
    ).fetchone()
    if row is None:  # pragma: no cover - INSERT ... RETURNING always returns a row
        raise RuntimeError("host slot reservation returned no row")
    slot, wait = row
    return max(0.0, float(wait)), slot


class PostgresHostLedger:
    """``web.HostLedger`` over Postgres: every request reserves its slot atomically.

    Reserving (rather than reading the table once and writing after each request)
    keeps the spacing correct when other processes use the same host at the same
    time, such as a crawler run beside ``pipeline fetch`` (spec 003 FR-010). ``wall``
    stands in for the database clock in tests; ``None`` uses the database clock.
    """

    def __init__(
        self, conn: psycopg.Connection, wall: Callable[[], float] | None = None
    ) -> None:
        self._conn = conn
        self._wall = wall

    def reserve(self, host: str, interval_s: float) -> float:
        at = self._wall() if self._wall is not None else None
        wait, _slot = reserve_host_slot(self._conn, host, interval_s, at_epoch=at)
        return wait
