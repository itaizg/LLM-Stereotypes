"""`crawler run --approval standing`: refusal, record, lock, run id, SIGTERM (T009)."""

from __future__ import annotations

import json
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from crawler_fakes import FakeClassifier, FakeDetector, FakeResolver, VirtualClock
from langchain_core.messages import BaseMessage
from test_crawler_cli import NOW, SECRET, Done

from crawler import approval, cli
from crawler.config import RunConfig
from crawler.graph import Tool
from crawler.memory import NullMemory

RUN_ID = "cr_20261123T101500_beef"


class Paid(Exception):
    """Raised by a fake that must never be reached."""


def build(model: Any) -> cli.Factory:
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


def finishing(config: RunConfig, env: Mapping[str, str], tools: dict[str, Tool]) -> Any:
    return Done()


def never(config: RunConfig, env: Mapping[str, str], tools: dict[str, Tool]) -> Any:
    raise Paid("a paid call was made without an approval")


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


def standing(env: Mapping[str, str], factory: cli.Factory, *extra: str) -> int:
    def refuse_prompt(question: str) -> str | None:
        raise AssertionError("a standing run must not prompt")

    return cli.main(
        ["run", "--approval", "standing", *extra],
        env=env,
        ask=refuse_prompt,
        factory=factory,
        now=lambda: NOW,
    )


def approve(dsn: str, by: str = "someone-else") -> str:
    config = RunConfig.model_validate({"run_id": RUN_ID})
    with psycopg.connect(dsn, autocommit=True) as conn:
        return approval.create(conn, config, by, NOW).approval_id


def rows(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT run_id, status, approved_by, approved_at, approval_id "
            "FROM crawler_runs"
        ).fetchall()


def test_no_approval_exits_three_before_any_paid_call(
    env: dict[str, str], results: Path
) -> None:
    assert standing(env, build(never)) == 3
    assert rows(env["PIPELINE_DSN"]) == []
    summary = json.loads(next(results.glob("cr_*/summary.json")).read_text())
    assert summary["status"] == "not_approved" and summary["reason"] == "none_active"


def test_a_valid_approval_is_recorded_on_the_run(env: dict[str, str]) -> None:
    approval_id = approve(env["PIPELINE_DSN"])
    assert standing(env, build(finishing)) == 0
    [(_, status, by, at, recorded)] = rows(env["PIPELINE_DSN"])
    assert status == "completed"
    assert (by, at, recorded) == ("someone-else", NOW, approval_id)


def test_a_busy_lock_exits_four_and_writes_no_row(
    env: dict[str, str], results: Path
) -> None:
    approve(env["PIPELINE_DSN"])
    with psycopg.connect(env["PIPELINE_DSN"], autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(hashtext('crawler.run'))")
        assert standing(env, build(never)) == 4
    assert rows(env["PIPELINE_DSN"]) == []
    summary = json.loads(next(results.glob("cr_*/summary.json")).read_text())
    assert summary["status"] == "busy"


def test_the_lock_is_released_after_a_run(env: dict[str, str]) -> None:
    approve(env["PIPELINE_DSN"])
    assert standing(env, build(finishing), "--run-id", RUN_ID) == 0
    assert standing(env, build(finishing), "--run-id", "cr_20261123T101501_beef") == 0


def test_a_bad_or_used_run_id_is_a_usage_error(env: dict[str, str]) -> None:
    approve(env["PIPELINE_DSN"])
    assert standing(env, build(never), "--run-id", "not-a-run-id") == 2
    assert standing(env, build(finishing), "--run-id", RUN_ID) == 0
    assert standing(env, build(never), "--run-id", RUN_ID) == 2


def test_sigterm_marks_the_run_failed_and_terminated(
    env: dict[str, str], results: Path
) -> None:
    approve(env["PIPELINE_DSN"])

    class Blocked:
        async def ainvoke(self, input: list[BaseMessage]) -> BaseMessage:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)  # what the trigger's SIGTERM does
            raise AssertionError("the handler should have raised")

    def blocked(
        config: RunConfig, env: Mapping[str, str], tools: dict[str, Tool]
    ) -> Any:
        return Blocked()

    before = signal.getsignal(signal.SIGTERM)
    assert standing(env, build(blocked)) == 1
    assert signal.getsignal(signal.SIGTERM) == before  # restored afterwards
    [(_, status, *_)] = rows(env["PIPELINE_DSN"])
    assert status == "failed"
    summary = json.loads(next(results.glob("cr_*/summary.json")).read_text())
    assert summary["terminated"] is True


def test_approve_and_revoke_commands(env: dict[str, str]) -> None:
    def ask(question: str) -> str | None:
        assert "standing budget for 30 days" in question
        return "y"

    assert cli.main(["approve"], env=env, ask=ask, now=lambda: NOW) == 0
    assert cli.main(["approve"], env=env, ask=lambda _: "n", now=lambda: NOW) == 3
    assert cli.main(["approve"], env=env, ask=lambda _: None, now=lambda: NOW) == 3
    assert standing(env, build(finishing)) == 0
    assert cli.main(["approve", "--revoke"], env=env, now=lambda: NOW) == 0
    assert standing(env, build(never)) == 3


def test_approve_needs_the_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["approve"], env={}, ask=lambda _: "y") == 2
    assert "PIPELINE_DSN" in capsys.readouterr().err
