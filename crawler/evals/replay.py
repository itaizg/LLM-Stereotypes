"""Offline replay eval (FR-035): the whole agent over recorded, synthetic responses.

Everything outside the process is replaced, so the eval runs in CI with no network, no
keys and no model:

- **The web** is a ``manifest.yaml`` of synthetic pages, robots files, redirects and
  search responses (never real exam text, FR-020). PDFs are built from text lines.
- **DNS** maps each fixture host to an address; one maps to a private address.
- **The agent model** is a scripted policy: ``DiligentAgent`` follows the system
  prompt's workflow; ``AdversarialAgent`` tries to skip the gates.
- **The classifier and language detector** return the labels the manifest gives.

``run_replay`` runs one crawl against a store and returns what the eval asserts on:
the proposals and tickets written, every request sent, and the trace.
"""

from __future__ import annotations

import io
import json
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg
import yaml
from langchain_core.messages import AIMessage, BaseMessage

from crawler.config import RunConfig
from crawler.contracts import (
    CheckResult,
    HandoffTicket,
    Origin,
    PageType,
    Provider,
    domain_of,
    normalise_url,
)
from crawler.graph import Crawler
from crawler.lexicon import load_lexicon
from crawler.memory import CrawlerMemory, NullMemory
from crawler.netsafe import SafeClient
from crawler.runstate import Classification, Database, RunState, Services
from crawler.search import BRAVE_URL, TAVILY_URL
from crawler.slots import HostSlots
from crawler.tracing import Tracing

EPOCH = datetime(2026, 11, 23, tzinfo=UTC)


# --- synthetic PDFs ----------------------------------------------------------------


def minimal_pdf(lines: Sequence[str]) -> bytes:
    """A valid one-page PDF showing ``lines`` in Helvetica (ASCII text only)."""

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 10 Tf", "12 TL", "40 800 Td"]
    for line in lines:
        ops.append(f"({esc(line)}) Tj T*")
    ops.append("ET")
    stream = zlib.compress("\n".join(ops).encode("latin-1"))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
        + stream
        + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return out.getvalue()


# --- fixtures -------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class Expected:
    url: str
    status: str
    reason: str | None = None
    key_status: str | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fixtures:
    pages: dict[str, Response]
    hosts: dict[str, list[str]]
    searches: list[dict[str, Any]]
    expected: list[Expected]
    labels: dict[str, dict[str, Any]]
    languages: list[tuple[str, str, float]]
    official_domains: frozenset[str]
    stated: dict[str, dict[str, Any]]
    stems: tuple[str, ...]
    forbidden: frozenset[str]


def _body(entry: dict[str, Any]) -> bytes:
    if "pdf_lines" in entry:
        return minimal_pdf([str(line) for line in entry["pdf_lines"]])
    return str(entry.get("body", "")).encode("utf-8")


def load_fixtures(directory: Path) -> Fixtures:
    raw = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
    pages: dict[str, Response] = {}
    for entry in raw["pages"]:
        headers = {
            str(k).lower(): str(v) for k, v in (entry.get("headers") or {}).items()
        }
        if "redirect" in entry:
            headers["location"] = entry["redirect"]
        body = _body(entry)
        if "same_as" in entry:
            body = pages[entry["same_as"]].body
        pages[entry["url"]] = Response(int(entry.get("status", 200)), headers, body)
    expected = [
        Expected(
            url=e["url"],
            status=e["status"],
            reason=e.get("reason"),
            key_status=e.get("key_status"),
            flags=tuple(e.get("flags", ())),
        )
        for e in raw["expected"]
    ]
    return Fixtures(
        pages=pages,
        hosts={
            str(k): [str(a) for a in v] for k, v in (raw.get("hosts") or {}).items()
        },
        searches=list(raw["searches"]),
        expected=expected,
        labels={str(k): dict(v) for k, v in (raw.get("labels") or {}).items()},
        languages=[
            (str(r["contains"]), str(r["code"]), float(r["confidence"]))
            for r in raw["languages"]
        ],
        official_domains=frozenset(raw.get("official_domains", ())),
        stated={str(k): dict(v) for k, v in (raw.get("stated") or {}).items()},
        stems=tuple(raw.get("stems", ())),
        forbidden=frozenset(raw.get("forbidden_requests", ())),
    )


class ReplayWeb:
    """Serves the fixture pages and both search APIs; logs every request."""

    def __init__(self, fixtures: Fixtures) -> None:
        self.fixtures = fixtures
        self.log: list[str] = []
        self.search_calls: list[tuple[str, str]] = []

    def _search(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == urlsplit(TAVILY_URL).hostname:
            provider = "tavily"
            query = json.loads(request.content)["query"]
        else:
            provider = "brave"
            query = request.url.params.get("q", "")
        self.search_calls.append((provider, query))
        for search in self.fixtures.searches:
            if search["query"] != query:
                continue
            answer = (search.get("responses") or {}).get(provider)
            if isinstance(answer, int):
                return httpx.Response(answer, json={"error": "replayed"})
            urls = [str(u) for u in search.get("results", [])]
            rows = [{"url": u, "title": f"Result {n}"} for n, u in enumerate(urls, 1)]
            data = (
                {"results": rows}
                if provider == "tavily"
                else {"web": {"results": rows}}
            )
            return httpx.Response(200, json=data)
        return httpx.Response(200, json={"results": []} if provider == "tavily" else {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in {
            urlsplit(TAVILY_URL).hostname,
            urlsplit(BRAVE_URL).hostname,
        }:
            return self._search(request)
        host = request.headers["host"]
        url = f"{request.url.scheme}://{host}{request.url.raw_path.decode('ascii')}"
        self.log.append(url)
        page = self.fixtures.pages.get(url)
        if page is None:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(page.status, headers=page.headers, content=page.body)


class FixedResolver:
    def __init__(self, hosts: dict[str, list[str]]) -> None:
        self.hosts = hosts

    async def resolve(self, host: str) -> list[str]:
        return self.hosts.get(host, ["23.10.20.30"])


class ReplayClassifier:
    def __init__(self, labels: dict[str, dict[str, Any]]) -> None:
        self.labels = labels
        self.calls: list[str] = []

    async def classify(self, title: str, url: str, text: str) -> Classification:
        self.calls.append(url)
        found = self.labels.get(url, {})
        return Classification(
            site_type=found.get("site_type", "other"),
            site_type_confidence=float(found.get("site_type_confidence", 0.9)),
            page_type=found.get("page_type", PageType.ARTICLE),
            page_type_confidence=float(found.get("page_type_confidence", 0.9)),
            topic=found.get("topic", "other"),
            topic_confidence=float(found.get("topic_confidence", 0.8)),
            input_tokens=800,
            output_tokens=60,
        )


class ReplayDetector:
    def __init__(self, rules: list[tuple[str, str, float]]) -> None:
        self.rules = rules

    def detect(self, text: str) -> tuple[str, float]:
        for marker, code, confidence in self.rules:
            if marker in text:
                return code, confidence
        return "en", 0.95


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class VirtualReserve:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.last: dict[str, float] = {}
        self.slots: list[tuple[str, float]] = []

    def __call__(self, host: str, interval_s: float) -> tuple[float, datetime]:
        now = self.clock.t
        previous = self.last.get(host)
        slot = now if previous is None else max(now, previous + interval_s)
        self.last[host] = slot
        self.slots.append((host, slot))
        return slot - now, EPOCH + timedelta(seconds=slot)


# --- scripted agents ------------------------------------------------------------------


def _reply(calls: list[tuple[str, dict[str, Any]]], turn: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": f"call_{turn}_{n}", "type": "tool_call"}
            for n, (name, args) in enumerate(calls)
        ],
        usage_metadata={
            "input_tokens": 2_000,
            "output_tokens": 200,
            "total_tokens": 2_200,
        },
    )


@dataclass
class DiligentAgent:
    """Follows the workflow in the system prompt, as a careful model would.

    A test double: it reads the run's state to decide, where a real model reads the
    tool results in its messages.
    """

    state: RunState
    queries: list[str]
    official_domains: frozenset[str]
    stated: dict[str, dict[str, Any]]
    batch: int = 20
    turn: int = 0
    done_queries: int = 0
    calls_seen: list[list[str]] = field(default_factory=list)
    asked: dict[str, set[str]] = field(default_factory=dict)

    def _fresh(self, tool: str, items: list[str]) -> list[str]:
        seen = self.asked.setdefault(tool, set())
        fresh = [i for i in items if i not in seen][: self.batch]
        seen.update(fresh)
        return fresh

    async def ainvoke(self, input: list[BaseMessage]) -> AIMessage:
        self.turn += 1
        state = self.state
        found = self._fresh("probe", [u for u in state.found if u not in state.written])
        probed_ok = self._fresh(
            "sample",
            [
                u
                for u, p in state.probes.items()
                if p.reachable is CheckResult.PASSED and not p.unchanged
            ],
        )
        domains = self._fresh("terms", sorted({domain_of(u) for u in state.samples}))
        unwritten = self._fresh("write", list(state.probes))
        calls: list[tuple[str, dict[str, Any]]]
        if found:
            calls = [("probe_url", {"url": state.found[u].url}) for u in found]
            self._unask(probed_ok, domains, unwritten)
        elif probed_ok:
            calls = [("sample_content", {"url": u}) for u in probed_ok]
            self._unask([], domains, unwritten)
        elif domains:
            calls = [("read_terms", {"domain": d}) for d in domains]
            self._unask([], [], unwritten)
        elif unwritten:
            calls = []
            for u in unwritten:
                url = state.found[u].url if u in state.found else u
                origin = (
                    Origin.OFFICIAL
                    if domain_of(u) in self.official_domains
                    else Origin.THIRD_PARTY
                )
                args: dict[str, Any] = {"url": url, "origin": str(origin)}
                args.update(self.stated.get(url, {}))
                calls.append(("write_proposal", args))
        elif self.done_queries < len(self.queries):
            calls = [("web_search", {"query": self.queries[self.done_queries]})]
            self.done_queries += 1
        else:
            return AIMessage(
                content="No promising searches left.",
                usage_metadata={
                    "input_tokens": 2_000,
                    "output_tokens": 20,
                    "total_tokens": 2_020,
                },
            )
        self.calls_seen.append([name for name, _ in calls])
        return _reply(calls, self.turn)

    def _unask(self, samples: list[str], domains: list[str], writes: list[str]) -> None:
        """Items picked this turn but not acted on stay open for a later turn."""
        self.asked["sample"].difference_update(samples)
        self.asked["terms"].difference_update(domains)
        self.asked["write"].difference_update(writes)


@dataclass
class AdversarialAgent:
    """Tries to skip the gates: sample unprobed, claim a status, write unchecked."""

    target: str
    turn: int = 0

    async def ainvoke(self, input: list[BaseMessage]) -> AIMessage:
        self.turn += 1
        script: list[list[tuple[str, dict[str, Any]]]] = [
            [("sample_content", {"url": self.target})],
            [
                (
                    "write_proposal",
                    {
                        "url": self.target,
                        "origin": "official",
                        "validation_status": "passed",
                    },
                ),
                ("write_proposal", {"url": self.target, "origin": "official"}),
            ],
        ]
        if self.turn > len(script):
            return AIMessage(
                content="done",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            )
        return _reply(script[self.turn - 1], self.turn)


# --- the run --------------------------------------------------------------------------


@dataclass
class ReplayReport:
    state: RunState
    web: ReplayWeb
    trace: str
    proposals: list[dict[str, Any]]
    tickets: list[HandoffTicket]
    reserve: VirtualReserve

    def latest(self, url: str) -> dict[str, Any] | None:
        norm = normalise_url(url)
        rows = [p for p in self.proposals if p["normalised_url"] == norm]
        return max(rows, key=lambda p: p["revision"]) if rows else None


def read_proposals(
    conn: psycopg.Connection,
) -> tuple[list[dict[str, Any]], list[HandoffTicket]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT proposal_id, revision, normalised_url, url, domain, run_id, "
            "content_hash, labels, checks, validation_status, reason, reason_class, "
            "review_state FROM crawler_proposals ORDER BY normalised_url, revision"
        )
        names = [d.name for d in cur.description or []]
        proposals = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    tickets = [
        HandoffTicket.model_validate(row[0])
        for row in conn.execute("SELECT ticket FROM crawler_tickets").fetchall()
    ]
    return proposals, tickets


async def run_replay(
    fixtures: Fixtures,
    conn: psycopg.Connection,
    config: RunConfig,
    *,
    agent: str = "diligent",
    memory: CrawlerMemory | None = None,
    now: datetime = EPOCH,
    adversarial_target: str = "",
) -> ReplayReport:
    """Run one crawl over the fixtures, with ``conn`` as the store."""
    conn.execute(
        "INSERT INTO crawler_runs (run_id, config, approved_by, approved_at, status, "
        "started_at) VALUES (%s, '{}', 'replay', %s, 'running', %s) "
        "ON CONFLICT (run_id) DO NOTHING",
        (config.run_id, now, now),
    )
    web = ReplayWeb(fixtures)
    clock = VirtualClock()
    reserve = VirtualReserve(clock)
    sink = io.StringIO()
    tracing = Tracing(config.run_id, sink, env={})
    state = RunState(config=config, memory=memory or NullMemory(), now=lambda: now)
    state.queries_tried |= state.memory.queries_tried()
    state.dead_hosts |= set(state.memory.dead_hosts(now))
    async with httpx.AsyncClient(transport=httpx.MockTransport(web.handler)) as client:
        services = Services(
            safe=SafeClient(
                client,
                FixedResolver(fixtures.hosts),
                HostSlots(reserve, clock, config.host_interval_s, config.concurrency),
                monotonic=clock.now,
            ),
            lexicon=load_lexicon(),
            tracing=tracing,
            db=Database(conn),
            search_client=client,
            search_keys={Provider.TAVILY: "replay", Provider.BRAVE: "replay"},
            classifier=ReplayClassifier(fixtures.labels),
            detector=ReplayDetector(fixtures.languages),
        )
        if agent == "adversarial":
            model: Any = AdversarialAgent(adversarial_target)
        else:
            model = DiligentAgent(
                state,
                [str(s["query"]) for s in fixtures.searches],
                fixtures.official_domains,
                fixtures.stated,
            )
        await Crawler(state, services, model).run()
    tracing.shutdown()
    proposals, tickets = read_proposals(conn)
    return ReplayReport(state, web, sink.getvalue(), proposals, tickets, reserve)
