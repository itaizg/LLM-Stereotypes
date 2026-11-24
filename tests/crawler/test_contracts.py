"""The crawler's contracts (FR-016, FR-019, FR-020, FR-022, research R11)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from crawler import contracts as c

URL = "https://Example.org:443/exam/2024/paper.pdf#p2"
NORM = "https://example.org/exam/2024/paper.pdf"
WHEN = datetime(2026, 11, 23, tzinfo=UTC)

# A synthetic multiple-choice sample: nothing below is real exam text.
STEMS = ("Which zorblat is the most quindle?", "How many vexors fit a plome?")
CHOICES = ("frabjous tock", "glimmering wuzzle", "obstreperous pim")


def models() -> list[type[BaseModel]]:
    return [
        v
        for v in vars(c).values()
        if isinstance(v, type)
        and issubclass(v, BaseModel)
        and v.__module__ == c.__name__
    ]


UNIT_SUFFIXES = {"_s": "s", "_bytes": "bytes", "_usd": "USD"}


@pytest.mark.parametrize("model", models(), ids=lambda m: m.__name__)
def test_quantities_carry_their_unit(model: type[BaseModel]) -> None:
    properties = model.model_json_schema().get("properties", {})
    for name, schema in properties.items():
        options = [schema, *schema.get("anyOf", [])]
        unit = next((o["unit"] for o in options if "unit" in o), None)
        for suffix, expected in UNIT_SUFFIXES.items():
            if name.endswith(suffix):
                assert unit == expected, f"{model.__name__}.{name}"
        if unit is not None and not name.endswith("share"):
            assert any(name.endswith(s) for s in UNIT_SUFFIXES), name


@pytest.mark.parametrize("model", models(), ids=lambda m: m.__name__)
def test_unknown_fields_are_refused(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"


def test_urls_are_normalised_and_ids_derived() -> None:
    assert c.normalise_url(URL) == NORM
    assert c.normalise_url("HTTP://a.test:8080") == "http://a.test:8080/"
    assert c.normalise_url("https://a.test/x?id=2") == "https://a.test/x?id=2"
    pid = c.proposal_id_for(NORM)
    assert pid.startswith("prop_") and len(pid) == 21


def test_titles_and_excerpts_are_capped() -> None:
    with pytest.raises(ValidationError):
        c.TermsResult(domain="a.test", terms_url=None, excerpt="x" * 301)
    assert len(c.clip("word " * 100, c.TITLE_MAX)) == c.TITLE_MAX


def checks(passed: bool) -> c.Checks:
    p, f = c.CheckResult.PASSED, c.CheckResult.FAILED
    return c.Checks(
        reachable=c.ReachableCheck(result=p, status=200, latency_s=0.5, size_bytes=10),
        downloadable=c.DownloadableCheck(result=p, sample_bytes=10),
        mcq=c.McqCheck(result=p if passed else f, item_count=12),
        key_located=c.KeyCheck(result=p, key_status=c.KeyStatus.INLINE),
    )


def proposal(**changes: object) -> c.Proposal:
    fields: dict[str, object] = {
        "proposal_id": c.proposal_id_for(NORM),
        "revision": 1,
        "url": URL,
        "normalised_url": NORM,
        "domain": "example.org",
        "run_id": "cr_20261123T101500_a3f9",
        "content_hash": "sha256:abc",
        "labels": c.Labels(origin=c.Origin.OFFICIAL, key_status=c.KeyStatus.INLINE),
        "checks": checks(True),
        "validation_status": c.ValidationStatus.PASSED,
        "reason": None,
        "reason_class": None,
        "observed_at": WHEN,
    }
    fields.update(changes)
    return c.Proposal.model_validate(fields)


def test_passed_exactly_when_the_three_gates_pass() -> None:
    assert proposal().validation_status is c.ValidationStatus.PASSED
    with pytest.raises(ValidationError, match="passed exactly"):
        proposal(checks=checks(False))
    failed = proposal(
        checks=checks(False),
        validation_status=c.ValidationStatus.FAILED,
        reason=c.ReasonCode.NOT_MCQ_STRUCTURE,
        reason_class="permanent",
    )
    assert failed.reason is c.ReasonCode.NOT_MCQ_STRUCTURE
    with pytest.raises(ValidationError, match="reason"):
        proposal(
            checks=checks(False),
            validation_status=c.ValidationStatus.FAILED,
            reason=None,
            reason_class=None,
        )


def test_an_unreachable_candidate_has_nothing_after_check_one() -> None:
    unreachable = c.Checks(
        reachable=c.ReachableCheck(result=c.CheckResult.FAILED),
        downloadable=c.DownloadableCheck(result=c.CheckResult.PASSED),
    )
    with pytest.raises(ValidationError, match="checks 2-4"):
        proposal(
            checks=unreachable,
            content_hash=None,
            validation_status=c.ValidationStatus.FAILED,
            reason=c.ReasonCode.BLOCKED,
            reason_class="permanent",
        )
    with pytest.raises(ValidationError, match="content hash"):
        proposal(
            checks=c.Checks(reachable=c.ReachableCheck(result=c.CheckResult.FAILED)),
            validation_status=c.ValidationStatus.FAILED,
            reason=c.ReasonCode.BLOCKED,
            reason_class="permanent",
        )


def test_the_key_check_never_fails() -> None:
    with pytest.raises(ValidationError, match="never fails"):
        c.KeyCheck(result=c.CheckResult.FAILED)


def test_the_id_must_match_the_url() -> None:
    with pytest.raises(ValidationError, match="derived"):
        proposal(proposal_id="prop_0000000000000000")


def test_confidence_alone_is_not_material() -> None:
    base = proposal()
    shifted = proposal(
        labels=c.Labels(
            origin=c.Origin.OFFICIAL,
            key_status=c.KeyStatus.INLINE,
            page_type_confidence=0.93,
        )
    )
    relabelled = proposal(
        labels=c.Labels(origin=c.Origin.THIRD_PARTY, key_status=c.KeyStatus.INLINE)
    )
    assert base.material_fingerprint() == shifted.material_fingerprint()
    assert base.material_fingerprint() != relabelled.material_fingerprint()
    assert (
        base.material_fingerprint()
        != proposal(content_hash="sha256:x").material_fingerprint()
    )


def ticket(status: c.ValidationStatus = c.ValidationStatus.PASSED) -> c.HandoffTicket:
    p = proposal()
    return c.HandoffTicket(
        run_id=p.run_id,
        validation_status=status,
        input=c.TicketInput(
            url=URL, query="zorblat exam pdf", provider=c.Provider.TAVILY
        ),
        output=c.TicketOutput(
            proposal_id=p.proposal_id,
            revision=1,
            normalised_url=NORM,
            content_hash=p.content_hash,
            reason=None,
            reason_class=None,
            key_status=c.KeyStatus.INLINE,
            flags=(),
            checks=p.checks,
            cost_usd=0.004,
        ),
        completeness=c.Completeness(checks_run=4, share=1.0),
    )


def test_a_stored_ticket_is_never_pending() -> None:
    assert ticket().for_storage().owner == "crawler"
    with pytest.raises(ValueError, match="never pending"):
        ticket(c.ValidationStatus.PENDING).for_storage()


def test_checks_run_must_match_the_checks() -> None:
    good = ticket()
    with pytest.raises(ValidationError, match="checks_run"):
        c.HandoffTicket.model_validate(
            {
                **good.model_dump(),
                "completeness": {"checks_run": 2, "share": 1.0, "notes": []},
            }
        )


def test_a_ticket_holds_no_question_text() -> None:
    """FR-020: built from a synthetic MCQ sample, the ticket holds none of its text."""
    dumped = json.dumps(ticket().model_dump(mode="json"), ensure_ascii=False)
    for text in (*STEMS, *CHOICES):
        assert text not in dumped
