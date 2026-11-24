"""``write_proposal``: revisions, idempotency, the re-check record (FR-021, 021a/b)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import psycopg
from crawler_fakes import NOW, FakeResolver, VirtualClock, make_services, make_state

from crawler.contracts import (
    CheckResult,
    HandoffTicket,
    KeyStatus,
    Kind,
    Origin,
    ProbeResult,
    ReasonCode,
    SampleResult,
    TermsResult,
    ToolError,
    ToolErrorCode,
    WriteProposalInput,
    normalise_url,
)
from crawler.lexicon import normalise
from crawler.proposals import write_proposal
from crawler.runstate import RunState, Sampled

URL = "https://exams.example/p.pdf"
NORM = normalise_url(URL)


def probed(state: RunState, ok: bool = True, reason: ReasonCode | None = None) -> None:
    state.probes[NORM] = ProbeResult(
        url=URL,
        final_url=URL,
        status=200 if ok else None,
        hops=0,
        robots_allowed=reason is not ReasonCode.BLOCKED,
        latency_s=0.2,
        reachable=CheckResult.PASSED if ok else CheckResult.FAILED,
        reason=reason,
        size_bytes=100,
        etag='"e1"',
    )


def sampled(
    state: RunState,
    *,
    digest: str = "sha256:a",
    key: KeyStatus = KeyStatus.INLINE,
    confidence: float = 0.9,
    observed_offset_s: int = 0,
    mcq: CheckResult = CheckResult.PASSED,
) -> None:
    reason = None if mcq is CheckResult.PASSED else ReasonCode.NOT_MCQ_STRUCTURE
    result = SampleResult(
        url=URL,
        kind=Kind.PDF,
        sample_bytes=100,
        pages_read=1,
        downloadable=CheckResult.PASSED,
        item_count=6,
        choice_counts=(4,) * 6,
        choices_consistent=True,
        language="en",
        language_confidence=0.95,
        page_type="past_paper",
        page_type_confidence=confidence,
        key_status=key,
        content_hash=digest,
        mcq=mcq,
        reason=reason,
        title="Synthetic Exam 2024",
    )
    state.samples[NORM] = Sampled(
        result,
        NOW + timedelta(seconds=observed_offset_s),
        normalise("Synthetic Exam 2024 practice paper"),
    )


def write(
    conn: psycopg.Connection, state: RunState, **args: object
) -> HandoffTicket | ToolError:
    async def go() -> HandoffTicket | ToolError:
        import httpx

        async with httpx.AsyncClient() as client:
            services = make_services(client, FakeResolver(), VirtualClock(), conn)
            payload = {"url": URL, "origin": Origin.OFFICIAL, **args}
            return await write_proposal(
                WriteProposalInput.model_validate(payload), state, services
            )

    return asyncio.run(go())


def rows(conn: psycopg.Connection) -> list[tuple[int, str, str]]:
    return conn.execute(
        "SELECT revision, validation_status, review_state FROM crawler_proposals "
        "ORDER BY revision"
    ).fetchall()


def ready(conn: psycopg.Connection) -> RunState:
    conn.execute(
        "INSERT INTO crawler_runs (run_id, config, approved_by, approved_at, status, "
        "started_at) VALUES ('cr_20261123T101500_a3f9', '{}', 't', now(), 'running', "
        "now())"
    )
    state = make_state()
    probed(state)
    sampled(state)
    return state


def test_revision_one_and_its_ticket_are_written_together(
    crawler_db: psycopg.Connection,
) -> None:
    state = ready(crawler_db)
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket)
    assert (ticket.validation_status, ticket.output.revision) == ("passed", 1)
    assert ticket.completeness.checks_run == 4 and ticket.completeness.share == 1.0
    assert rows(crawler_db) == [(1, "passed", "unreviewed")]
    count = crawler_db.execute("SELECT count(*) FROM crawler_tickets").fetchone()
    assert count == (1,)


def test_a_retry_returns_the_existing_ticket(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    first = write(crawler_db, state)
    again = write(crawler_db, state)
    assert first == again and len(rows(crawler_db)) == 1


def test_a_confidence_shift_is_not_material(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    write(crawler_db, state)
    sampled(state, confidence=0.75, observed_offset_s=60)
    write(crawler_db, state)
    assert len(rows(crawler_db)) == 1


def test_a_material_change_writes_the_next_revision(
    crawler_db: psycopg.Connection,
) -> None:
    state = ready(crawler_db)
    write(crawler_db, state)
    sampled(state, digest="sha256:b", observed_offset_s=60)
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket) and ticket.output.revision == 2
    assert rows(crawler_db) == [
        (1, "passed", "stale_superseded"),
        (2, "passed", "unreviewed"),
    ]


def test_a_changed_reason_is_material(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    probed(state, ok=False, reason=ReasonCode.TIMEOUT)
    write(crawler_db, state)
    probed(state, ok=False, reason=ReasonCode.BLOCKED)
    write(crawler_db, state)
    reasons = crawler_db.execute(
        "SELECT reason FROM crawler_proposals ORDER BY revision"
    ).fetchall()
    assert reasons == [("timeout",), ("blocked",)]


def test_an_older_observation_writes_nothing(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    sampled(state, observed_offset_s=600)
    write(crawler_db, state)
    sampled(state, digest="sha256:old", observed_offset_s=0)
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket)
    assert "older_observation" in ticket.completeness.notes
    assert len(rows(crawler_db)) == 1


def test_a_change_to_an_approved_source_reopens_it(
    crawler_db: psycopg.Connection,
) -> None:
    state = ready(crawler_db)
    write(crawler_db, state)
    crawler_db.execute("UPDATE crawler_proposals SET review_state = 'approved'")
    sampled(state, key=KeyStatus.LINKED_FILE, observed_offset_s=60)
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket)
    assert "reopened_after_approval" in ticket.output.flags


def test_the_recheck_record_is_kept_on_every_call(
    crawler_db: psycopg.Connection,
) -> None:
    state = ready(crawler_db)
    probed(state, ok=False, reason=ReasonCode.TIMEOUT)
    write(crawler_db, state)
    write(crawler_db, state)
    row = crawler_db.execute(
        "SELECT last_reason, last_reason_class, transient_attempts FROM "
        "crawler_url_status"
    ).fetchone()
    assert row == ("timeout", "transient", 2)
    probed(state)
    write(crawler_db, state)
    row = crawler_db.execute(
        "SELECT last_reason, transient_attempts, last_size_bytes, last_etag "
        "FROM crawler_url_status"
    ).fetchone()
    assert row == (None, 0, 100, '"e1"')


def test_unprobed_urls_fail_and_unsampled_ones_are_refused(
    crawler_db: psycopg.Connection,
) -> None:
    state = ready(crawler_db)
    state.probes.clear()
    state.samples.clear()
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket)
    assert (ticket.validation_status, ticket.output.reason) == ("failed", "not_probed")
    assert ticket.completeness.checks_run == 0
    probed(state)
    refused = write(crawler_db, state)
    assert isinstance(refused, ToolError) and refused.error is ToolErrorCode.BAD_INPUT


def test_a_guessed_exam_name_is_dropped(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    ticket = write(crawler_db, state, exam_name_stated="ENEM", exam_year_stated=2024)
    assert isinstance(ticket, HandoffTicket)
    assert "exam_name_not_on_page" in ticket.completeness.notes
    labels = crawler_db.execute("SELECT labels FROM crawler_proposals").fetchone()
    assert labels is not None
    assert (labels[0]["exam_name_stated"], labels[0]["exam_year_stated"]) == (
        None,
        2024,
    )


def test_flags_come_from_the_run(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    state.terms["exams.example"] = TermsResult(
        domain="exams.example",
        terms_url="https://exams.example/terms",
        excerpt="no scraping",
        restrictive=True,
    )
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket)
    assert set(ticket.output.flags) == {"restrictive_terms", "memory_unavailable"}


def test_a_failed_mcq_check_records_its_reason(crawler_db: psycopg.Connection) -> None:
    state = ready(crawler_db)
    sampled(state, mcq=CheckResult.FAILED)
    ticket = write(crawler_db, state)
    assert isinstance(ticket, HandoffTicket)
    assert (ticket.output.reason, ticket.output.reason_class) == (
        "not_mcq_structure",
        "permanent",
    )
