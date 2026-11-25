"""The run configuration and its defaults (FR-002, FR-004, FR-016a)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from crawler.config import (
    REASON_CLASS,
    RUN_ID_PATTERN,
    ModelSpec,
    RunConfig,
    catalog_gaps,
    load_overrides,
    new_run_id,
    reason_class,
)
from crawler.contracts import ReasonCode

RUN_ID = "cr_20261123T101500_a3f9"


def test_the_default_budget_is_the_specs() -> None:
    config = RunConfig(run_id=RUN_ID)
    assert config.budget.max_queries == 500
    assert config.budget.max_cost_usd == 10.0
    assert config.budget.max_iterations == 100
    assert config.budget.max_consecutive_failures == 5
    assert config.budget.max_tool_calls_per_iteration == 20
    assert (config.host_interval_s, config.concurrency) == (5.0, 8)
    assert config.classifier_max_cost_usd == 0.02


def test_run_ids_follow_the_pattern() -> None:
    moment = datetime(2026, 11, 23, 10, 15, 0, tzinfo=UTC)
    assert new_run_id(moment, "a3f9") == RUN_ID
    assert RUN_ID_PATTERN.match(new_run_id())
    with pytest.raises(ValidationError):
        RunConfig(run_id="run-1")


def test_unknown_settings_are_refused() -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"run_id": RUN_ID, "max_queries": 10})


def test_every_reason_has_exactly_one_class() -> None:
    assert set(REASON_CLASS) == set(ReasonCode)
    transient = {r for r, c in REASON_CLASS.items() if c == "transient"}
    assert transient == {
        ReasonCode.TIMEOUT,
        ReasonCode.HTTP_5XX,
        ReasonCode.CONNECTION_ERROR,
        ReasonCode.TOO_LARGE_UNSAMPLED,
        ReasonCode.NOT_CLASSIFIED,
    }
    assert reason_class(ReasonCode.BLOCKED) == "permanent"


def test_costs_come_from_token_usage_and_prices() -> None:
    model = ModelSpec(
        model="m",
        price_in_usd_per_mtok=4.0,
        price_out_usd_per_mtok=20.0,
        max_output_tokens=1_000,
    )
    assert model.cost_usd(1_000_000, 0) == pytest.approx(4.0)
    assert model.worst_case_usd(500_000) == pytest.approx(2.0 + 0.02)


def test_overrides_and_gaps_are_read_from_files(tmp_path: Path) -> None:
    config = tmp_path / "run.yaml"
    config.write_text("refresh: true\nbudget: {max_queries: 50}\n", encoding="utf-8")
    overrides = load_overrides(config)
    assert (
        RunConfig.model_validate({"run_id": RUN_ID, **overrides}).budget.max_queries
        == 50
    )
    todo = tmp_path / "TODO.md"
    todo.write_text(
        "## Exam catalog gaps (spec 001)\n\n- [ ] **Gaokao: find a key.** More.\n"
        "- [x] **Done gap.**\n## Other\n- [ ] **Not a gap.**\n",
        encoding="utf-8",
    )
    assert catalog_gaps(todo) == ("Gaokao: find a key.",)
    assert catalog_gaps(tmp_path / "missing.md") == ()
    (tmp_path / "bad.yaml").write_text("- a\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_overrides(tmp_path / "bad.yaml")
