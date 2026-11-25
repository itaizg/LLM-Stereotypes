"""The run's shared state and the services the tools use.

``RunState`` is the run's compact worklist (research R1): budgets, what each tool found
for each URL in this run, failure counters and the stop reason. The tools read and write
it; the LLM never sees it directly. Gates read it: ``sample_content`` needs this run's
passing probe, and ``write_proposal`` takes every check result from here, never from
the model (research R4).

Budgets are *reserved* under one lock before a paid or counted call starts, and the
unused part is released after it, so concurrent tool calls in one iteration can never
take the run past its limits (FR-002, contracts/tools.md "Budget reservation").
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypeVar

import httpx
import psycopg

from crawler.config import RunConfig
from crawler.contracts import (
    ProbeResult,
    Provider,
    SampleResult,
    StopLimit,
    TermsResult,
    TicketInput,
)
from crawler.lexicon import Lexicon
from crawler.memory import CrawlerMemory, NullMemory
from crawler.netsafe import SafeClient
from crawler.tracing import Tracing

T = TypeVar("T")


class Database:
    """One connection, used from worker threads one call at a time."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable[[psycopg.Connection], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, self.conn)


@dataclass(frozen=True)
class Sampled:
    """A sample's result plus what the stated-exam guard needs, kept in memory only.

    ``folded_text`` is never stored, traced or returned to the model (FR-020): it lives
    for the run so ``write_proposal`` can check that a stated exam name appears on the
    page.
    """

    result: SampleResult
    observed_at: datetime | None
    folded_text: str


@dataclass(frozen=True)
class Classification:
    site_type: str | None
    site_type_confidence: float
    page_type: str | None
    page_type_confidence: float
    topic: str | None
    topic_confidence: float
    input_tokens: int
    output_tokens: int


class Classifier(Protocol):
    """Site, page and topic labels for a de-tagged sample (research R2, R12)."""

    async def classify(self, title: str, url: str, text: str) -> Classification: ...


class LanguageDetector(Protocol):
    def detect(self, text: str) -> tuple[str, float]: ...


@dataclass
class ProviderSwitch:
    at: str
    from_: Provider
    to: Provider
    reason: str

    def as_json(self) -> dict[str, str]:
        return {"at": self.at, "from": self.from_, "to": self.to, "reason": self.reason}


@dataclass
class RunState:
    config: RunConfig
    memory: CrawlerMemory = field(default_factory=NullMemory)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    queries_used: int = 0
    iterations_used: int = 0
    cost_usd: float = 0.0
    reserved_usd: float = 0.0
    stop_limit: StopLimit | None = None
    escalated_tool: str | None = None
    provider: Provider | None = None
    providers_down: set[Provider] = field(default_factory=set)
    provider_switches: list[ProviderSwitch] = field(default_factory=list)
    found: dict[str, TicketInput] = field(default_factory=dict)
    probes: dict[str, ProbeResult] = field(default_factory=dict)
    samples: dict[str, Sampled] = field(default_factory=dict)
    terms: dict[str, TermsResult] = field(default_factory=dict)
    answer_key_domains: set[str] = field(default_factory=set)
    written: dict[str, tuple[str, int, str]] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    started: set[str] = field(default_factory=set)
    failures: dict[str, int] = field(default_factory=dict)
    queries_tried: set[str] = field(default_factory=set)
    dead_hosts: set[str] = field(default_factory=set)
    task_queries: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    tasks_started: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if self.provider is None:
            self.provider = self.config.primary_provider

    # --- budgets ------------------------------------------------------------------

    async def reserve_query(self) -> bool:
        async with self.lock:
            if self.queries_used >= self.config.budget.max_queries:
                self.stop_limit = self.stop_limit or StopLimit.QUERIES
                return False
            self.queries_used += 1
            return True

    async def reserve_cost(self, amount_usd: float) -> bool:
        """Hold ``amount_usd`` against the cost cap, or refuse and stop the run."""
        async with self.lock:
            committed = self.cost_usd + self.reserved_usd
            if committed + amount_usd > self.config.budget.max_cost_usd:
                self.stop_limit = self.stop_limit or StopLimit.COST
                return False
            self.reserved_usd += amount_usd
            return True

    async def settle_cost(self, reserved_usd: float, actual_usd: float) -> None:
        """Release a reservation and charge what the call actually cost."""
        async with self.lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reserved_usd)
            self.cost_usd += actual_usd

    # --- run outcome --------------------------------------------------------------

    @property
    def completeness(self) -> float:
        """The share of URLs this run started work on that got a proposal."""
        if not self.started:
            return 1.0
        done = sum(1 for url in self.started if url in self.written)
        return round(done / len(self.started), 3)

    @property
    def stopped(self) -> bool:
        return self.stop_limit is not None or self.escalated_tool is not None


@dataclass(frozen=True)
class Services:
    """What the tools reach outside the run: network, search, models, store, trace."""

    safe: SafeClient
    lexicon: Lexicon
    tracing: Tracing
    db: Database
    search_client: httpx.AsyncClient
    search_keys: dict[Provider, str]
    classifier: Classifier
    detector: LanguageDetector
