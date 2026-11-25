"""OTLP export, content routing, failures and export errors (spec 004 T021, T022)."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import socket
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from crawler_fakes import FakeResolver, VirtualClock, make_services, make_state
from langchain_core.messages import AIMessage, BaseMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from crawler import tracing as tracing_module
from crawler.contracts import Contract, ToolError, ToolErrorCode, UrlInput
from crawler.graph import Crawler, Tool
from crawler.tracing import (
    CONTENT_KEYS,
    TRUNCATED,
    Tracing,
    open_trace,
)

RUN_ID = "cr_20261125T020000_abcd"
SECRET = "sk-ant-SECRET-9012"


def make(
    mode: str | None = None, secret_env: bool = True
) -> tuple[Tracing, io.StringIO, InMemorySpanExporter]:
    env = {"ANTHROPIC_API_KEY": SECRET} if secret_env else {}
    if mode:
        env["CRAWLER_TRACE_CONTENT"] = mode
    sink = io.StringIO()
    exporter = InMemorySpanExporter()
    return Tracing(RUN_ID, sink, env=env, otlp_exporter=exporter), sink, exporter


def attrs(exporter: InMemorySpanExporter, name: str) -> dict[str, Any]:
    [span] = [s for s in exporter.get_finished_spans() if s.name == name]
    return dict(span.attributes or {})


def generate(tracing: Tracing, prompt: str, response: str) -> None:
    with tracing.span("agent.step") as span:
        tracing.generation(span, "claude-opus-5-5", 10, 5, 0.001, prompt, response)


def test_full_mode_puts_a_generation_on_the_otlp_path() -> None:
    tracing, sink, exporter = make()
    generate(tracing, "the prompt", "the answer")
    tracing.shutdown()
    got = attrs(exporter, "agent.step")
    assert got["langfuse.observation.type"] == "generation"
    assert got["gen_ai.request.model"] == "claude-opus-5-5"
    assert got["gen_ai.usage.input_tokens"] == 10
    assert got["gen_ai.usage.output_tokens"] == 5
    assert got["gen_ai.usage.cost"] == 0.001
    assert len(got["prompt_sha256"]) == len(got["response_sha256"]) == 64
    assert json.loads(got["langfuse.observation.input"]) == "the prompt"
    assert json.loads(got["langfuse.observation.output"]) == "the answer"
    assert tracing.export_status().spans_exported == 1
    for key in CONTENT_KEYS:
        assert key not in sink.getvalue()
    assert "the prompt" not in sink.getvalue()


def test_metadata_mode_carries_no_content_anywhere() -> None:
    tracing, sink, exporter = make("metadata")
    generate(tracing, "the prompt", "the answer")
    tracing.shutdown()
    got = attrs(exporter, "agent.step")
    assert not CONTENT_KEYS & got.keys()
    assert got["prompt_sha256"] and got["langfuse.observation.type"] == "generation"
    assert "the prompt" not in sink.getvalue()


def test_long_content_is_cut_and_marked() -> None:
    tracing, _, exporter = make()
    generate(tracing, "x" * 200_000, "short")
    tracing.shutdown()
    text = json.loads(attrs(exporter, "agent.step")["langfuse.observation.input"])
    assert text.endswith(TRUNCATED) and len(text) < 70_000


def test_secrets_in_content_are_redacted() -> None:
    tracing, _, exporter = make()
    generate(tracing, f"key={SECRET}", f"echo {SECRET}")
    tracing.shutdown()
    got = attrs(exporter, "agent.step")
    assert SECRET not in json.dumps(got)
    assert "***" in got["langfuse.observation.input"]


def test_the_root_span_names_the_session() -> None:
    tracing, _, exporter = make()
    with tracing.span("crawler.run") as root:
        tracing.mark_root(root)
    tracing.shutdown()
    got = attrs(exporter, "crawler.run")
    assert got["langfuse.session.id"] == RUN_ID
    assert got["langfuse.trace.name"] == "crawler.run"


def test_mark_failed_sets_error_status_and_level() -> None:
    tracing, _, exporter = make()
    with tracing.span("tool.probe_url") as span:
        tracing.mark_failed(span, f"timeout {SECRET}")
    tracing.shutdown()
    [span] = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == "timeout ***"
    assert (span.attributes or {})["langfuse.observation.level"] == "ERROR"


# --- graph-level: failures are marked, skips are not ---------------------------------


class Replies:
    def __init__(self, replies: list[AIMessage]) -> None:
        self.replies = replies

    async def ainvoke(self, input: list[BaseMessage]) -> BaseMessage:
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="done", usage_metadata=_usage())


def _usage() -> Any:
    return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def call_reply() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "probe_url",
                "args": {"url": "https://a.test/x"},
                "id": "c0",
                "type": "tool_call",
            }
        ],
        usage_metadata=_usage(),
    )


def traced_run(
    replies: list[AIMessage], error: ToolErrorCode | None
) -> InMemorySpanExporter:
    tracing, _, exporter = make()

    async def run(args: UrlInput) -> Contract:
        if error is None:
            raise AssertionError("not used")
        return ToolError(error=error, retryable=False)

    tools = {"probe_url": Tool("probe_url", "probe", UrlInput, run, timeout_s=5.0)}

    async def go() -> None:
        async with httpx.AsyncClient() as client:
            services = dataclasses.replace(
                make_services(client, FakeResolver(), VirtualClock()),
                tracing=tracing,
            )
            await Crawler(make_state(), services, Replies(replies), tools).run()

    asyncio.run(go())
    tracing.shutdown()
    return exporter


def statuses(exporter: InMemorySpanExporter, prefix: str) -> list[str]:
    return [
        s.status.status_code.name
        for s in exporter.get_finished_spans()
        if s.name.startswith(prefix)
    ]


def test_a_failed_tool_call_is_an_error_span_and_a_skip_is_not() -> None:
    failed = traced_run([call_reply()], ToolErrorCode.TIMEOUT)
    assert statuses(failed, "tool.") == ["ERROR"]
    skipped = traced_run([call_reply()], ToolErrorCode.SKIPPED_DEAD_HOST)
    assert statuses(skipped, "tool.") == ["UNSET"]


def test_the_agent_step_is_a_generation_and_the_root_has_a_session() -> None:
    exporter = traced_run([call_reply()], ToolErrorCode.SKIPPED_DEAD_HOST)
    steps = [s for s in exporter.get_finished_spans() if s.name == "agent.step"]
    assert steps and all(
        (s.attributes or {})["langfuse.observation.type"] == "generation" for s in steps
    )
    root = attrs(exporter, "crawler.run")
    assert root["langfuse.session.id"] == RUN_ID


def test_a_refusal_marks_the_agent_step_as_an_error() -> None:
    refusal = AIMessage(
        content="",
        response_metadata={"stop_reason": "refusal"},
        usage_metadata=_usage(),
    )
    exporter = traced_run([refusal], None)
    assert statuses(exporter, "agent.step")[0] == "ERROR"


# --- export failures never fail the run ----------------------------------------------


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_an_unreachable_endpoint_is_bounded_and_reported(tmp_path: Path) -> None:
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{closed_port()}"}
    tracing = open_trace(RUN_ID, tmp_path, env)
    for n in range(20):
        with tracing.span("tool.probe_url", n=n):
            pass
    started = time.monotonic()
    tracing.shutdown()
    assert time.monotonic() - started < 15
    status = tracing.export_status()
    assert status.endpoint_set and status.spans_failed > 0 and status.error
    assert len((tmp_path / "trace.jsonl").read_text().splitlines()) == 20


def test_an_unimportable_exporter_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise ImportError("no exporter")

    monkeypatch.setattr(tracing_module, "_make_otlp_exporter", missing)
    tracing = Tracing(
        RUN_ID,
        io.StringIO(),
        env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"},
    )
    with tracing.span("agent.step"):
        pass
    tracing.shutdown()
    assert tracing.export_status().error == "ImportError"


def test_without_an_endpoint_export_is_off() -> None:
    tracing = Tracing(RUN_ID, io.StringIO(), env={})
    tracing.shutdown()
    status = tracing.export_status()
    assert (status.mode, status.endpoint_set, status.error) == ("off", False, None)
