"""The ledger fold for crawler decisions (spec 003 FR-021b, FR-026, FR-028)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.manifests import (
    LedgerError,
    append_event,
    append_events,
    read_ledger,
    replay_ledger,
    source_origins,
)

SID = "src_0000000000000001"
URL = "https://mirror.example/p.pdf"


def candidate(
    decision_id: str | None = "dec_a", revision: int = 1
) -> dict[str, object]:
    return {
        "source_id": SID,
        "url": URL,
        "domain": "mirror.example",
        "method": "crawler",
        "found_on": None,
        "proposal_id": "prop_0000000000000001",
        "revision": revision,
        "origin": "third_party",
        "labels_digest": "x",
        "evidence": [URL],
        "decision_id": decision_id,
    }


def review(
    decision: str = "approved", decision_id: str | None = "dec_a"
) -> dict[str, object]:
    return {
        "source_id": SID,
        "decision": decision,
        "edition_id": "sat-2024-main" if decision == "approved" else None,
        "role": "paper" if decision == "approved" else None,
        "published": None,
        "reason": None if decision == "approved" else "duplicate",
        "decision_id": decision_id,
    }


EXAM = {
    "exam_key": "vestibular_x",
    "name": "Vestibular X",
    "body": "Universidade X",
    "language": "pt",
    "domain": "vest.example",
    "decision_id": "dec_a",
    "proposal_id": "prop_0000000000000001",
    "revision": 1,
    "evidence": [URL],
}
ALLOW = {
    "exam_key": "sat",
    "domain": "mirror.example",
    "origin": "third_party",
    "decision_id": "dec_a",
}


def test_a_closed_decision_adds_its_exam_domain_and_source(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_events(
        ledger,
        [
            ("exam_added", EXAM),
            ("domain_allowlisted", ALLOW),
            ("candidate", candidate()),
            ("review", review()),
        ],
        at="t",
    )
    state = replay_ledger(read_ledger(ledger))
    assert set(state.exams_added) == {"vestibular_x"}
    assert state.allowlisted == {"sat": frozenset({"mirror.example"})}
    source = state.sources[SID]
    assert (source.decision, source.origin, source.revision) == (
        "approved",
        "third_party",
        1,
    )
    assert source_origins(state) == {SID: "third_party"}


def test_a_decision_without_its_review_exposes_nothing(tmp_path: Path) -> None:
    """A crash mid-decision: the events are there, but the decision never closed."""
    ledger = tmp_path / "ledger.jsonl"
    append_events(
        ledger,
        [
            ("exam_added", EXAM),
            ("domain_allowlisted", ALLOW),
            ("candidate", candidate()),
        ],
        at="t",
    )
    state = replay_ledger(read_ledger(ledger))
    assert (state.exams_added, state.allowlisted, dict(state.sources)) == ({}, {}, {})


def test_a_torn_final_line_is_ignored(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_event(ledger, "candidate", candidate(None), at="t")
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"seq":2,"kind":"review","at":"t","source_')
    assert len(read_ledger(ledger)) == 1
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write("\n")  # a complete but broken line is still an error
    with pytest.raises(LedgerError):
        read_ledger(ledger)


def test_the_latest_review_decides(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_events(ledger, [("candidate", candidate()), ("review", review())], at="t")
    append_event(ledger, "review", review("rejected"), at="t")
    source = replay_ledger(read_ledger(ledger)).sources[SID]
    assert (source.decision, source.approvals) == ("rejected", {})


def test_a_reopened_source_returns_to_pending(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_events(ledger, [("candidate", candidate()), ("review", review())], at="t")
    append_event(
        ledger,
        "review_reopened",
        {
            "source_id": SID,
            "proposal_id": "prop_0000000000000001",
            "from_revision": 1,
            "to_revision": 2,
            "changed": ["key"],
        },
        at="t",
    )
    state = replay_ledger(read_ledger(ledger))
    source = state.sources[SID]
    assert (source.decision, source.approvals, source.reopened_to) == (
        "pending",
        {},
        [2],
    )
    assert len(state.reopened) == 1
    # A new decision on the new revision updates the source's revision.
    append_events(
        ledger,
        [("candidate", candidate("dec_b", 2)), ("review", review(decision_id="dec_b"))],
        at="t",
    )
    source = replay_ledger(read_ledger(ledger)).sources[SID]
    assert (source.decision, source.revision) == ("approved", 2)


def test_reopening_an_unknown_source_is_an_error(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_event(
        ledger, "review_reopened", {"source_id": SID, "to_revision": 2}, at="t"
    )
    with pytest.raises(LedgerError, match="unknown source"):
        replay_ledger(read_ledger(ledger))


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("exam_added", {"decision_id": None}),
        ("domain_allowlisted", {"exam_key": "sat"}),
    ],
)
def test_malformed_events_are_errors(
    tmp_path: Path, kind: str, fields: dict[str, object]
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_event(ledger, kind, fields, at="t")
    with pytest.raises(LedgerError):
        replay_ledger(read_ledger(ledger))


def test_manual_candidates_default_to_official(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_event(
        ledger,
        "candidate",
        {
            "source_id": SID,
            "url": URL,
            "domain": "d",
            "method": "manual",
            "found_on": None,
        },
        at="t",
    )
    assert source_origins(replay_ledger(read_ledger(ledger))) == {SID: "official"}
