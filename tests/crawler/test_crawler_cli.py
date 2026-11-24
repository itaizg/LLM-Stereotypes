"""``python -m crawler``: approval, keys, exit codes, summary (contracts/cli.md)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from crawler_fakes import FakeClassifier, FakeDetector, FakeResolver, VirtualClock
from langchain_core.messages import AIMessage, BaseMessage

from crawler import cli
from crawler.config import RunConfig
from crawler.graph import Tool
from crawler.memory import NullMemory

NOW = datetime(2026, 11, 23, 10, 15, tzinfo=UTC)
SECRET = "sk-ant-SECRET-0000"


class Done:
    """A model that ends at once (or fails, to exercise exit 1 and 5)."""

    def __init__(self, mode: str = "done") -> None:
        self.mode = mode

    async def ainvoke(self, input: list[BaseMessage]) -> BaseMessage:
        if self.mode == "boom":
            raise RuntimeError(SECRET)
        if self.mode == "refuse":
            return AIMessage(content="", response_metadata={"stop_reason": "refusal"})
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        return AIMessage(content="nothing to do", usage_metadata=usage)


def factory(mode: str = "done") -> cli.Factory:
    def model(config: RunConfig, env: Mapping[str, str], tools: dict[str, Tool]) -> Any:
        if mode == "crash":
            raise RuntimeError(f"cannot start {SECRET}")
        return Done(mode)

    return cli.Factory(
        model=model,
        classifier=lambda config, env: FakeClassifier(),
        detector=FakeDetector,
        memory=lambda env: NullMemory(),
        http=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ),
        resolver=FakeResolver,
        clock=VirtualClock,
    )


@pytest.fixture
def env(crawler_dsn: str) -> dict[str, str]:
    return {
        "PIPELINE_DSN": crawler_dsn,
        "ANTHROPIC_API_KEY": SECRET,
        "TAVILY_API_KEY": "tvly-x",
        "USER": "maintainer",
    }


@pytest.fixture(autouse=True)
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "RESULTS", tmp_path)
    return tmp_path


def run(env: Mapping[str, str], answer: str | None = "y", mode: str = "done") -> int:
    return cli.main(
        ["run"], env=env, ask=lambda _: answer, factory=factory(mode), now=lambda: NOW
    )


def runs(dsn: str) -> list[tuple[Any, ...]]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT status, approved_by, approved_at FROM crawler_runs"
        ).fetchall()


@pytest.mark.parametrize("answer", [None, "", "n", "yes please"])
def test_without_approval_nothing_runs(env: dict[str, str], answer: str | None) -> None:
    assert run(env, answer) == 2
    assert runs(env["PIPELINE_DSN"]) == []


def test_a_missing_key_is_named_never_printed(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    del env["TAVILY_API_KEY"]
    assert run(env) == 2
    err = capsys.readouterr().err
    assert "TAVILY_API_KEY is not set" in err and SECRET not in err


def test_an_approved_run_is_recorded_and_summarised(
    env: dict[str, str], results: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env) == 0
    [(status, by, at)] = runs(env["PIPELINE_DSN"])
    assert (status, by, at) == ("completed", "maintainer", NOW)
    summary = json.loads(next(results.glob("cr_*/summary.json")).read_text())
    assert summary["status"] == "completed" and summary["completeness"] == 1.0
    assert summary["cost_usd"] >= 0 and summary["stop_limit"] is None
    assert "Approve" not in capsys.readouterr().out  # the prompt goes through ask()


def test_repeated_refusals_exit_five(env: dict[str, str]) -> None:
    assert run(env, mode="refuse") == 5
    assert runs(env["PIPELINE_DSN"])[0][0] == "escalated"


def test_an_unexpected_error_exits_one_without_leaking_secrets(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, mode="crash") == 1
    assert runs(env["PIPELINE_DSN"])[0][0] == "failed"
    assert SECRET not in capsys.readouterr().err


def test_a_limit_exits_six(env: dict[str, str], tmp_path: Path) -> None:
    config = tmp_path / "run.yaml"
    config.write_text("budget: {max_cost_usd: 0.000001}\n", encoding="utf-8")
    code = cli.main(
        ["run", "--config", str(config)],
        env=env,
        ask=lambda _: "y",
        factory=factory(),
        now=lambda: NOW,
    )
    assert code == 6
    assert runs(env["PIPELINE_DSN"])[0][0] == "stopped_at_limit"


def test_status_lists_runs_and_counts(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    run(env)
    capsys.readouterr()
    assert cli.main(["status"], env=env) == 0
    out = capsys.readouterr().out
    assert "runs: count=1" in out
    run_id = out.split("\t", 1)[0]
    assert cli.main(["status", run_id], env=env) == 0
    assert "status: completed" in capsys.readouterr().out
    assert cli.main(["status", "cr_20000101T000000_0000"], env=env) == 2


def test_usage_errors_exit_two(env: dict[str, str]) -> None:
    assert cli.main(["nonsense"], env=env) == 2
    assert cli.main(["status"], env={}) == 2


def test_reopened_sources_are_reported_with_the_command(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "reopened_count", lambda conn, run_id: 2)
    assert run(env) == 0
    err = capsys.readouterr().err
    assert "2 approved source(s) changed" in err
    assert "pipeline import-decisions --reopen-only" in err


def test_reopened_count_reads_this_runs_flags(crawler_db: Any) -> None:
    from crawler_fakes import RUN_ID, add_proposal, add_run

    add_run(crawler_db)
    add_proposal(crawler_db, "https://a.test/p.pdf", flags=("reopened_after_approval",))
    add_proposal(crawler_db, "https://b.test/p.pdf")
    assert cli.reopened_count(crawler_db, RUN_ID) == 1
