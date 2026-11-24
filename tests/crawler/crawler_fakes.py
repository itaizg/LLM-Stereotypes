"""Fakes shared by the crawler tests (imported as ``crawler_fakes``).

The name avoids clashing with ``tests/dagster/fakes.py`` when one pytest session
collects both directories.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import psycopg
from psycopg.types.json import Jsonb

from crawler.config import RunConfig
from crawler.contracts import Provider, normalise_url, proposal_id_for
from crawler.lexicon import load_lexicon
from crawler.memory import DeadHost, QueryTried, QueryYield
from crawler.netsafe import SafeClient
from crawler.runstate import (
    Classification,
    Classifier,
    Database,
    LanguageDetector,
    RunState,
    Services,
)
from crawler.slots import HostSlots
from crawler.tracing import Tracing

EPOCH = 1_700_000_000.0


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def wall(self) -> datetime:
        return datetime.fromtimestamp(EPOCH + self.t, UTC)


class FakeResolver:
    def __init__(self, table: dict[str, list[str]] | None = None) -> None:
        self.table = dict(table or {})
        self.asked: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.asked.append(host)
        if host in self.table:
            return self.table[host]
        digest = hashlib.sha256(host.encode()).digest()
        return [f"23.{digest[0]}.{digest[1]}.{max(1, digest[2])}"]


@dataclass
class Page:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class ReplayWeb:
    """An in-memory web. Routes are full URLs; anything else is a 404."""

    def __init__(self) -> None:
        self.pages: dict[str, Page] = {}
        self.log: list[str] = []
        self.peers: list[str] = []
        self.raises: dict[str, Exception] = {}

    def add(
        self,
        url: str,
        body: bytes | str = b"",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.pages[url] = Page(status, data, dict(headers or {}))

    def redirect(self, url: str, to: str, status: int = 302) -> None:
        self.add(url, status=status, headers={"location": to})

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        path = request.url.raw_path.decode("ascii")
        url = f"{request.url.scheme}://{host}{path}"
        self.log.append(url)
        self.peers.append(request.url.host)
        if url in self.raises:
            raise self.raises[url]
        page = self.pages.get(url)
        if page is None:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(page.status, headers=page.headers, content=page.body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class FakeReserve:
    """``pipeline.store.reserve_host_slot`` on the virtual clock."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.last: dict[str, float] = {}

    def __call__(self, host: str, interval_s: float) -> tuple[float, datetime]:
        now = self.clock.t
        previous = self.last.get(host)
        slot = now if previous is None else max(now, previous + interval_s)
        self.last[host] = slot
        return slot - now, datetime.fromtimestamp(EPOCH + slot, UTC)


def safe_client(
    client: httpx.AsyncClient,
    resolver: FakeResolver,
    slots: HostSlots | None,
    clock: VirtualClock,
) -> SafeClient:
    return SafeClient(client, resolver, slots, monotonic=clock.now)


# --- builders for tool tests ----------------------------------------------------------

RUN_ID = "cr_20261123T101500_a3f9"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FakeClassifier:
    def __init__(self, page_type: str = "past_paper", confidence: float = 0.9) -> None:
        self.page_type = page_type
        self.confidence = confidence
        self.calls: list[str] = []

    async def classify(self, title: str, url: str, text: str) -> Classification:
        self.calls.append(url)
        return Classification(
            site_type="exam_body",
            site_type_confidence=0.9,
            page_type=self.page_type,
            page_type_confidence=self.confidence,
            topic="reading",
            topic_confidence=0.8,
            input_tokens=1_000,
            output_tokens=100,
        )


class FakeDetector:
    def __init__(self, language: str = "en", confidence: float = 0.95) -> None:
        self.language = language
        self.confidence = confidence

    def detect(self, text: str) -> tuple[str, float]:
        return self.language, self.confidence


def make_state(**config: object) -> RunState:
    return RunState(
        config=RunConfig.model_validate({"run_id": RUN_ID, **config}), now=lambda: NOW
    )


def make_services(
    client: httpx.AsyncClient,
    resolver: FakeResolver,
    clock: VirtualClock,
    conn: psycopg.Connection | None = None,
    *,
    classifier: Classifier | None = None,
    detector: LanguageDetector | None = None,
    keys: dict[Provider, str] | None = None,
) -> Services:
    slots = HostSlots(FakeReserve(clock), clock, interval_s=5.0, concurrency=8)
    return Services(
        safe=SafeClient(client, resolver, slots, monotonic=clock.now),
        lexicon=load_lexicon(),
        tracing=Tracing(RUN_ID, io.StringIO(), env={}),
        db=Database(cast("psycopg.Connection", conn)),
        search_client=client,
        search_keys=keys
        if keys is not None
        else {Provider.TAVILY: "t", Provider.BRAVE: "b"},
        classifier=classifier if classifier is not None else FakeClassifier(),
        detector=detector if detector is not None else FakeDetector(),
    )


# --- seeded proposals for review and import tests -------------------------------------


def add_run(conn: psycopg.Connection, run_id: str = RUN_ID) -> None:
    conn.execute(
        "INSERT INTO crawler_runs (run_id, config, approved_by, approved_at, status, "
        "started_at) VALUES (%s, '{}', 't', now(), 'completed', now())",
        (run_id,),
    )


def add_proposal(
    conn: psycopg.Connection,
    url: str,
    *,
    status: str = "passed",
    digest: str | None = None,
    origin: str = "third_party",
    flags: tuple[str, ...] = (),
    title: str = "",
    language: str = "en",
    terms: str = "",
) -> None:
    norm = normalise_url(url)
    labels: dict[str, Any] = {
        "origin": origin,
        "kind": "pdf",
        "language": language,
        "page_type": "past_paper",
        "page_type_confidence": 0.9,
        "flags": list(flags),
        "keyword_counts": {"exam": 2},
        "exam_name_stated": title or None,
        "terms_excerpt": terms,
        "terms_restrictive": bool(terms),
        "key_status": "linked_file",
    }
    checks = {
        "reachable": {"result": "passed"},
        "downloadable": {"result": "passed"},
        "mcq": {
            "result": "passed" if status == "passed" else "failed",
            "item_count": 9,
        },
        "key_located": {
            "result": "passed",
            "key_status": "linked_file",
            "evidence": "javascript:alert(1)",
        },
    }
    conn.execute(
        "INSERT INTO crawler_proposals (proposal_id, revision, normalised_url, url, "
        "domain, "
        "run_id, content_hash, labels, checks, validation_status, reason, "
        "reason_class, "
        "material_fingerprint, observed_at) VALUES (%s, 1, %s, %s, 'd', %s, %s, %s, "
        "%s, %s, "
        "%s, %s, 'f', now())",
        (
            proposal_id_for(norm),
            norm,
            url,
            RUN_ID,
            digest or f"sha256:{norm}",
            Jsonb(labels),
            Jsonb(checks),
            status,
            None if status == "passed" else "not_mcq_structure",
            None if status == "passed" else "permanent",
        ),
    )


class InMemoryMemory:
    """``CrawlerMemory`` kept in a dict, shared between replay runs (US4 tests)."""

    def __init__(self) -> None:
        self.available = True
        self.queries: list[QueryTried] = []
        self.yields: list[QueryYield] = []
        self.dead: list[DeadHost] = []

    def queries_tried(self) -> set[str]:
        from crawler.memory import query_key

        return {query_key(q.query) for q in self.queries}

    def dead_hosts(self, now: datetime) -> dict[str, DeadHost]:
        return {d.host: d for d in self.dead if d.until > now}

    def record_query(self, entry: QueryTried) -> None:
        self.queries.append(entry)

    def record_yield(self, entry: QueryYield) -> None:
        self.yields.append(entry)

    def record_dead_host(self, entry: DeadHost) -> None:
        self.dead.append(entry)
