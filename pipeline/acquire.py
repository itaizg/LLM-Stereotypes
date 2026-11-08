"""The acquisition stages: catalog, review, discover, and fetch.

``manifests.py`` reads and validates the committed files; the stages here turn
them into database rows and ledger events. Every stage is callable on its own
(FR-018) and idempotent (FR-019).

Network rules (FR-007, FR-008, research R1 and R6): a URL is fetched only after
the maintainer approves it, and only through ``web.Fetcher``, which applies the
allowlist, robots.txt, the per-domain rate limit, and backoff. A refusal is
recorded in the ledger, not raised.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

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
from pipeline.web import Clock, Fetched, Fetcher, extract_links

__all__ = [
    "CatalogError",
    "DiscoverSummary",
    "FetchSummary",
    "LedgerError",
    "add_manual_url",
    "discover",
    "fetch",
    "review",
    "run_catalog",
]

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


# --- polite fetching -------------------------------------------------------------


def interval_for(catalog: Catalog) -> Callable[[str], float]:
    """Seconds between requests to a host: the strictest of the exams listing it."""

    def interval(host: str) -> float:
        owners = [e.exam_id for e in catalog.exams.values() if host in e.domains]
        return max(
            (rate_limit_for(catalog, owner) for owner in owners),
            default=catalog.default_rate_limit_seconds,
        )

    return interval


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


def _cached(fetches: Sequence[FetchRecord], blobs: BlobStore) -> FetchRecord | None:
    """The latest successful fetch whose bytes are still in the blob store.

    Only such a fetch may make a request conditional: a 304 says "you already
    have it", which is useless if the bytes are gone.
    """
    for record in reversed(fetches):
        if record.outcome == "fetched":
            if record.sha256 and blobs.exists(record.sha256):
                return record
            return None
    return None


async def _get_and_store(
    fetcher: Fetcher,
    url: str,
    allowed: frozenset[str],
    blobs: BlobStore,
    cached: FetchRecord | None,
) -> tuple[Fetched, FetchRecord | None]:
    """GET (conditionally, when ``cached``) and store new bytes as a blob.

    Returns the result and, when it is a success, the record to log. A 304 is
    a success that reuses the cached blob, so it logs nothing new unless the
    latest event for the source was a failure.
    """
    result = await fetcher.get(
        url,
        allowed,
        etag=cached.etag if cached else None,
        last_modified=cached.last_modified if cached else None,
    )
    if result.outcome == "not_modified":
        if cached is None:  # a 304 to an unconditional request is a server bug
            return Fetched("http_error", result.status), None
        record = FetchRecord(
            "fetched",
            cached.sha256,
            cached.media_type,
            cached.size,
            result.etag or cached.etag,
            result.last_modified or cached.last_modified,
        )
        return result, record
    if result.outcome != "fetched":
        return result, None
    record = FetchRecord(
        "fetched",
        blobs.put(result.body),
        result.media_type,
        len(result.body),
        result.etag,
        result.last_modified,
    )
    return result, record


def _record_fetch(
    ledger_path: Path,
    latest: dict[str, FetchRecord],
    source_id: str,
    result: Fetched,
    record: FetchRecord | None,
    at: str,
) -> bool:
    """Append a fetch event unless it repeats the source's latest outcome and hash.

    ``record`` is the success to log (None for a refusal or error). Validators
    are written only on success; an ETag that changes while the bytes do not is
    not a change (FR-019), so its stored value can go stale, which only costs a
    full 200 response on the next refresh.
    """
    if record is None:
        record = FetchRecord(result.outcome, None, result.media_type, None)
    previous = latest.get(source_id)
    if (
        previous
        and previous.outcome == record.outcome
        and previous.sha256 == record.sha256
    ):
        return False
    fields: dict[str, object] = {
        "source_id": source_id,
        "outcome": record.outcome,
        "sha256": record.sha256,
        "media_type": record.media_type,
        "bytes": record.size,
        "http_status": result.status,
    }
    if record.outcome == "fetched":
        fields["etag"] = record.etag
        fields["last_modified"] = record.last_modified
    append_event(ledger_path, "fetch", fields, at=at)
    latest[source_id] = record
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
            history = state.sources[seed_id].fetches if seed_id in state.sources else []
            result, record = await _get_and_store(
                fetcher, seed.url, allowed, blobs, _cached(history, blobs)
            )
            _record_fetch(ledger_path, latest, seed_id, result, record, at)
            if result.outcome in _REFUSALS:
                refused += 1
            if record is None or record.sha256 is None:
                continue
            seeds_fetched += 1
            # An unchanged seed (304) is re-read from its blob, so a better link
            # extractor still finds links on a page the server has not changed.
            body = (
                result.body if result.outcome == "fetched" else blobs.get(record.sha256)
            )
            links = extract_links(seed.url, body, record.media_type, result.charset)
            for link in links:
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
        result, record = await _get_and_store(
            fetcher,
            source.url,
            frozenset(catalog.exams[exam_id].domains),
            blobs,
            _cached(source.fetches, blobs),
        )
        changed = _record_fetch(ledger_path, latest, sid, result, record, at)
        if record is not None:
            fetched += changed
            unchanged += not changed
        elif result.outcome in _REFUSALS:
            refused += 1
        else:
            errors += 1
    return FetchSummary(fetched, unchanged, refused, errors, skipped)
