"""Console lines derived from spans (spec 004 T029)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from crawler.logs import RunLog, configure_logging
from crawler.tracing import CONTENT_KEYS, Tracing, open_trace

RUN_ID = "cr_20261125T020000_abcd"
SECRET = "sk-ant-SECRET-9012"


def traced(fmt: str | None = None) -> tuple[Tracing, io.StringIO, io.StringIO]:
    env = {"ANTHROPIC_API_KEY": SECRET}
    if fmt:
        env["CRAWLER_LOG_FORMAT"] = fmt
    console, sink = io.StringIO(), io.StringIO()
    logger = configure_logging(env, console)
    return Tracing(RUN_ID, sink, env=env, console=logger), console, sink


def lines(console: io.StringIO) -> list[str]:
    return console.getvalue().splitlines()


def test_one_text_line_per_span_with_the_named_fields() -> None:
    tracing, console, _ = traced()
    with tracing.span("tool.probe_url") as span:
        span.set_attribute("outcome", "ok")
    tracing.shutdown()
    [line] = lines(console)
    assert " INFO " in line
    for part in (f"run={RUN_ID}", "step=tool.probe_url", "tool=probe_url"):
        assert part in line
    assert "outcome=ok" in line and "duration_ms=" in line


def test_failures_are_errors_and_skips_are_not() -> None:
    tracing, console, _ = traced()
    with tracing.span("tool.probe_url") as span:
        tracing.mark_failed(span, "timeout")
    with tracing.span("tool.web_search") as span:
        span.set_attribute("error", "RuntimeError")
    with tracing.span("tool.probe_url") as span:
        span.set_attribute("outcome", "skipped_dead_host")
    with tracing.span("agent.step"):
        pass
    tracing.shutdown()
    levels = [line.split()[1] for line in lines(console)]
    assert levels == ["ERROR", "ERROR", "INFO", "INFO"]
    assert "tool= " in lines(console)[-1]  # no tool on a non-tool span


def test_json_mode_is_one_object_per_line() -> None:
    tracing, console, _ = traced("json")
    with tracing.span("tool.probe_url") as span:
        span.set_attribute("outcome", "ok")
    tracing.shutdown()
    [record] = [json.loads(line) for line in lines(console)]
    assert record["level"] == "INFO" and record["run_id"] == RUN_ID
    assert record["step"] == "tool.probe_url" and record["tool"] == "probe_url"
    assert record["outcome"] == "ok" and isinstance(record["duration_ms"], int)


def test_lines_never_carry_content_or_secrets() -> None:
    tracing, console, _ = traced()
    with tracing.span("agent.step") as span:
        tracing.generation(
            span, "m", 1, 1, 0.0, f"prompt {SECRET} text", "response text"
        )
        tracing.mark_failed(span, f"bad {SECRET}")
    tracing.shutdown()
    text = console.getvalue()
    assert SECRET not in text and "prompt" not in text and "response text" not in text
    assert not any(key in text for key in CONTENT_KEYS)


def test_run_level_lines_use_the_cli_step_and_redact() -> None:
    console = io.StringIO()
    log = RunLog(configure_logging({}, console), RUN_ID, [SECRET])
    log.error(f"crawler: failed: {SECRET}")
    [line] = lines(console)
    assert "step=crawler.cli" in line and SECRET not in line and " ERROR " in line


def test_console_lines_match_trace_records(tmp_path: Path) -> None:
    console = io.StringIO()
    tracing = open_trace(RUN_ID, tmp_path, {}, configure_logging({}, console))
    for name in ("crawler.run", "agent.step", "tool.probe_url", "tool.sample"):
        with tracing.span(name):
            pass
    tracing.shutdown()
    records = (tmp_path / "trace.jsonl").read_text().splitlines()
    assert len(lines(console)) == len(records) == 4
