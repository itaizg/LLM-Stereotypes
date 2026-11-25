"""Standing budget approval for unattended runs (spec 004, data-model.md).

A person approves a budget once at the terminal. The approval lasts 30 days and covers
only the exact models and prices it was given for, so a price or model change makes it
stop applying instead of silently spending under new terms (FR-004). At most one
approval is active; a new one revokes the old one in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from enum import StrEnum

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from crawler.config import Budget, RunConfig

APPROVAL_DAYS = 30


class RefusalReason(StrEnum):
    NONE_ACTIVE = "none_active"
    EXPIRED = "expired"
    PRICING_CHANGED = "pricing_changed"
    OVER_CAP = "over_cap"


class Limits(BaseModel):
    """What the approval caps. The cost cap is in USD."""

    model_config = ConfigDict(frozen=True)

    max_queries: int
    max_cost_usd: float = Field(json_schema_extra={"unit": "USD"})
    max_iterations: int

    @classmethod
    def of(cls, budget: Budget) -> Limits:
        return cls(
            max_queries=budget.max_queries,
            max_cost_usd=budget.max_cost_usd,
            max_iterations=budget.max_iterations,
        )

    def covers(self, budget: Budget) -> bool:
        return (
            budget.max_queries <= self.max_queries
            and budget.max_cost_usd <= self.max_cost_usd
            and budget.max_iterations <= self.max_iterations
        )


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    limits: Limits
    pricing_fingerprint: str


class Refusal(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: RefusalReason

    def message(self) -> str:
        return {
            RefusalReason.NONE_ACTIVE: "no standing approval is active",
            RefusalReason.EXPIRED: "the standing approval has expired",
            RefusalReason.PRICING_CHANGED: (
                "model or price changed since the standing approval"
            ),
            RefusalReason.OVER_CAP: "the run's limits exceed the approved cap",
        }[self.reason]


def pricing(config: RunConfig) -> dict[str, object]:
    """The readable form of what the fingerprint covers: models and prices."""
    return {
        "agent_model": config.agent_model.model_dump(mode="json"),
        "classifier_model": config.classifier_model.model_dump(mode="json"),
    }


def pricing_fingerprint(config: RunConfig) -> str:
    canonical = json.dumps(pricing(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_approval_id(now: datetime, token: str | None = None) -> str:
    return f"ap-{now:%Y%m%dT%H%M%S}-{token or secrets.token_hex(2)}"


def create(
    conn: psycopg.Connection, config: RunConfig, approved_by: str, now: datetime
) -> Approval:
    """Insert a new approval and revoke any earlier one, in one transaction."""
    approval = Approval(
        approval_id=new_approval_id(now),
        approved_by=approved_by,
        approved_at=now,
        expires_at=now + timedelta(days=APPROVAL_DAYS),
        limits=Limits.of(config.budget),
        pricing_fingerprint=pricing_fingerprint(config),
    )
    with conn.transaction():
        conn.execute(
            "UPDATE crawler_budget_approvals SET revoked_at = %s "
            "WHERE revoked_at IS NULL",
            (now,),
        )
        conn.execute(
            "INSERT INTO crawler_budget_approvals (approval_id, approved_by, "
            "approved_at, expires_at, limits, pricing_fingerprint, pricing) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                approval.approval_id,
                approval.approved_by,
                approval.approved_at,
                approval.expires_at,
                Jsonb(approval.limits.model_dump(mode="json")),
                approval.pricing_fingerprint,
                Jsonb(pricing(config)),
            ),
        )
    return approval


def revoke(conn: psycopg.Connection, now: datetime) -> bool:
    """Revoke the active approval. True when there was one."""
    cursor = conn.execute(
        "UPDATE crawler_budget_approvals SET revoked_at = %s WHERE revoked_at IS NULL",
        (now,),
    )
    return cursor.rowcount > 0


def check(
    conn: psycopg.Connection, config: RunConfig, now: datetime
) -> Approval | Refusal:
    """The active approval if it covers this run, else why it does not."""
    row = conn.execute(
        "SELECT approval_id, approved_by, approved_at, expires_at, limits, "
        "pricing_fingerprint FROM crawler_budget_approvals WHERE revoked_at IS NULL"
    ).fetchone()
    if row is None:
        return Refusal(reason=RefusalReason.NONE_ACTIVE)
    approval = Approval(
        approval_id=row[0],
        approved_by=row[1],
        approved_at=row[2],
        expires_at=row[3],
        limits=Limits.model_validate(row[4]),
        pricing_fingerprint=row[5],
    )
    if now >= approval.expires_at:
        return Refusal(reason=RefusalReason.EXPIRED)
    if approval.pricing_fingerprint != pricing_fingerprint(config):
        return Refusal(reason=RefusalReason.PRICING_CHANGED)
    if not approval.limits.covers(config.budget):
        return Refusal(reason=RefusalReason.OVER_CAP)
    return approval
