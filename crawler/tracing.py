"""Tracing: every agent step and tool call is a span carrying the run's ``run_id``.

The constitution requires the trace's ``run_id`` to match the hand-off tickets'
(FR-032).
Spans go to ``results/crawler/<run_id>/trace.jsonl`` (local, gitignored, research R10).
If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set and the OTLP exporter package is installed,
spans are also exported there; the crawler extra does not install it, so local JSONL is
the default.

Spans carry counts, enums, URLs and hashes only (FR-020). Secret values from the
environment are redacted from every attribute and status message on export, as
``pipeline.store.redact`` does for the DSN.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span as ApiSpan
from opentelemetry.trace import Status, StatusCode

SECRET_ENV = ("TAVILY_API_KEY", "BRAVE_API_KEY", "ANTHROPIC_API_KEY", "XERJ_TOKEN")

AttrValue = str | bool | int | float


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(v for k in SECRET_ENV if (v := env.get(k, "")) and len(v) >= 4)


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
                    k: self._clean(v) for k, v in (span.attributes or {}).items()
                },
            }
            self._sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._sink.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._sink.flush()


class Tracing:
    """The run's tracer. ``span`` is the only call the rest of the crawler makes."""

    def __init__(
        self,
        run_id: str,
        sink: IO[str],
        env: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if env is None else env
        self.run_id = run_id
        self.secrets = secret_values(environment)
        self._sink = sink
        self._provider = TracerProvider(
            resource=Resource.create({"service.name": "crawler", "run_id": run_id})
        )
        self._provider.add_span_processor(_RunIdProcessor(run_id))
        self._provider.add_span_processor(
            SimpleSpanProcessor(JsonlExporter(sink, self.secrets))
        )
        if environment.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            self._add_otlp()
        self._tracer = self._provider.get_tracer("crawler")

    def _add_otlp(self) -> None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # ty: ignore[unresolved-import] optional, not in the crawler extra
                OTLPSpanExporter,
            )
        except ImportError:
            return
        self._provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))

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

    def shutdown(self) -> None:
        self._provider.shutdown()


def open_trace(
    run_id: str, out_dir: Path, env: Mapping[str, str] | None = None
) -> Tracing:
    out_dir.mkdir(parents=True, exist_ok=True)
    sink = (out_dir / "trace.jsonl").open("a", encoding="utf-8")
    return Tracing(run_id, sink, env)
