"""The review page's decisions: computed in Python, merged like the page (FR-025)."""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
import pytest
from crawler_fakes import add_proposal, add_run
from pydantic import ValidationError

from crawler.review import Group, decision_skeletons, load_groups, merge_choices, render
from pipeline.decisions import decision_id_for, parse_decisions

RUN_ID = "cr_20261123T101500_a3f9"


@pytest.fixture
def groups(crawler_db: psycopg.Connection) -> list[Group]:
    add_run(crawler_db)
    add_proposal(
        crawler_db, "https://board.test/p.pdf", digest="sha256:g", origin="official"
    )
    add_proposal(crawler_db, "https://mirror.test/p.pdf", digest="sha256:g")
    add_proposal(crawler_db, "https://other.test/q.pdf", digest="sha256:h")
    return load_groups(crawler_db, RUN_ID)


def test_skeletons_name_the_group_and_revisions(groups: list[Group]) -> None:
    skeleton = next(
        s for s in decision_skeletons(RUN_ID, groups) if s["content_hash"] == "sha256:g"
    )
    assert skeleton["decision_id"] == decision_id_for(RUN_ID, "sha256:g", [1, 1])
    assert [(m["primary"], m["origin"]) for m in skeleton["members"]] == [
        (True, "official"),
        (False, "third_party"),
    ]


def test_the_page_embeds_exactly_these_skeletons(groups: list[Group]) -> None:
    page = render(RUN_ID, groups)
    block = re.search(r'id="review-data">(.*?)</script>', page, re.S)
    assert block is not None
    embedded = json.loads(block.group(1))
    assert embedded["skeletons"] == decision_skeletons(RUN_ID, groups)
    assert embedded["format"] == "crawler-decisions/1"
    script = page.split("<script>", 1)[1]
    assert "crypto" not in script and "sha" not in script.lower()


def test_merged_choices_make_a_valid_decisions_file(groups: list[Group]) -> None:
    skeletons = decision_skeletons(RUN_ID, groups)
    approve = merge_choices(
        skeletons[0],
        {
            "decision": "approve",
            "exam": {"key": "enem", "new": False},
            "edition_id": "enem-2024-d1",
            "role": "paper",
            "origins": {skeletons[0]["members"][1]["proposal_id"]: "official"},
            "reason": "ignored on approve",
        },
    )
    reject = merge_choices(skeletons[1], {"decision": "reject", "reason": "duplicate"})
    file = {
        "format": "crawler-decisions/1",
        "run_id": RUN_ID,
        "decided_at": "2026-11-24T09:00:00Z",
        "decisions": [approve, reject],
    }
    parsed = parse_decisions(json.dumps(file).encode())
    first, second = parsed.decisions
    assert [m.origin for m in first.members] == ["official", "official"]
    assert first.reason is None and second.exam is None


@pytest.mark.parametrize(
    "choices",
    [
        {"decision": "reject"},
        {"decision": "approve", "exam": {"key": "enem", "new": False}, "role": "paper"},
        {
            "decision": "approve",
            "exam": {"key": "enem", "new": False},
            "edition_id": "e",
            "role": "lunch",
        },
    ],
)
def test_incomplete_choices_are_refused(
    groups: list[Group], choices: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        merge_choices(decision_skeletons(RUN_ID, groups)[0], choices)
