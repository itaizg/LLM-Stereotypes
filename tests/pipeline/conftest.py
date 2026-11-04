"""Shared fixtures for the pipeline tests.

Everything here is offline and synthetic (FR-025, FR-027): a local Postgres
reached through ``PIPELINE_TEST_DSN`` (loopback only), a local blob store, a
virtual clock, and a hand-written PDF writer so extraction tests need no real
exam file and no PDF-writing dependency.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Collection, Generator, Sequence
from datetime import date
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import urlparse

import httpx
import psycopg
import pytest
import yaml
from psycopg import sql
from psycopg.conninfo import make_conninfo

import pipeline
from pipeline.blobs import LocalBlobStore
from pipeline.manifests import append_event, source_id_for

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


# --- a synthetic catalog and ledger -------------------------------------------

EXAMS: tuple[str, ...] = (
    "sat",
    "psychometric",
    "csat",
    "gaokao",
    "common_test",
    "enem",
)
YEARS: range = range(2015, 2026)


def domain_of(exam_id: str) -> str:
    """The synthetic official domain of an exam (DNS names have no underscores)."""
    return f"{exam_id.replace('_', '-')}.exams.test"


class DatasetBuilder:
    """Build a valid synthetic ``catalog.yaml`` and ``ledger.jsonl``.

    Every one of the 66 cells starts as a ``not_released`` edition whose evidence
    is an official statement from an approved source, which is the smallest
    catalog that passes validation. Tests mutate ``catalog`` and call ``save``
    to break exactly one rule. All text is invented (FR-025).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.catalog_path = root / "catalog.yaml"
        self.ledger_path = root / "ledger.jsonl"
        self._tick = 0
        self.catalog: dict[str, Any] = {
            "version": 1,
            "translation": {
                "model": "synthetic-model",
                "base_url": "https://mt.example.test/v1/",
            },
            "defaults": {"rate_limit_seconds": 5},
            "exams": {},
            "editions": [],
        }
        for exam_id in EXAMS:
            self._add_exam(exam_id)
        self.save()

    # -- helpers

    def at(self) -> str:
        self._tick += 1
        return f"2026-01-01T00:00:{self._tick % 60:02d}Z"

    @staticmethod
    def edition_id(exam_id: str, year: int, sitting: str = "main") -> str:
        return f"{exam_id}-{year}-{sitting}"

    def edition(self, edition_id: str) -> dict[str, Any]:
        for entry in self.catalog["editions"]:
            if entry["edition_id"] == edition_id:
                return entry
        raise KeyError(edition_id)

    def save(self) -> None:
        self.catalog_path.write_text(
            yaml.safe_dump(self.catalog, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # -- the ledger

    def add_source(
        self,
        url: str,
        edition_id: str,
        role: str,
        *,
        method: str = "manual",
        found_on: str | None = None,
        fetched: bytes | None = None,
        media_type: str = "application/pdf",
        published: str | None = None,
        approve: bool = True,
    ) -> str:
        """Append candidate (and review, and fetch) events; return the source id."""
        source_id = source_id_for(url)
        append_event(
            self.ledger_path,
            "candidate",
            {
                "source_id": source_id,
                "url": url,
                "domain": urlparse(url).hostname or "",
                "method": method,
                "found_on": found_on,
                "auto_decision": None,
            },
            at=self.at(),
        )
        if approve:
            self.review(source_id, edition_id, role, published=published)
        if fetched is not None:
            self.fetch_event(source_id, fetched, media_type)
        return source_id

    def review(
        self,
        source_id: str,
        edition_id: str,
        role: str,
        *,
        published: str | None = None,
        approve: bool = True,
        reason: str | None = None,
    ) -> None:
        append_event(
            self.ledger_path,
            "review",
            {
                "source_id": source_id,
                "decision": "approved" if approve else "rejected",
                "edition_id": edition_id if approve else None,
                "role": role if approve else None,
                "published": published,
                "reason": reason,
            },
            at=self.at(),
        )

    def fetch_event(self, source_id: str, data: bytes, media_type: str) -> None:
        append_event(
            self.ledger_path,
            "fetch",
            {
                "source_id": source_id,
                "outcome": "fetched",
                "sha256": hashlib.sha256(data).hexdigest(),
                "media_type": media_type,
                "bytes": len(data),
                "http_status": 200,
            },
            at=self.at(),
        )

    # -- the catalog

    def _add_exam(self, exam_id: str) -> None:
        domain = domain_of(exam_id)
        policy = self.add_source(
            f"https://{domain}/keys-policy",
            self.edition_id(exam_id, 2015),
            "statement",
        )
        self.catalog["exams"][exam_id] = {
            "name": f"Synthetic {exam_id}",
            "body": "Synthetic Body",
            "language": "en" if exam_id == "sat" else "ko",
            "publishes_keys": True,
            "keys_evidence": {"source_id": policy, "note": "Keys are published"},
            "domains": [domain],
            "seeds": [
                {"url": f"https://{domain}/archive", "complete_listing": True},
                {"url": f"https://{domain}/news", "complete_listing": False},
            ],
            "parse": {
                "item_marker": r"^(\d+)\.",
                "choice_markers": ["A", "B", "C", "D"],
                "columns": 1,
                "key_table": r"^(\d+)\s+([A-D])$",
            },
        }
        for year in YEARS:
            edition_id = self.edition_id(exam_id, year)
            notice = self.add_source(
                f"https://{domain}/notice/{year}", edition_id, "statement"
            )
            self.catalog["editions"].append(
                {
                    "edition_id": edition_id,
                    "exam": exam_id,
                    "year": year,
                    "sitting": "main",
                    "kind": "administered",
                    "status": "not_released",
                    "lineage": "synthetic",
                    "language": self.catalog["exams"][exam_id]["language"],
                    "evidence": [
                        {
                            "type": "statement",
                            "source_id": notice,
                            "note": "not published",
                        }
                    ],
                }
            )

    def make_ingested(
        self,
        edition_id: str,
        *,
        paper: bytes | None = b"%PDF synthetic paper",
        key: bytes | None = b"%PDF synthetic key",
    ) -> dict[str, str]:
        """Turn an edition into ``ingested`` with approved, fetched paper and key."""
        entry = self.edition(edition_id)
        domain = domain_of(entry["exam"])
        ids: dict[str, str] = {}
        if paper is not None:
            ids["paper"] = self.add_source(
                f"https://{domain}/{edition_id}/paper.pdf",
                edition_id,
                "paper",
                fetched=paper,
            )
        if key is not None:
            ids["key"] = self.add_source(
                f"https://{domain}/{edition_id}/key.pdf",
                edition_id,
                "key",
                fetched=key,
            )
        release = self.add_source(
            f"https://{domain}/{edition_id}/release", edition_id, "statement"
        )
        entry.update(
            {
                "status": "ingested",
                "release_date": date(2020, 1, 1),
                "official_url": f"https://{domain}/{edition_id}/release",
                "evidence": [{"type": "release_page", "source_id": release}],
            }
        )
        return ids


@pytest.fixture
def dataset(tmp_path: Path) -> DatasetBuilder:
    return DatasetBuilder(tmp_path / "data")


# --- an offline web ----------------------------------------------------------


class Web:
    """An in-memory website behind ``httpx.MockTransport`` (no network, FR-027).

    ``calls`` records ``(url, virtual time)`` for every request, so tests can
    assert both what was never requested and how far apart requests were.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.calls: list[tuple[str, float]] = []

    def add(
        self,
        url: str,
        body: bytes | str = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self._routes[url] = (status, headers or {}, data)

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((url, self._clock.now()))
        status, headers, body = self._routes.get(url, (404, {}, b""))
        return httpx.Response(status, headers=headers, content=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), follow_redirects=False
        )


@pytest.fixture
def web(clock: FakeClock) -> Web:
    return Web(clock)
