"""Tracing: every agent step and tool call is a span carrying the run's ``run_id``.

The constitution requires the trace's ``run_id`` to match the hand-off tickets'
(FR-032).
Spans go to ``results/crawler/<run_id>/trace.jsonl`` (local, gitignored, research R10).
If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, spans are also exported there over OTLP/HTTP
(self-hosted Langfuse, spec 004). Export is batched and bounded: an unreachable
endpoint costs at most the shutdown flush timeout, never the run (FR-011), and the
failure is counted and reported, never silent (FR-012).

Spans carry counts, enums, URLs and hashes (FR-020). Prompt and response text is the one
exception, and only on the OTLP path in ``full`` mode (``CONTENT_KEYS``): it never goes
to ``trace.jsonl`` or the console (spec 004 FR-010a/b). Secret values from the
environment are redacted from every attribute and status message, as
``pipeline.store.redact`` does for the DSN.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Literal
from urllib.parse import unquote

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span as ApiSpan
from opentelemetry.trace import Status, StatusCode

SECRET_ENV = (
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XERJ_TOKEN",
    "CRAWLER_TRIGGER_TOKEN",
    "OTEL_EXPORTER_OTLP_HEADERS",
)
# Header lists hold secrets in their values (``Authorization=Basic <b64>``), and a value
# may be logged on its own, so each value and its last word are redacted too (spec 004).
HEADER_ENV = ("OTEL_EXPORTER_OTLP_HEADERS",)

AttrValue = str | bool | int | float

# Prompt and response text. Only the OTLP exporter may carry these (FR-010b).
OBS_INPUT = "langfuse.observation.input"
OBS_OUTPUT = "langfuse.observation.output"
CONTENT_KEYS = frozenset({OBS_INPUT, OBS_OUTPUT})
CONTENT_MAX_CHARS = 64 * 1024
TRUNCATED = "…[truncated]"
EXPORT_TIMEOUT_MS = 5_000
EXPORT_QUEUE_SIZE = 2_048
ContentMode = Literal["full", "metadata"]


def _header_parts(headers: str) -> list[str]:
    parts: list[str] = []
    for item in headers.split(","):
        _, _, value = item.partition("=")
        value = value.strip()
        parts += [value, value.rsplit(" ", 1)[-1]]
    return parts


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    found = [env.get(k, "") for k in SECRET_ENV]
    for key in HEADER_ENV:
        found += _header_parts(env.get(key, ""))
    # Longest first, so a whole value is replaced before a part of it.
    unique = {v for v in found if len(v) >= 4}
    return tuple(sorted(unique, key=len, reverse=True))


def redact_secrets(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


class _RunIdProcessor(SpanProcessor):
    """Stamps ``run_id`` on every span when it starts."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        span.set_attribute("run_id", self._run_id)


class JsonlExporter(SpanExporter):
    """One JSON object per finished span, secrets redacted."""

    def __init__(self, sink: IO[str], secrets: Sequence[str]) -> None:
        self._sink = sink
        self._secrets = tuple(secrets)

    def _clean(self, value: object) -> object:
        if isinstance(value, str):
            return redact_secrets(value, self._secrets)
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            context = span.get_span_context()
            parent = span.parent
            record = {
                "name": span.name,
                "trace_id": f"{context.trace_id:032x}" if context else None,
                "span_id": f"{context.span_id:016x}" if context else None,
                "parent_id": f"{parent.span_id:016x}" if parent else None,
                "start_ns": span.start_time,
                "end_ns": span.end_time,
                "status": span.status.status_code.name,
                "status_message": self._clean(span.status.description or ""),
                "attributes": {
                    k: self._clean(v)
                    for k, v in (span.attributes or {}).items()
                    if k not in CONTENT_KEYS
                },
            }
            self._sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._sink.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._sink.flush()


@dataclass
class TraceExport:
    """What happened to the OTLP export, for ``summary.json`` (FR-012)."""

    mode: str
    endpoint_set: bool
    spans_exported: int = 0
    spans_failed: int = 0
    error: str | None = None

    def as_json(self) -> dict[str, object]:
        return asdict(self)


class CountingExporter(SpanExporter):
    """Wraps the OTLP exporter: counts spans, keeps the first error, never raises."""

    def __init__(self, inner: SpanExporter, status: TraceExport) -> None:
        self._inner = inner
        self._status = status
        self._lock = threading.Lock()

    def _failed(self, count: int, error: str) -> None:
        with self._lock:
            self._status.spans_failed += count
            self._status.error = self._status.error or error

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception as error:
            self._failed(len(spans), type(error).__name__)
            return SpanExportResult.FAILURE
        if result is SpanExportResult.SUCCESS:
            with self._lock:
                self._status.spans_exported += len(spans)
        else:
            self._failed(len(spans), "ExportFailed")
        return result

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception as error:
            self._failed(0, type(error).__name__)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _make_otlp_exporter(
    endpoint: str, headers: dict[str, str], timeout_s: int
) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    return OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=timeout_s)


def _otlp_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.split(","):
        name, sep, value = item.partition("=")
        if sep and name.strip():
            headers[name.strip()] = unquote(value.strip())
    return headers


def _content_mode(raw: str | None) -> ContentMode:
    # Anything but an explicit "full" (or unset) is the safe mode.
    return "full" if raw in (None, "", "full") else "metadata"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Tracing:
    """The run's tracer. ``span`` is the only call the rest of the crawler makes."""

    def __init__(
        self,
        run_id: str,
        sink: IO[str],
        env: Mapping[str, str] | None = None,
        otlp_exporter: SpanExporter | None = None,
    ) -> None:
        environment = os.environ if env is None else env
        self.run_id = run_id
        self.secrets = secret_values(environment)
        self.content_mode: ContentMode = _content_mode(
            environment.get("CRAWLER_TRACE_CONTENT")
        )
        endpoint = environment.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        self._status = TraceExport(
            mode=self.content_mode if endpoint or otlp_exporter else "off",
            endpoint_set=bool(endpoint),
        )
        self._sink = sink
        self._provider = TracerProvider(
            resource=Resource.create({"service.name": "crawler", "run_id": run_id})
        )
        self._provider.add_span_processor(_RunIdProcessor(run_id))
        self._provider.add_span_processor(
            SimpleSpanProcessor(JsonlExporter(sink, self.secrets))
        )
        if otlp_exporter is not None:
            self._add_otlp(otlp_exporter)
        elif endpoint:
            self._add_otlp_from_env(endpoint, environment)
        self._tracer = self._provider.get_tracer("crawler")

    def _add_otlp_from_env(self, endpoint: str, env: Mapping[str, str]) -> None:
        try:
            exporter = _make_otlp_exporter(
                endpoint.rstrip("/") + "/v1/traces",
                _otlp_headers(env.get("OTEL_EXPORTER_OTLP_HEADERS", "")),
                EXPORT_TIMEOUT_MS // 1000,
            )
        except ImportError as error:
            self._status.error = type(error).__name__
            return
        self._add_otlp(exporter)

    def _add_otlp(self, exporter: SpanExporter) -> None:
        self._provider.add_span_processor(
            BatchSpanProcessor(
                CountingExporter(exporter, self._status),
                max_queue_size=EXPORT_QUEUE_SIZE,
                export_timeout_millis=EXPORT_TIMEOUT_MS,
            )
        )

    @contextmanager
    def span(self, name: str, **attributes: AttrValue | None) -> Generator[ApiSpan]:
        clean = {k: v for k, v in attributes.items() if v is not None}
        with self._tracer.start_as_current_span(
            name,
            attributes=clean,
            record_exception=False,
            set_status_on_exception=False,
        ) as current:
            try:
                yield current
            except BaseException as error:
                current.set_status(
                    Status(
                        StatusCode.ERROR,
                        redact_secrets(type(error).__name__, self.secrets),
                    )
                )
                raise

    def redact(self, text: str) -> str:
        return redact_secrets(text, self.secrets)

    def mark_root(self, span: ApiSpan) -> None:
        """Group the run's trace under its run id in Langfuse (FR-008)."""
        span.set_attribute("langfuse.trace.name", "crawler.run")
        span.set_attribute("langfuse.session.id", self.run_id)

    def mark_failed(self, span: ApiSpan, code: str) -> None:
        """A step or tool failure: ERROR status and level, with the code only."""
        message = self.redact(code)
        span.set_status(Status(StatusCode.ERROR, message))
        span.set_attribute("langfuse.observation.level", "ERROR")
        span.set_attribute("langfuse.observation.status_message", message)

    def generation(
        self,
        span: ApiSpan,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        prompt: str,
        response: str,
    ) -> None:
        """Describe an LLM call. Text goes on the span in ``full`` mode only."""
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        span.set_attribute("gen_ai.usage.cost", cost_usd)
        span.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps({"input": input_tokens, "output": output_tokens}),
        )
        span.set_attribute(
            "langfuse.observation.cost_details", json.dumps({"total": cost_usd})
        )
        span.set_attribute("prompt_sha256", _hash(prompt))
        span.set_attribute("response_sha256", _hash(response))
        if self.content_mode == "full":
            span.set_attribute(OBS_INPUT, self._content(prompt))
            span.set_attribute(OBS_OUTPUT, self._content(response))

    def _content(self, text: str) -> str:
        clean = self.redact(text)
        if len(clean) > CONTENT_MAX_CHARS:
            clean = clean[:CONTENT_MAX_CHARS] + TRUNCATED
        return json.dumps(clean, ensure_ascii=False)

    def export_status(self) -> TraceExport:
        return self._status

    def shutdown(self) -> None:
        self._provider.shutdown()


def open_trace(
    run_id: str, out_dir: Path, env: Mapping[str, str] | None = None
) -> Tracing:
    out_dir.mkdir(parents=True, exist_ok=True)
    sink = (out_dir / "trace.jsonl").open("a", encoding="utf-8")
    return Tracing(run_id, sink, env)
