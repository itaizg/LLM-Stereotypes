"""The acquisition stages: catalog, review, discover, and fetch.

``manifests.py`` reads and validates the committed files; the stages here turn
them into database rows and ledger events. Every stage is callable on its own
(FR-018) and idempotent (FR-019).

Network rules (FR-007, FR-008, research R1 and R6): a URL is fetched only after
the maintainer approves it, only from its exam's allowlisted domains, only if
robots.txt allows it, and never faster than the per-domain rate limit. A
refusal is recorded in the ledger, not raised.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import psycopg

from pipeline.blobs import BlobStore
from pipeline.manifests import (
    REVIEW_ROLES,
    Catalog,
    CatalogError,
    EvidenceItem,
    FetchRecord,
    LedgerError,
    append_event,
    artifact_rows,
    load_catalog,
    rate_limit_for,
    read_ledger,
    replay_ledger,
    source_id_for,
    validate_catalog,
)
from pipeline.store import Counts, StageSummary, apply_schema, reconcile

__all__ = [
    "USER_AGENT",
    "CatalogError",
    "DiscoverSummary",
    "FetchSummary",
    "LedgerError",
    "SystemClock",
    "add_manual_url",
    "discover",
    "fetch",
    "review",
    "run_catalog",
]

# No personal contact details: the repository URL identifies the project.
USER_AGENT = "LLM-Stereotypes-dataset/1.0 (+https://github.com/itaizg/LLM-Stereotypes)"
MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_REFUSALS = frozenset({"refused_domain", "refused_robots", "login_required"})


# --- the catalog stage -------------------------------------------------------


def _evidence_json(item: EvidenceItem) -> dict[str, str]:
    body = {"type": item.type, "note": item.note}
    if item.source_id:
        body["source_id"] = item.source_id
    if item.artifact_sha256:
        body["artifact_sha256"] = item.artifact_sha256
    return body


def run_catalog(
    conn: psycopg.Connection, catalog_path: Path, ledger_path: Path
) -> StageSummary:
    """Validate, then reconcile exams, editions, sources, and artifacts.

    Validation runs first and writes nothing on failure. The reconcile runs in
    one transaction, so the catalog tables are all old or all new.
    """
    apply_schema(conn)
    catalog = load_catalog(catalog_path)
    state = replay_ledger(read_ledger(ledger_path))
    errors = validate_catalog(catalog, state)
    if errors:
        shown = errors[:20]
        more = f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""
        raise CatalogError("; ".join(shown) + more)

    exams = [
        {
            "exam_id": e.exam_id,
            "name": e.name,
            "body": e.body,
            "language": e.language,
            "domains": list(e.domains),
            "publishes_keys": e.publishes_keys,
        }
        for e in catalog.exams.values()
    ]
    editions = [
        {
            "edition_id": e.edition_id,
            "exam_id": e.exam_id,
            "year": e.year,
            "sitting": e.sitting,
            "kind": e.kind,
            "status": e.status,
            "status_reason": e.status_reason,
            "release_date": e.release_date,
            "official_url": e.official_url,
            "lineage": e.lineage,
            "language": e.language,
            "english_item_map": dict(e.english_item_map)
            if e.english_item_map
            else None,
            "evidence": [_evidence_json(i) for i in e.evidence],
        }
        for e in catalog.editions
    ]
    sources = [
        {
            "source_id": s.source_id,
            "url": s.url,
            "domain": s.domain,
            "discovery_method": s.method,
            "found_on": s.found_on,
            "edition_id": s.edition_id,
            "decision": s.decision,
            "decision_reason": s.decision_reason,
            "fetch_outcome": s.fetch_outcome,
        }
        for s in state.sources.values()
    ]
    artifacts = artifact_rows(state)

    counts = Counts()
    with conn.transaction():
        for table, key, rows in (
            ("exams", ["exam_id"], exams),
            ("exam_editions", ["edition_id"], editions),
            ("sources", ["source_id"], sources),
            ("artifacts", ["sha256", "edition_id"], artifacts),
        ):
            counts += reconcile(conn, table, key=key, scope={}, rows=rows)
    return StageSummary("catalog", editions=len(catalog.editions), counts=counts)


# --- review -------------------------------------------------------------------


def review(
    ledger_path: Path,
    source_id: str,
    *,
    approve: bool,
    edition_id: str | None,
    role: str | None,
    published: str | None,
    reason: str | None,
    at: str,
) -> bool:
    """Record the maintainer's decision on a candidate; return False if unchanged.

    Approval is the only way a URL becomes fetchable or citable (FR-007). A
    review identical to the latest one for the same source and edition appends
    nothing, so repeating a command is harmless (FR-019). Bad arguments raise
    ``ValueError`` before anything is written.
    """
    state = replay_ledger(read_ledger(ledger_path))
    if source_id not in state.sources:
        raise ValueError(f"unknown candidate: {source_id}")
    if approve:
        if not edition_id:
            raise ValueError("approve needs --edition")
        if role not in REVIEW_ROLES:
            raise ValueError(f"approve needs --role, one of {', '.join(REVIEW_ROLES)}")
        if role == "key_erratum" and not published:
            raise ValueError("a key_erratum needs --published")
        if published:
            try:
                date.fromisoformat(published)
            except ValueError:
                raise ValueError(
                    "--published must be an ISO date (YYYY-MM-DD)"
                ) from None
    elif not reason:
        raise ValueError("reject needs --reason")

    fields: dict[str, object] = {
        "source_id": source_id,
        "decision": "approved" if approve else "rejected",
        "edition_id": edition_id if approve else None,
        "role": role if approve else None,
        "published": published if approve else None,
        "reason": None if approve else reason,
    }
    for event in reversed(read_ledger(ledger_path)):
        if event.kind != "review" or event.text("source_id") != source_id:
            continue
        # Only reviews that bear on this decision can make it a repeat.
        if (
            event.text("decision") == "rejected"
            or event.text("edition_id") == edition_id
        ):
            if all(event.fields.get(k) == v for k, v in fields.items()):
                return False
            break
    append_event(ledger_path, "review", fields, at=at)
    return True


# --- polite fetching -----------------------------------------------------------


class Clock(Protocol):
    """Time, injected so rate limiting is testable without waiting."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock, used by the CLI; tests inject a virtual one instead."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Fetched:
    """The result of one policy-checked GET."""

    outcome: str
    status: int | None = None
    body: bytes = b""
    media_type: str | None = None


def interval_for(catalog: Catalog) -> Callable[[str], float]:
    """Seconds between requests to a host: the strictest of the exams listing it."""

    def interval(host: str) -> float:
        owners = [e.exam_id for e in catalog.exams.values() if host in e.domains]
        return max(
            (rate_limit_for(catalog, owner) for owner in owners),
            default=catalog.default_rate_limit_seconds,
        )

    return interval


class Fetcher:
    """GET with the allowlist, robots.txt, redirect, and rate-limit rules applied.

    One instance per command run, so robots.txt is fetched once per domain.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        interval: Callable[[str], float],
    ) -> None:
        self._client = client
        self._clock = clock
        self._interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        # True/False mean allow-all/disallow-all (no or unusable robots.txt).
        self._robots: dict[str, RobotFileParser | bool] = {}

    async def _throttled_get(self, url: str) -> httpx.Response:
        host = (urlsplit(url).hostname or "").lower()
        async with self._locks.setdefault(host, asyncio.Lock()):
            last = self._last.get(host)
            if last is not None:
                wait = last + self._interval(host) - self._clock.now()
                if wait > 0:
                    await self._clock.sleep(wait)
            try:
                return await self._client.get(url, headers={"User-Agent": USER_AGENT})
            finally:
                self._last[host] = self._clock.now()

    async def _robots_allow(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        rules = self._robots.get(host)
        if rules is None:
            rules = await self._read_robots(f"{parts.scheme}://{parts.netloc}")
            self._robots[host] = rules
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(USER_AGENT, url)

    async def _read_robots(self, origin: str) -> RobotFileParser | bool:
        """Read robots.txt as ``urllib`` does, but through the rate limiter.

        401/403 or a server error forbids everything (be polite when unsure);
        any other 4xx means there is no robots.txt, so nothing is forbidden.
        """
        try:
            response = await self._throttled_get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return False
        code = response.status_code
        if code == 200:
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            parser.modified()  # can_fetch refuses until the file counts as read
            return parser
        return not (code in {401, 403} or code >= 500)

    async def get(self, url: str, allowed: frozenset[str]) -> Fetched:
        """Fetch ``url`` if the rules allow; otherwise return the refusal."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = (urlsplit(current).hostname or "").lower()
            if host not in allowed:
                # Off the allowlist from the start is a refused domain; arriving
                # there by redirect usually means a sign-in page.
                return Fetched("refused_domain" if current == url else "login_required")
            if not await self._robots_allow(current):
                return Fetched("refused_robots")
            try:
                response = await self._throttled_get(current)
            except httpx.HTTPError:
                return Fetched("http_error")
            code = response.status_code
            location = response.headers.get("location")
            if code in _REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            if code in {401, 403}:
                return Fetched("login_required", code)
            if 200 <= code < 300:
                kind = response.headers.get("content-type", "").split(";")[0]
                return Fetched(
                    "fetched",
                    code,
                    response.content,
                    kind.strip().lower() or "application/octet-stream",
                )
            return Fetched("http_error", code)
        return Fetched("http_error")


# --- link extraction -----------------------------------------------------------


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)


def extract_links(base_url: str, body: bytes, media_type: str | None) -> list[str]:
    """Absolute http(s) links in an HTML page or sitemap, fragments removed."""
    raw: list[str] = []
    if "xml" in (media_type or "") or base_url.endswith(".xml"):
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []
        raw = [
            (node.text or "").strip()
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] == "loc"
        ]
    else:
        parser = _LinkParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        raw = parser.hrefs
    links: list[str] = []
    for href in raw:
        absolute, _ = urldefrag(urljoin(base_url, href))
        if urlsplit(absolute).scheme in {"http", "https"} and absolute not in links:
            links.append(absolute)
    return links


# --- discover ------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoverSummary:
    seeds_fetched: int = 0
    candidates_added: int = 0
    rejected_off_allowlist: int = 0
    refused: int = 0


def _candidate_fields(
    url: str, method: str, found_on: str | None, allowed: frozenset[str]
) -> dict[str, object]:
    host = (urlsplit(url).hostname or "").lower()
    off = host not in allowed
    return {
        "source_id": source_id_for(url),
        "url": url,
        "domain": host,
        "method": method,
        "found_on": found_on,
        "auto_decision": (
            {"decision": "rejected", "reason": "non_allowlisted"} if off else None
        ),
    }


def _record_fetch(
    ledger_path: Path,
    latest: dict[str, FetchRecord],
    source_id: str,
    result: Fetched,
    sha256: str | None,
    at: str,
) -> bool:
    """Append a fetch event unless it repeats the source's latest outcome and hash."""
    previous = latest.get(source_id)
    if previous and previous.outcome == result.outcome and previous.sha256 == sha256:
        return False
    append_event(
        ledger_path,
        "fetch",
        {
            "source_id": source_id,
            "outcome": result.outcome,
            "sha256": sha256,
            "media_type": result.media_type,
            "bytes": len(result.body) if sha256 else None,
            "http_status": result.status,
        },
        at=at,
    )
    latest[source_id] = FetchRecord(result.outcome, sha256, result.media_type, None)
    return True


async def discover(
    catalog: Catalog,
    ledger_path: Path,
    blobs: BlobStore,
    client: httpx.AsyncClient,
    clock: Clock,
    *,
    at: str,
    exam: str | None = None,
) -> DiscoverSummary:
    """Crawl each exam's official index pages and record every link as a candidate.

    Seeds are authored in the committed catalog, so the maintainer has already
    chosen them. Everything they link to is only a *candidate*: pending until
    reviewed, rejected on sight if off the allowlist, and never fetched here.
    """
    state = replay_ledger(read_ledger(ledger_path))
    known = set(state.sources)
    latest = {sid: s.fetches[-1] for sid, s in state.sources.items() if s.fetches}
    fetcher = Fetcher(client, clock, interval_for(catalog))
    seeds_fetched = added = rejected = refused = 0
    for exam_id in sorted(catalog.exams):
        if exam is not None and exam_id != exam:
            continue
        allowed = frozenset(catalog.exams[exam_id].domains)
        for seed in catalog.exams[exam_id].seeds:
            seed_id = source_id_for(seed.url)
            if seed_id not in known:
                append_event(
                    ledger_path,
                    "candidate",
                    _candidate_fields(seed.url, "index_crawl", None, allowed),
                    at=at,
                )
                known.add(seed_id)
                added += 1
            result = await fetcher.get(seed.url, allowed)
            sha256 = blobs.put(result.body) if result.outcome == "fetched" else None
            _record_fetch(ledger_path, latest, seed_id, result, sha256, at)
            if result.outcome in _REFUSALS:
                refused += 1
            if result.outcome != "fetched":
                continue
            seeds_fetched += 1
            for link in extract_links(seed.url, result.body, result.media_type):
                if source_id_for(link) in known:
                    continue
                fields = _candidate_fields(link, "index_crawl", seed.url, allowed)
                append_event(ledger_path, "candidate", fields, at=at)
                known.add(source_id_for(link))
                added += 1
                rejected += fields["auto_decision"] is not None
    return DiscoverSummary(seeds_fetched, added, rejected, refused)


def add_manual_url(
    catalog: Catalog, ledger_path: Path, url: str, edition_id: str, *, at: str
) -> bool:
    """Record a URL the maintainer found by hand; return False if already known.

    The edition only selects which exam's allowlist applies. The candidate still
    needs approval before anything is fetched from it (FR-007).
    """
    edition = next((e for e in catalog.editions if e.edition_id == edition_id), None)
    if edition is None:
        raise ValueError(f"unknown edition: {edition_id}")
    if source_id_for(url) in replay_ledger(read_ledger(ledger_path)).sources:
        return False
    allowed = frozenset(catalog.exams[edition.exam_id].domains)
    append_event(
        ledger_path, "candidate", _candidate_fields(url, "manual", None, allowed), at=at
    )
    return True


# --- fetch ---------------------------------------------------------------------


@dataclass(frozen=True)
class FetchSummary:
    fetched: int = 0
    unchanged: int = 0
    refused: int = 0
    errors: int = 0
    skipped: int = 0


async def fetch(
    catalog: Catalog,
    ledger_path: Path,
    blobs: BlobStore,
    client: httpx.AsyncClient,
    clock: Clock,
    *,
    at: str,
    exam: str | None = None,
    refresh: bool = False,
    source_ids: Sequence[str] = (),
) -> FetchSummary:
    """Fetch approved sources, storing each file once under its content hash.

    By default a source that was already fetched is skipped; ``refresh`` requests
    it again, which is how changed content at the same URL is noticed. A changed
    file becomes a second blob and a second event: the first is never touched
    (FR-009). Naming an unapproved source records ``not_approved`` and makes no
    request.
    """
    state = replay_ledger(read_ledger(ledger_path))
    edition_exam = {e.edition_id: e.exam_id for e in catalog.editions}
    latest = {sid: s.fetches[-1] for sid, s in state.sources.items() if s.fetches}
    fetcher = Fetcher(client, clock, interval_for(catalog))
    unknown = [sid for sid in source_ids if sid not in state.sources]
    if unknown:
        raise ValueError(f"unknown candidate: {unknown[0]}")
    targets = sorted(source_ids) or sorted(
        sid for sid, s in state.sources.items() if s.decision == "approved"
    )
    fetched = unchanged = refused = errors = skipped = 0
    for sid in targets:
        source = state.sources[sid]
        if source.decision != "approved":
            _record_fetch(ledger_path, latest, sid, Fetched("not_approved"), None, at)
            refused += 1
            continue
        exam_id = edition_exam.get(source.edition_id or "")
        if exam_id is None or (exam is not None and exam_id != exam):
            continue
        if not refresh and sid in latest and latest[sid].outcome == "fetched":
            skipped += 1
            continue
        result = await fetcher.get(
            source.url, frozenset(catalog.exams[exam_id].domains)
        )
        sha256 = blobs.put(result.body) if result.outcome == "fetched" else None
        changed = _record_fetch(ledger_path, latest, sid, result, sha256, at)
        if result.outcome == "fetched":
            fetched += changed
            unchanged += not changed
        elif result.outcome in _REFUSALS:
            refused += 1
        else:
            errors += 1
    return FetchSummary(fetched, unchanged, refused, errors, skipped)
