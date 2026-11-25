"""Tracing (FR-032, research R10): run_id on every span, secrets redacted."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from crawler.tracing import Tracing, open_trace

RUN_ID = "cr_20261123T101500_a3f9"
ENV = {
    "TAVILY_API_KEY": "tvly-SECRET-1234",
    "BRAVE_API_KEY": "brave-SECRET-5678",
    "ANTHROPIC_API_KEY": "sk-ant-SECRET-9012",
    "XERJ_TOKEN": "xerj-SECRET-3456",
}


def spans(sink: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def test_every_span_carries_the_run_id_and_its_parent() -> None:
    sink = io.StringIO()
    tracing = Tracing(RUN_ID, sink, env={})
    with (
        tracing.span("agent.step", iteration=1),
        tracing.span("tool.probe_url", url="u"),
    ):
        pass
    tracing.shutdown()
    tool, step = spans(sink)
    assert {tool["name"], step["name"]} == {"tool.probe_url", "agent.step"}
    for span in (tool, step):
        assert span["attributes"]["run_id"] == RUN_ID
    assert tool["parent_id"] == step["span_id"]


def test_secrets_never_reach_the_trace() -> None:
    sink = io.StringIO()
    tracing = Tracing(RUN_ID, sink, env=ENV)
    with tracing.span("tool.web_search", detail=f"key={ENV['TAVILY_API_KEY']}"):
        pass
    with pytest.raises(RuntimeError), tracing.span("tool.failing"):
        raise RuntimeError(ENV["ANTHROPIC_API_KEY"])
    tracing.shutdown()
    text = sink.getvalue()
    for secret in ENV.values():
        assert secret not in text
    assert "***" in text
    assert tracing.redact(f"x {ENV['XERJ_TOKEN']}") == "x ***"
    assert spans(sink)[-1]["status"] == "ERROR"


def test_the_trace_file_is_written_under_the_run(tmp_path: Path) -> None:
    tracing = open_trace(
        RUN_ID, tmp_path / RUN_ID, env={"OTEL_EXPORTER_OTLP_ENDPOINT": "x"}
    )
    with tracing.span("agent.step"):
        pass
    tracing.shutdown()
    lines = (tmp_path / RUN_ID / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["attributes"]["run_id"] == RUN_ID


def test_trigger_token_and_otlp_headers_are_redacted() -> None:
    """Spec 004 FR-010/FR-016: the new secrets are redacted like the API keys."""
    env = {
        "CRAWLER_TRIGGER_TOKEN": "trigger-SECRET-0123456789abcdef0123",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic cGstbGYtU0VDUkVUOnNrLWxm",
    }
    sink = io.StringIO()
    tracing = Tracing(RUN_ID, sink, env=env)
    with tracing.span(
        "tool.web_search",
        detail=f"token={env['CRAWLER_TRIGGER_TOKEN']} auth=cGstbGYtU0VDUkVUOnNrLWxm",
    ):
        pass
    tracing.shutdown()
    text = sink.getvalue()
    assert "trigger-SECRET" not in text
    assert "cGstbGYtU0VDUkVUOnNrLWxm" not in text
