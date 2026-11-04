"""Shared fixtures for the pipeline tests.

Everything here is offline and synthetic (FR-025, FR-027): a local Postgres
reached through ``PIPELINE_TEST_DSN`` (loopback only), a local blob store, a
virtual clock, and a hand-written PDF writer so extraction tests need no real
exam file and no PDF-writing dependency.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Collection, Generator, Sequence
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

import pipeline
from pipeline.blobs import LocalBlobStore

SCHEMA_SQL = Path(pipeline.__file__).parent / "schema.sql"


@pytest.fixture(scope="session")
def test_dsn() -> str:
    """The local Postgres for DB tests.

    Unset locally means DB tests skip. With ``CI=true`` an unset DSN is a hard
    failure, so a required check can never pass by skipping (research R4).
    """
    dsn = os.environ.get("PIPELINE_TEST_DSN")
    if dsn:
        return dsn
    if os.environ.get("CI") == "true":
        pytest.fail("PIPELINE_TEST_DSN must be set in CI")
    pytest.skip("PIPELINE_TEST_DSN is not set; skipping database tests")


@pytest.fixture
def schema_dsn(test_dsn: str) -> Generator[str]:
    """A DSN whose connections all see one fresh schema, dropped afterwards."""
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
def db(schema_dsn: str) -> Generator[psycopg.Connection]:
    """A connection to the fresh test schema, with the pipeline schema applied."""
    with psycopg.connect(schema_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def blob_store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


class FakeClock:
    """A virtual clock: ``sleep`` advances time instantly and records the call.

    Rate-limit tests read ``now()`` and ``sleeps`` instead of waiting, and no
    test ever depends on the wall clock (constitution Principle III).
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def utc_iso(self) -> str:
        return f"2026-01-01T00:00:{int(self._now) % 60:02d}Z"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _minimal_pdf(
    pages: Sequence[str],
    *,
    image_pages: Collection[int] = (),
    ruled_pages: Collection[int] = (),
) -> bytes:
    """Write a tiny valid PDF 1.4 from page texts (``\\n`` separates lines).

    Text is Helvetica (ASCII only). ``image_pages`` (0-based) get an image
    XObject and ``ruled_pages`` get ruling lines, which is what extraction reads
    as figure and table evidence. Written by hand to avoid a dependency (KISS).
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # Patched once the page tree exists.
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    image = add(
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\n"
        b"stream\n\x80\nendstream"
    )
    kids: list[int] = []
    for index, text in enumerate(pages):
        ops: list[str] = []
        y = 740
        for line in text.split("\n"):
            ops.append(f"BT /F1 11 Tf 72 {y} Td ({_escape(line)}) Tj ET")
            y -= 16
        if index in image_pages:
            ops.append("q 50 0 0 50 72 120 cm /Im1 Do Q")
        if index in ruled_pages:
            ops.append("72 300 m 400 300 l S")
            ops.append("72 280 m 400 280 l S")
            ops.append("72 300 m 72 280 l S")
            ops.append("400 300 m 400 280 l S")
        stream = "\n".join(ops).encode("latin-1")
        content = add(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
        page = add(
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> "
            f"/XObject << /Im1 {image} 0 R >> >> /Contents {content} 0 R >>".encode()
        )
        kids.append(page)

    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    refs = " ".join(f"{k} 0 R" for k in kids)
    objects[tree - 1] = f"<< /Type /Pages /Kids [{refs}] /Count {len(kids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _seed_edition(
    conn: psycopg.Connection,
    *,
    exam_id: str = "sat",
    year: int = 2020,
    sitting: str = "main",
    status: str = "ingested",
    language: str = "en",
) -> str:
    """Insert an exam (if absent) and one edition directly; return its edition_id.

    Direct SQL, not a stage, so tests of later stages do not depend on the
    catalog stage. Text is synthetic (FR-025).
    """
    conn.execute(
        "INSERT INTO exams (exam_id, name, body, language, domains, publishes_keys) "
        "VALUES (%s, %s, %s, %s, %s, true) ON CONFLICT DO NOTHING",
        (
            exam_id,
            f"Synthetic {exam_id}",
            "Synthetic Body",
            language,
            [f"{exam_id}.exams.test"],
        ),
    )
    edition_id = f"{exam_id}-{year}-{sitting}"
    conn.execute(
        "INSERT INTO exam_editions (edition_id, exam_id, year, sitting, kind, status, "
        "release_date, official_url, lineage, language, evidence) "
        "VALUES (%s, %s, %s, %s, 'administered', %s, '2020-01-01', %s, 'synthetic', "
        "%s, "
        '\'[{"type": "release_page"}]\'::jsonb) ON CONFLICT DO NOTHING',
        (
            edition_id,
            exam_id,
            year,
            sitting,
            status,
            f"https://{exam_id}.exams.test/{year}",
            language,
        ),
    )
    return edition_id


# Test modules cannot import from conftest (tests/ has no package), so shared
# helpers are exposed as factory fixtures.
SeedEdition = Callable[..., str]
MakePdf = Callable[..., bytes]


@pytest.fixture
def seed_edition() -> SeedEdition:
    return _seed_edition


@pytest.fixture
def make_pdf() -> MakePdf:
    return _minimal_pdf
