"""``write_proposal``: store a proposal and its hand-off ticket (FR-019 - FR-022).

The LLM supplies only the URL, the proposed origin and the exam name and year *as
stated on the page*. Every check result comes from this run's records, and the status,
reason, reason class and material fingerprint are computed here (research R4), so a
model that skips or misreports a check cannot produce a ``passed`` proposal.

One transaction writes the proposal and its ticket together, keyed by (normalised URL,
revision) (FR-021). For an existing URL it first locks the latest row (``SELECT ... FOR
UPDATE``), which serialises it against other runs and against ``pipeline
import-decisions``:

- an observation older than the latest revision's writes nothing
  (``older_observation``);
- the same material fingerprint writes nothing and returns the existing ticket;
- a material change (FR-021a) writes revision n+1 and marks revision n
  ``stale_superseded``; if revision n was approved and the content or key status
  changed, the new row is flagged ``reopened_after_approval`` (FR-021b).

``crawler_url_status`` is upserted on every call, the no-op path included (FR-016a).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from crawler.config import reason_class
from crawler.contracts import (
    CheckResult,
    Checks,
    Completeness,
    DownloadableCheck,
    Flag,
    HandoffTicket,
    KeyCheck,
    Labels,
    McqCheck,
    Proposal,
    ReachableCheck,
    ReasonCode,
    TicketInput,
    TicketOutput,
    ToolError,
    ToolErrorCode,
    ValidationStatus,
    WriteProposalInput,
    domain_of,
    normalise_url,
    proposal_id_for,
)
from crawler.lexicon import normalise
from crawler.runstate import RunState, Services

DESCRIPTION = (
    "Store the proposal for a URL you have checked. Check results are taken from this "
    "run's records, not from you."
)


@dataclass(frozen=True)
class Assembled:
    proposal: Proposal
    ticket: HandoffTicket
    size_bytes: int | None
    etag: str | None


def _checks(state: RunState, norm: str) -> tuple[Checks, ReasonCode | None, str | None]:
    """Checks from run state, the failure reason, and the content hash."""
    probe = state.probes.get(norm)
    if probe is None:
        return (
            Checks(reachable=ReachableCheck(result=CheckResult.NOT_RUN)),
            ReasonCode.NOT_PROBED,
            None,
        )
    reachable = ReachableCheck(
        result=probe.reachable,
        status=probe.status,
        latency_s=probe.latency_s,
        size_bytes=probe.size_bytes,
    )
    if probe.reachable is not CheckResult.PASSED:
        return Checks(reachable=reachable), probe.reason or ReasonCode.NOT_PROBED, None
    sampled = state.samples.get(norm)
    if sampled is None:
        return Checks(reachable=reachable), None, None
    sample = sampled.result
    downloadable = DownloadableCheck(
        result=sample.downloadable, sample_bytes=sample.sample_bytes
    )
    if sample.downloadable is not CheckResult.PASSED:
        return (
            Checks(reachable=reachable, downloadable=downloadable),
            sample.reason,
            sample.content_hash,
        )
    checks = Checks(
        reachable=reachable,
        downloadable=downloadable,
        mcq=McqCheck(
            result=sample.mcq,
            item_count=sample.item_count,
            page_type_confidence=sample.page_type_confidence,
            instruction_hits=sample.instruction_hits,
        ),
        key_located=KeyCheck(
            result=CheckResult.PASSED,
            key_status=sample.key_status,
            evidence=sample.key_evidence,
        ),
    )
    return checks, sample.reason, sample.content_hash


def _stated(
    args: WriteProposalInput, folded_text: str | None
) -> tuple[str | None, int | None, list[str]]:
    """Keep a stated exam name or year only if the sampled page shows it."""
    notes: list[str] = []
    name, year = args.exam_name_stated, args.exam_year_stated
    if name is not None and (folded_text is None or normalise(name) not in folded_text):
        name = None
        notes.append("exam_name_not_on_page")
    if year is not None and (folded_text is None or str(year) not in folded_text):
        year = None
        notes.append("exam_year_not_on_page")
    return name, year, notes


def assemble(args: WriteProposalInput, state: RunState) -> Assembled:
    norm = normalise_url(args.url)
    checks, reason, digest = _checks(state, norm)
    sampled = state.samples.get(norm)
    sample = sampled.result if sampled else None
    probe = state.probes.get(norm)
    domain = domain_of(norm)
    terms = state.terms.get(domain)
    name, year, notes = _stated(args, sampled.folded_text if sampled else None)
    flags: set[Flag] = set()
    if sample is not None and sample.lexicon_missing:
        flags.add(Flag.LEXICON_MISSING)
    if terms is not None and terms.restrictive:
        flags.add(Flag.RESTRICTIVE_TERMS)
    if not state.memory.available:
        flags.add(Flag.MEMORY_UNAVAILABLE)
    labels = Labels(
        origin=args.origin,
        kind=sample.kind if sample else None,
        language=sample.language if sample else "unknown",
        language_confidence=sample.language_confidence if sample else 0.0,
        site_type=sample.site_type if sample else None,
        site_type_confidence=sample.site_type_confidence if sample else 0.0,
        page_type=sample.page_type if sample else None,
        page_type_confidence=sample.page_type_confidence if sample else 0.0,
        topic=sample.topic if sample else None,
        topic_confidence=sample.topic_confidence if sample else 0.0,
        keyword_counts=sample.keyword_counts if sample else {},
        exam_name_stated=name,
        exam_year_stated=year,
        key_status=checks.key_located.key_status,
        terms_url=terms.terms_url if terms else None,
        terms_excerpt=terms.excerpt if terms else "",
        terms_restrictive=bool(terms and terms.restrictive),
        flags=tuple(sorted(flags)),
    )
    passed = checks.gates_passed
    if not passed and reason is None:
        reason = ReasonCode.NOT_CLASSIFIED
    observed = (
        sampled.observed_at
        if sampled is not None and sampled.observed_at is not None
        else state.now()
    )
    proposal = Proposal(
        proposal_id=proposal_id_for(norm),
        revision=1,
        url=args.url,
        normalised_url=norm,
        domain=domain,
        run_id=state.config.run_id,
        content_hash=digest,
        labels=labels,
        checks=checks,
        validation_status=ValidationStatus.PASSED
        if passed
        else ValidationStatus.FAILED,
        reason=None if passed else reason,
        reason_class=None if passed or reason is None else reason_class(reason),
        observed_at=observed,
    )
    if "memory_unavailable" not in notes and not state.memory.available:
        notes.append("memory_unavailable")
    ticket = HandoffTicket(
        run_id=state.config.run_id,
        validation_status=proposal.validation_status,
        input=state.found.get(norm) or TicketInput(url=args.url),
        output=TicketOutput(
            proposal_id=proposal.proposal_id,
            revision=1,
            normalised_url=norm,
            content_hash=digest,
            reason=proposal.reason,
            reason_class=proposal.reason_class,
            key_status=labels.key_status,
            flags=labels.flags,
            checks=checks,
            cost_usd=sample.cost_usd if sample else 0.0,
        ),
        completeness=Completeness(
            checks_run=checks.run_count,
            share=round(checks.run_count / 4, 3),
            notes=tuple(notes),
        ),
    )
    return Assembled(
        proposal,
        ticket,
        probe.size_bytes if probe and probe.reachable is CheckResult.PASSED else None,
        probe.etag if probe and probe.reachable is CheckResult.PASSED else None,
    )


def _with_revision(item: Assembled, revision: int, reopened: bool) -> Assembled:
    flags = item.proposal.labels.flags
    if reopened:
        flags = tuple(sorted({*flags, Flag.REOPENED_AFTER_APPROVAL}))
    labels = item.proposal.labels.model_copy(update={"flags": flags})
    proposal = Proposal.model_validate(
        {**item.proposal.model_dump(), "revision": revision, "labels": labels}
    )
    output = item.ticket.output.model_copy(
        update={"revision": revision, "flags": flags}
    )
    ticket = item.ticket.model_copy(update={"output": output})
    return Assembled(proposal, ticket, item.size_bytes, item.etag)


def _upsert_status(
    conn: psycopg.Connection, item: Assembled, checked_at: datetime
) -> None:
    proposal = item.proposal
    conn.execute(
        "INSERT INTO crawler_url_status AS s (normalised_url, last_reason, "
        "last_reason_class, transient_attempts, last_checked_at, last_size_bytes, "
        "last_etag) VALUES (%(url)s, %(reason)s, %(class)s, "
        "CASE WHEN %(class)s = 'transient' THEN 1 ELSE 0 END, %(at)s, %(size)s, "
        "%(etag)s) "
        "ON CONFLICT (normalised_url) DO UPDATE SET last_reason = "
        "EXCLUDED.last_reason, "
        "last_reason_class = EXCLUDED.last_reason_class, "
        "transient_attempts = CASE WHEN EXCLUDED.last_reason_class = 'transient' "
        "THEN s.transient_attempts + 1 ELSE 0 END, "
        "last_checked_at = EXCLUDED.last_checked_at, "
        "last_size_bytes = EXCLUDED.last_size_bytes, last_etag = EXCLUDED.last_etag",
        {
            "url": proposal.normalised_url,
            "reason": proposal.reason,
            "class": proposal.reason_class,
            "at": checked_at,
            "size": item.size_bytes,
            "etag": item.etag,
        },
    )


def _ticket(conn: psycopg.Connection, proposal_id: str, revision: int) -> HandoffTicket:
    row = conn.execute(
        "SELECT ticket FROM crawler_tickets WHERE proposal_id = %s AND revision = %s",
        (proposal_id, revision),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"{proposal_id} revision {revision} has no ticket")
    return HandoffTicket.model_validate(row[0])


def store(
    conn: psycopg.Connection, item: Assembled, checked_at: datetime
) -> tuple[HandoffTicket, str]:
    """Write (or not) in one transaction; return the ticket and what happened."""
    norm = item.proposal.normalised_url
    with conn.transaction():
        # First writers of a URL have no row to lock; this serialises them too.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (norm,))
        latest = conn.execute(
            "SELECT revision, material_fingerprint, observed_at, review_state, "
            "content_hash, labels->>'key_status' FROM crawler_proposals "
            "WHERE normalised_url = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE",
            (norm,),
        ).fetchone()
        outcome = "written"
        if latest is None:
            final = _with_revision(item, 1, reopened=False)
        else:
            revision, fingerprint, observed_at, review_state, digest, key = latest
            if item.proposal.observed_at < observed_at:
                _upsert_status(conn, item, checked_at)
                return _ticket(
                    conn, item.proposal.proposal_id, revision
                ), "older_observation"
            if fingerprint == item.proposal.material_fingerprint():
                _upsert_status(conn, item, checked_at)
                return _ticket(conn, item.proposal.proposal_id, revision), "unchanged"
            reopened = review_state == "approved" and (
                digest != item.proposal.content_hash
                or key != (item.proposal.labels.key_status or None)
            )
            final = _with_revision(item, revision + 1, reopened=reopened)
            conn.execute(
                "UPDATE crawler_proposals SET review_state = 'stale_superseded' "
                "WHERE proposal_id = %s AND revision = %s",
                (item.proposal.proposal_id, revision),
            )
            outcome = "reopened" if reopened else "revised"
        proposal = final.proposal
        ticket = final.ticket.for_storage()
        conn.execute(
            "INSERT INTO crawler_proposals (proposal_id, revision, normalised_url, "
            "url, "
            "domain, run_id, content_hash, labels, checks, validation_status, reason, "
            "reason_class, material_fingerprint, observed_at) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                proposal.proposal_id,
                proposal.revision,
                proposal.normalised_url,
                proposal.url,
                proposal.domain,
                proposal.run_id,
                proposal.content_hash,
                Jsonb(proposal.labels.model_dump(mode="json")),
                Jsonb(proposal.checks.model_dump(mode="json")),
                str(proposal.validation_status),
                proposal.reason,
                proposal.reason_class,
                proposal.material_fingerprint(),
                proposal.observed_at,
            ),
        )
        conn.execute(
            "INSERT INTO crawler_tickets (proposal_id, revision, ticket, run_id) "
            "VALUES (%s, %s, %s, %s)",
            (
                proposal.proposal_id,
                proposal.revision,
                Jsonb(json.loads(ticket.model_dump_json())),
                ticket.run_id,
            ),
        )
        _upsert_status(conn, final, checked_at)
    return ticket, outcome


def latest_ticket(
    conn: psycopg.Connection, normalised_url: str, checked_at: datetime
) -> HandoffTicket | None:
    """The current ticket of an unchanged URL; its re-check time is still recorded."""
    conn.execute(
        "UPDATE crawler_url_status SET last_checked_at = %s WHERE normalised_url = %s",
        (checked_at, normalised_url),
    )
    row = conn.execute(
        "SELECT t.ticket FROM crawler_tickets t JOIN crawler_proposals p "
        "USING (proposal_id, revision) WHERE p.normalised_url = %s "
        "ORDER BY p.revision DESC LIMIT 1",
        (normalised_url,),
    ).fetchone()
    return None if row is None else HandoffTicket.model_validate(row[0])


async def write_proposal(
    args: WriteProposalInput, state: RunState, services: Services
) -> HandoffTicket | ToolError:
    norm = normalise_url(args.url)
    probe = state.probes.get(norm)
    if (
        probe is not None
        and probe.reachable is CheckResult.PASSED
        and norm not in state.samples
    ):
        if probe.unchanged:
            now = state.now()
            existing = await services.db.call(
                lambda conn: latest_ticket(conn, norm, now)
            )
            if existing is not None:
                state.written[norm] = (
                    existing.output.proposal_id,
                    existing.output.revision,
                    str(existing.validation_status),
                )
                return existing
        return ToolError(
            error=ToolErrorCode.BAD_INPUT,
            retryable=False,
            detail="sample_content first",
        )
    item = assemble(args, state)
    checked_at = state.now()
    ticket, outcome = await services.db.call(lambda conn: store(conn, item, checked_at))
    state.written[norm] = (
        ticket.output.proposal_id,
        ticket.output.revision,
        str(ticket.validation_status),
    )
    state.outcomes[norm] = outcome
    if outcome == "older_observation":
        notes = (*ticket.completeness.notes, "older_observation")
        completeness = ticket.completeness.model_copy(update={"notes": notes})
        return ticket.model_copy(update={"completeness": completeness})
    return ticket
