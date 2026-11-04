"""The acquisition stages: catalog, review, discover, and fetch.

``manifests.py`` reads and validates the committed files; the stages here turn
them into database rows and ledger events. Every stage is callable on its own
(FR-018) and idempotent (FR-019).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import psycopg

from pipeline.manifests import (
    REVIEW_ROLES,
    CatalogError,
    EvidenceItem,
    LedgerError,
    append_event,
    artifact_rows,
    load_catalog,
    read_ledger,
    replay_ledger,
    validate_catalog,
)
from pipeline.store import Counts, StageSummary, apply_schema, reconcile

__all__ = ["CatalogError", "LedgerError", "review", "run_catalog"]


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
