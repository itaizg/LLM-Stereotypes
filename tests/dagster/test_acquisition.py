"""The ``exam_acquisition`` assets, materialised in-process on an ephemeral instance.

No daemon and no subprocess: ``FakeRunner`` answers every stage command, so the tests
see exactly which commands the assets issue and how each outcome is handled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dagster as dg
import pytest
from dagster import DagsterInstance
from exam_dagster.assets import AcquisitionAssets, build_assets

if TYPE_CHECKING:
    from fakes import FakeRunner

ARGV_FILE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "orchestration_acquisition_argv.json"
)
CONTRACT: dict[str, Any] = json.loads(ARGV_FILE.read_text(encoding="utf-8"))
EXAMS: list[str] = CONTRACT["exams"]

# Retry at once: a three-minute delay would stall the test.
FAST = dg.RetryPolicy(max_retries=2, delay=0)


def by_exam(argvs: list[list[str]]) -> dict[str, list[list[str]]]:
    """Group ``--exam X`` commands by exam, keeping each exam's own order."""
    grouped: dict[str, list[list[str]]] = {}
    for argv in argvs:
        if "--exam" in argv:
            grouped.setdefault(argv[argv.index("--exam") + 1], []).append(argv)
    return grouped


def run_catalog(built: AcquisitionAssets) -> dg.ExecuteInProcessResult:
    return dg.materialize(
        [built.catalog], instance=DagsterInstance.ephemeral(), raise_on_error=False
    )


def run_exam(built: AcquisitionAssets, exam: str) -> dg.ExecuteInProcessResult:
    """One partition of the exam assets (``catalog`` is a separate, once-only run)."""
    return dg.materialize(
        [built.catalog, built.discovered, built.fetched],
        selection=[built.discovered, built.fetched],
        partition_key=exam,
        instance=DagsterInstance.ephemeral(),
        raise_on_error=False,
    )


def run_all(built: AcquisitionAssets, exams: list[str]) -> dict[str, bool]:
    """Catalog once, then every exam; ``{exam: succeeded}``."""
    assert run_catalog(built).success
    return {exam: run_exam(built, exam).success for exam in exams}


class TestHappyPath:
    def test_issues_exactly_the_commands_in_the_shared_contract(
        self, fake_runner: FakeRunner
    ) -> None:
        built = build_assets(EXAMS, retry_policy=FAST)

        outcome = run_all(built, EXAMS)

        assert all(outcome.values())
        issued = [list(argv) for argv in fake_runner.argvs]
        assert issued[0] == ["catalog"]
        assert sorted(issued) == sorted(CONTRACT["argv"])
        assert by_exam(issued) == by_exam(CONTRACT["argv"])

    def test_one_exam_runs_only_that_exam(self, fake_runner: FakeRunner) -> None:
        built = build_assets(EXAMS, retry_policy=FAST)

        assert run_exam(built, EXAMS[1]).success

        assert [list(a) for a in fake_runner.argvs] == [
            ["discover", "--exam", EXAMS[1]],
            ["candidates", "--exam", EXAMS[1]],
            ["fetch", "--exam", EXAMS[1]],
        ]

    def test_the_summary_is_reported_as_metadata(self, fake_runner: FakeRunner) -> None:
        fake_runner.script[("fetch",)] = (0, "fetch: fetched=3 unchanged=0\n", "")
        built = build_assets(["a"], retry_policy=FAST)

        result = run_exam(built, "a")

        event = result.get_asset_materialization_events()[-1]
        meta = event.materialization.metadata
        assert meta["summary"].value == "fetch: fetched=3 unchanged=0"


class TestGuards:
    def test_an_invalid_catalog_fails_with_no_retry(
        self, fake_runner: FakeRunner
    ) -> None:
        fake_runner.script[("catalog",)] = (3, "", "refused: catalog invalid")
        built = build_assets(["a"], retry_policy=FAST)

        result = run_catalog(built)

        assert not result.success
        assert [a[0] for a in fake_runner.argvs] == ["catalog"]

    def test_a_failed_catalog_does_not_block_the_exams(
        self, fake_runner: FakeRunner
    ) -> None:
        """Discovery must work on an incomplete catalog: that completes it."""
        fake_runner.script[("catalog",)] = (3, "", "refused: catalog invalid")
        built = build_assets(["a"], retry_policy=FAST)

        result = dg.materialize(
            built.assets,
            partition_key="a",
            instance=DagsterInstance.ephemeral(),
            raise_on_error=False,
        )

        assert not result.success  # the catalog step failed
        assert {a[0] for a in fake_runner.argvs} == {
            "catalog",
            "discover",
            "candidates",
            "fetch",
        }


class TestFailures:
    FETCH_A = ("fetch", "--exam", "a")

    def test_a_failing_exam_does_not_stop_the_other(
        self, fake_runner: FakeRunner
    ) -> None:
        fake_runner.script[self.FETCH_A] = (3, "", "refused: integrity")
        built = build_assets(["a", "b"], retry_policy=FAST)

        outcome = run_all(built, ["a", "b"])

        assert outcome == {"a": False, "b": True}
        issued = [list(a) for a in fake_runner.argvs]
        assert issued.count(list(self.FETCH_A)) == 1  # exit 3 is never retried
        assert ["fetch", "--exam", "b"] in issued

    def test_a_usage_error_is_not_retried(self, fake_runner: FakeRunner) -> None:
        fake_runner.script[self.FETCH_A] = (2, "", "usage")
        built = build_assets(["a"], retry_policy=FAST)

        assert not run_exam(built, "a").success
        assert [list(a) for a in fake_runner.argvs].count(list(self.FETCH_A)) == 1

    def test_an_unexpected_exit_is_tried_three_times_then_fails(
        self, fake_runner: FakeRunner
    ) -> None:
        fake_runner.script[self.FETCH_A] = (1, "", "boom")
        built = build_assets(["a"], retry_policy=FAST)

        assert not run_exam(built, "a").success
        assert [list(a) for a in fake_runner.argvs].count(list(self.FETCH_A)) == 3

    def test_a_held_lock_is_retried_and_then_succeeds(
        self, fake_runner: FakeRunner
    ) -> None:
        fake_runner.then[self.FETCH_A] = [(4, "", "locked: another command is running")]
        built = build_assets(["a"], retry_policy=FAST)

        assert run_exam(built, "a").success
        assert [list(a) for a in fake_runner.argvs].count(list(self.FETCH_A)) == 2


class TestPendingCandidates:
    @pytest.fixture
    def two_pending(self, fake_runner: FakeRunner) -> FakeRunner:
        fake_runner.script[("candidates", "--exam", "a")] = (
            0,
            "candidate src_1 exam=a domain=a.test\n"
            "candidate src_2 exam=a domain=a.test\n"
            "candidates: pending=2\n",
            "",
        )
        return fake_runner

    def test_the_run_stays_successful(self, two_pending: FakeRunner) -> None:
        assert run_exam(build_assets(["a", "b"], retry_policy=FAST), "a").success

    def test_the_check_warns_with_the_pending_ids(
        self, two_pending: FakeRunner
    ) -> None:
        result = run_exam(build_assets(["a", "b"], retry_policy=FAST), "a")

        (check,) = result.get_asset_check_evaluations()
        assert check.check_name == "pending_review"
        assert not check.passed
        assert check.severity == dg.AssetCheckSeverity.WARN
        (event,) = [
            e
            for e in result.get_asset_materialization_events()
            if e.asset_key == dg.AssetKey("discovered")
        ]
        meta = event.materialization.metadata
        assert meta["pending_count"].value == 2
        assert meta["pending_ids"].value == ["src_1", "src_2"]

    def test_the_check_passes_when_nothing_is_pending(
        self, two_pending: FakeRunner
    ) -> None:
        result = run_exam(build_assets(["a", "b"], retry_policy=FAST), "b")

        (check,) = result.get_asset_check_evaluations()
        assert check.passed
