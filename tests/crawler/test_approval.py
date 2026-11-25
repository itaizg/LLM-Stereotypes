"""Standing budget approval: expiry, fingerprint, caps, revoke (spec 004, T008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from crawler import approval
from crawler.approval import Approval, Refusal, RefusalReason
from crawler.config import Budget, ModelSpec, RunConfig

NOW = datetime(2026, 11, 25, 2, 0, tzinfo=UTC)
RUN_ID = "cr_20261125T020000_abcd"


def config(**changes: object) -> RunConfig:
    return RunConfig.model_validate({"run_id": RUN_ID, **changes})


def test_approve_stores_a_thirty_day_approval(crawler_db: psycopg.Connection) -> None:
    made = approval.create(crawler_db, config(), "maintainer", NOW)
    assert made.expires_at == NOW + timedelta(days=30)
    result = approval.check(crawler_db, config(), NOW)
    assert isinstance(result, Approval) and result.approval_id == made.approval_id


def test_a_new_approval_revokes_the_old_one(crawler_db: psycopg.Connection) -> None:
    first = approval.create(crawler_db, config(), "maintainer", NOW)
    second = approval.create(crawler_db, config(), "maintainer", NOW)
    active = crawler_db.execute(
        "SELECT approval_id FROM crawler_budget_approvals WHERE revoked_at IS NULL"
    ).fetchall()
    assert first.approval_id != second.approval_id
    assert active == [(second.approval_id,)]


def test_the_database_allows_one_active_approval(
    crawler_db: psycopg.Connection,
) -> None:
    approval.create(crawler_db, config(), "maintainer", NOW)
    with pytest.raises(psycopg.errors.UniqueViolation):
        crawler_db.execute(
            "INSERT INTO crawler_budget_approvals (approval_id, approved_by, "
            "approved_at, expires_at, limits, pricing_fingerprint, pricing) "
            "VALUES ('x', 'm', %s, %s, '{}', %s, '{}')",
            (NOW, NOW + timedelta(days=1), "0" * 64),
        )


def test_revoke(crawler_db: psycopg.Connection) -> None:
    assert approval.revoke(crawler_db, NOW) is False
    approval.create(crawler_db, config(), "maintainer", NOW)
    assert approval.revoke(crawler_db, NOW) is True
    result = approval.check(crawler_db, config(), NOW)
    assert result == Refusal(reason=RefusalReason.NONE_ACTIVE)


def test_limits_at_or_below_the_cap_pass(crawler_db: psycopg.Connection) -> None:
    approval.create(crawler_db, config(), "maintainer", NOW)
    smaller = config(budget=Budget(max_queries=10, max_cost_usd=1.0, max_iterations=5))
    assert isinstance(approval.check(crawler_db, smaller, NOW), Approval)


def test_no_approval_is_refused(crawler_db: psycopg.Connection) -> None:
    assert approval.check(crawler_db, config(), NOW) == Refusal(
        reason=RefusalReason.NONE_ACTIVE
    )


def test_an_expired_approval_is_refused(crawler_db: psycopg.Connection) -> None:
    made = approval.create(crawler_db, config(), "maintainer", NOW)
    assert approval.check(crawler_db, config(), made.expires_at) == Refusal(
        reason=RefusalReason.EXPIRED
    )


def test_a_changed_price_or_model_is_refused(crawler_db: psycopg.Connection) -> None:
    approval.create(crawler_db, config(), "maintainer", NOW)
    base = config().agent_model
    dearer = ModelSpec(
        model=base.model,
        price_in_usd_per_mtok=base.price_in_usd_per_mtok + 1,
        price_out_usd_per_mtok=base.price_out_usd_per_mtok,
        max_output_tokens=base.max_output_tokens,
    )
    renamed = dearer.model_copy(
        update={
            "model": "another-model",
            "price_in_usd_per_mtok": base.price_in_usd_per_mtok,
        }
    )
    for changed in (dearer, renamed):
        result = approval.check(crawler_db, config(agent_model=changed), NOW)
        assert result == Refusal(reason=RefusalReason.PRICING_CHANGED)


@pytest.mark.parametrize(
    "budget",
    [
        Budget(max_queries=501),
        Budget(max_cost_usd=10.01),
        Budget(max_iterations=101),
    ],
)
def test_a_limit_above_the_cap_is_refused(
    crawler_db: psycopg.Connection, budget: Budget
) -> None:
    approval.create(crawler_db, config(), "maintainer", NOW)
    result = approval.check(crawler_db, config(budget=budget), NOW)
    assert result == Refusal(reason=RefusalReason.OVER_CAP)


def test_every_refusal_has_a_message() -> None:
    for reason in RefusalReason:
        assert Refusal(reason=reason).message()
