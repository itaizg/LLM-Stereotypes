"""The offline replay eval (FR-035): the whole agent over synthetic recordings.

It asserts Story 1's acceptance scenarios and the safety criteria:
- every fixture gets its expected status, reason and key status;
- no request reaches a disallowed path or a non-public address (SC-003);
- every ticket validates against the contract (SC-004);
- a second run over the same inputs writes nothing new (SC-005);
- each limit stops the run cleanly with its name and completeness (SC-006);
- no synthetic question text appears in tickets, rows or spans (SC-009);
- an adversarial model cannot get a ``passed`` proposal (research R4).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from crawler.config import RunConfig
from crawler.contracts import HandoffTicket, StopLimit
from crawler.evals.replay import Fixtures, ReplayReport, load_fixtures, run_replay

FIXTURES = Path(__file__).parent / "fixtures" / "replay"
RUN_ID = "cr_20261123T101500_a3f9"
SECOND_RUN = "cr_20261130T101500_b4e0"
OFFICIAL = "https://exam.example.org/papers/practice-2024.pdf"
MIRROR = "https://prep.example.net/mirror/practice-2024.pdf"


@pytest.fixture(scope="module")
def fixtures() -> Fixtures:
    return load_fixtures(FIXTURES)


def replay(
    fixtures: Fixtures, conn: psycopg.Connection, run_id: str = RUN_ID, **config: Any
) -> ReplayReport:
    run_config = RunConfig.model_validate({"run_id": run_id, **config})
    return asyncio.run(run_replay(fixtures, conn, run_config))


@pytest.fixture
def report(fixtures: Fixtures, crawler_db: psycopg.Connection) -> ReplayReport:
    return replay(fixtures, crawler_db)


def test_every_fixture_gets_its_expected_outcome(
    fixtures: Fixtures, report: ReplayReport
) -> None:
    for expected in fixtures.expected:
        row = report.latest(expected.url)
        assert row is not None, expected.url
        assert row["validation_status"] == expected.status, expected.url
        assert row["reason"] == expected.reason, expected.url
        if expected.key_status is not None:
            assert row["labels"]["key_status"] == expected.key_status, expected.url
        for flag in expected.flags:
            assert flag in row["labels"]["flags"], (expected.url, flag)
    assert report.state.stop_limit is None and report.state.escalated_tool is None


def test_the_official_paper_carries_every_label(report: ReplayReport) -> None:
    """Story 1, scenario 1."""
    row = report.latest(OFFICIAL)
    assert row is not None
    labels = row["labels"]
    assert (labels["kind"], labels["language"], labels["page_type"]) == (
        "pdf",
        "en",
        "past_paper",
    )
    assert labels["origin"] == "official" and row["run_id"] == RUN_ID
    assert labels["terms_restrictive"] and labels["terms_url"].endswith("/terms")
    assert labels["keyword_counts"]["instruction"] >= 1
    # "SAT" is not on the page, so the guard dropped it; the year is on it.
    assert labels["exam_name_stated"] is None and labels["exam_year_stated"] == 2024
    mirror = report.latest(MIRROR)
    assert mirror is not None and mirror["content_hash"] == row["content_hash"]
    assert mirror["labels"]["origin"] == "third_party"


def test_unreachable_candidates_record_checks_two_to_four_as_not_run(
    report: ReplayReport,
) -> None:
    """Story 1, scenario 3 (FR-012)."""
    row = report.latest("https://blocked.example.org/private/paper.pdf")
    assert row is not None
    checks = row["checks"]
    assert checks["reachable"]["result"] == "failed"
    for name in ("downloadable", "mcq", "key_located"):
        assert checks[name]["result"] == "not_run"
    assert row["content_hash"] is None


def test_no_forbidden_request_is_sent(fixtures: Fixtures, report: ReplayReport) -> None:
    """SC-003 and Story 1, scenarios 3 and 4."""
    assert set(report.web.log) & fixtures.forbidden == set()
    assert "https://blocked.example.org/robots.txt" in report.web.log


def test_every_ticket_validates_and_matches_its_proposal(report: ReplayReport) -> None:
    """SC-004: one ticket per proposal, each valid against the output contract."""
    assert len(report.tickets) == len(report.proposals) == 14
    for ticket in report.tickets:
        HandoffTicket.model_validate(ticket.model_dump(mode="json")).for_storage()
        assert ticket.run_id == RUN_ID
        row = report.latest(ticket.input.url)
        assert row is not None
        assert row["validation_status"] == str(ticket.validation_status)


def test_the_provider_switch_is_recorded_and_counted(report: ReplayReport) -> None:
    """FR-007a: Tavily's quota error moved the run to Brave; both queries counted."""
    switches = [s.as_json() for s in report.state.provider_switches]
    assert switches == [
        {
            "at": switches[0]["at"],
            "from": "tavily",
            "to": "brave",
            "reason": "quota_429",
        }
    ]
    assert report.state.queries_used == 3
    assert [p for p, _ in report.web.search_calls] == ["tavily", "tavily", "brave"]


def test_requests_to_one_host_are_spaced(report: ReplayReport) -> None:
    """FR-010: every reserved slot for a host is at least 5 s after the previous one."""
    last: dict[str, float] = {}
    for host, slot in report.reserve.slots:
        if host in last:
            assert slot - last[host] >= 5.0
        last[host] = slot


def test_a_second_run_writes_nothing_new(
    fixtures: Fixtures, crawler_db: psycopg.Connection, report: ReplayReport
) -> None:
    """SC-005: the same inputs again create no proposal or ticket."""
    again = replay(fixtures, crawler_db, run_id=SECOND_RUN, refresh=True)
    assert len(again.proposals) == len(report.proposals)
    assert len(again.tickets) == len(report.tickets)


@pytest.mark.parametrize(
    ("config", "limit"),
    [
        ({"budget": {"max_queries": 1}}, StopLimit.QUERIES),
        ({"budget": {"max_iterations": 3}}, StopLimit.ITERATIONS),
        ({"budget": {"max_cost_usd": 0.12}}, StopLimit.COST),
    ],
)
def test_each_limit_stops_the_run_cleanly(
    fixtures: Fixtures,
    crawler_db: psycopg.Connection,
    config: dict[str, Any],
    limit: StopLimit,
) -> None:
    """SC-006 and Story 1, scenario 5: stopped, with the limit named, never past it."""
    stopped = replay(fixtures, crawler_db, **config)
    state = stopped.state
    assert state.stop_limit is limit
    budget = state.config.budget
    assert state.queries_used <= budget.max_queries
    assert state.iterations_used <= budget.max_iterations
    assert state.cost_usd <= budget.max_cost_usd
    assert 0.0 <= state.completeness <= 1.0
    # Proposals written before the stop are kept.
    assert len(stopped.tickets) == len(stopped.proposals)


def test_no_question_text_leaves_the_run(
    fixtures: Fixtures, report: ReplayReport
) -> None:
    """SC-009 and FR-020: tickets, rows and spans hold no stems or choices."""
    stored = json.dumps(
        [t.model_dump(mode="json") for t in report.tickets] + report.proposals,
        default=str,
        ensure_ascii=False,
    )
    for text in fixtures.stems:
        assert text not in stored
        assert text not in report.trace


def test_spans_carry_the_run_id(report: ReplayReport) -> None:
    """FR-032: the trace's run_id is the tickets' run_id."""
    spans = [json.loads(line) for line in report.trace.splitlines()]
    assert {s["name"] for s in spans} >= {"crawler.run", "agent.step", "tool.probe_url"}
    assert all(s["attributes"]["run_id"] == RUN_ID for s in spans)


def test_an_adversarial_model_cannot_pass_an_unchecked_url(
    fixtures: Fixtures, crawler_db: psycopg.Connection
) -> None:
    """Research R4: sampling unprobed URLs and claiming a status both fail."""
    adversarial = asyncio.run(
        run_replay(
            fixtures,
            crawler_db,
            RunConfig(run_id=RUN_ID),
            agent="adversarial",
            adversarial_target=OFFICIAL,
        )
    )
    row = adversarial.latest(OFFICIAL)
    assert row is not None
    assert (row["validation_status"], row["reason"]) == ("failed", "not_probed")
    # The sample was refused, so the paper itself was never requested.
    assert OFFICIAL not in adversarial.web.log
