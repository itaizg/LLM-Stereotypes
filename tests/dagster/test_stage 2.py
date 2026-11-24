"""run_stage maps exit codes to step outcomes and never leaks the environment.

Covers SC-004 (every exit code mapped) and SC-009 (no secret in a step log).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import dagster as dg
import pytest
from exam_dagster import stage

if TYPE_CHECKING:
    from fakes import FakeRunner

SENTINEL = "postgresql://pipeline:SENTINEL-PASSWORD@localhost:5432/pipeline"


def test_success_returns_the_summary_line(fake_runner: FakeRunner) -> None:
    out = "noise\nfetch: fetched=3 unchanged=12 refused=0 errors=0 skipped=1\n"
    fake_runner.script[("fetch",)] = (0, out, "")

    result = stage.run_stage(["fetch", "--exam", "csat"], exam="csat")

    assert result == {
        "stage": "fetch",
        "exam": "csat",
        "summary": "fetch: fetched=3 unchanged=12 refused=0 errors=0 skipped=1",
    }


def test_success_without_a_summary_line_gives_an_empty_summary(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.script[("catalog",)] = (0, "ok\n", "")

    assert stage.run_stage(["catalog"])["summary"] == ""


@pytest.mark.parametrize("code", [1, 4])
def test_retryable_exit_codes_raise_a_retryable_error(
    fake_runner: FakeRunner, code: int
) -> None:
    fake_runner.script[("fetch",)] = (code, "", "boom")

    with pytest.raises(stage.RetryableStageError):
        stage.run_stage(["fetch"])


@pytest.mark.parametrize("code", [2, 3, 99])
def test_other_exit_codes_fail_without_retry(
    fake_runner: FakeRunner, code: int
) -> None:
    fake_runner.script[("fetch",)] = (code, "", "")

    with pytest.raises(dg.Failure) as info:
        stage.run_stage(["fetch"])

    assert info.value.allow_retries is False


def test_retry_policy_is_defined_once() -> None:
    assert stage.RETRY_POLICY.max_retries == 2
    assert stage.RETRY_POLICY.delay == 180


def test_command_and_working_directory(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIPELINE_PROJECT", "/opt/project")

    stage.run_stage(["discover", "--exam", "enem"])

    assert fake_runner.commands == [
        (sys.executable, "-m", "pipeline", "discover", "--exam", "enem")
    ]
    assert fake_runner.cwds == ["/opt/project"]


def test_working_directory_defaults_to_this_checkout(
    fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIPELINE_PROJECT", raising=False)

    stage.run_stage(["catalog"])

    assert (Path(fake_runner.cwds[0]) / "pipeline" / "__main__.py").is_file()


def test_logs_argv_output_lines_and_exit_code(
    fake_runner: FakeRunner, caplog: pytest.LogCaptureFixture
) -> None:
    fake_runner.script[("fetch",)] = (0, "line one\nline two\n", "careful\n")
    with caplog.at_level(logging.DEBUG, logger=stage.__name__):
        stage.run_stage(["fetch", "--exam", "csat"])

    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert records == [
        (logging.INFO, "pipeline fetch --exam csat"),
        (logging.INFO, "line one"),
        (logging.INFO, "line two"),
        (logging.WARNING, "careful"),
        (logging.INFO, "exit=0"),
    ]


def test_no_environment_value_reaches_the_log(
    fake_runner: FakeRunner,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_DSN", SENTINEL)
    fake_runner.script[("fetch",)] = (1, "", "failed")

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(stage.RetryableStageError) as info,
    ):
        stage.run_stage(["fetch", "--exam", "csat"])

    logged = "\n".join(r.getMessage() for r in caplog.records) + str(info.value)
    assert "SENTINEL-PASSWORD" not in logged
    assert SENTINEL not in logged


@pytest.mark.parametrize(
    ("stdout", "prefix", "expected"),
    [
        ("a\nexams: count=3\n", "exams", "exams: count=3"),
        ("exams: count=1\nexams: count=2\n", "exams", "exams: count=2"),
        ("candidate x exam=y domain=z\n", "candidates", ""),
        ("", "snapshot", ""),
    ],
)
def test_summary_line(stdout: str, prefix: str, expected: str) -> None:
    assert stage.summary_line(stdout, prefix) == expected


def test_listed_ids_skip_the_summary_line() -> None:
    lines = ["csat", "enem", "", "exams: count=2"]

    assert stage.listed_ids(lines, "exams") == ["csat", "enem"]


def test_pending_ids_parse_only_candidate_lines() -> None:
    lines = [
        "candidate src_1 exam=a domain=a.test",
        "candidate src_2 exam=a domain=a.test",
        "candidates: pending=2",
    ]

    assert stage.pending_ids(lines) == ["src_1", "src_2"]


def test_a_stage_that_never_returns_is_killed_and_counted_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage, "STAGE_TIMEOUT_SECONDS", 0.5)

    done = stage._subprocess_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd="."
    )

    assert done.returncode == 1
    assert "timed out" in done.stderr


def test_the_failure_message_carries_the_first_stderr_line(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.script[("catalog",)] = (
        3,
        "",
        "refused: exam sat: keys_evidence must cite an approved source\nsecond line\n",
    )

    with pytest.raises(dg.Failure) as info:
        stage.run_stage(["catalog"])

    assert info.value.description == (
        "pipeline catalog exited 3: "
        "refused: exam sat: keys_evidence must cite an approved source"
    )


def test_a_retryable_failure_carries_the_reason_too(fake_runner: FakeRunner) -> None:
    fake_runner.script[("fetch",)] = (1, "", "\n  error: connection failed\n")

    with pytest.raises(stage.RetryableStageError, match="exited 1: error: connection"):
        stage.run_stage(["fetch"])


def test_a_very_long_reason_is_cut(fake_runner: FakeRunner) -> None:
    fake_runner.script[("catalog",)] = (3, "", "refused: " + "x" * 5000)

    with pytest.raises(dg.Failure) as info:
        stage.run_stage(["catalog"])

    assert len(info.value.description or "") < 400
    assert (info.value.description or "").endswith("…")


def test_a_url_password_in_stderr_is_masked_in_the_message(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.script[("catalog",)] = (
        1,
        "",
        "error: bad connection string postgresql://pipeline:SENTINEL-PASSWORD@db/x",
    )

    with pytest.raises(stage.RetryableStageError) as info:
        stage.run_stage(["catalog"])

    assert "SENTINEL-PASSWORD" not in str(info.value)
    assert "postgresql://pipeline:***@db/x" in str(info.value)


def test_no_stderr_leaves_the_message_unchanged(fake_runner: FakeRunner) -> None:
    fake_runner.script[("fetch",)] = (2, "", "")

    with pytest.raises(dg.Failure) as info:
        stage.run_stage(["fetch"])

    assert info.value.description == "pipeline fetch exited 2"
