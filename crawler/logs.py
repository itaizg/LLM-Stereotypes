"""Console lines derived from spans: one structured line per finished step (spec 004).

The lines come from the same spans that go to ``trace.jsonl`` and Langfuse, so the
console and the trace cannot disagree about what happened (FR-013). A line holds the run
id, step, tool, outcome and duration, never prompt text or secret values: the processor
reads only those named fields, and the whole line is redacted again on the way out.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import IO

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from crawler.tracing import redact_secrets

TEXT, JSON = "text", "json"
CLI_STEP = "crawler.cli"
TOOL_PREFIX = "tool."


def _stamp(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        f: dict[str, object] = record.__dict__["fields"]
        line = (
            f"{f['ts']} {record.levelname:<5} run={f['run_id']} step={f['step']} "
            f"tool={f['tool']} outcome={f['outcome']} duration_ms={f['duration_ms']}"
        )
        message = f.get("msg")
        return f"{line} msg={message}" if message else line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        f: dict[str, object] = record.__dict__["fields"]
        return json.dumps(
            {
                "ts": f["ts"],
                "level": record.levelname,
                **{k: v for k, v in f.items() if k != "ts"},
            },
            ensure_ascii=False,
        )


def configure_logging(
    env: Mapping[str, str], stream: IO[str] | None = None
) -> logging.Logger:
    """A private stderr logger in the ``CRAWLER_LOG_FORMAT`` (text by default)."""
    logger = logging.Logger("crawler.console", logging.INFO)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(
        JsonFormatter() if env.get("CRAWLER_LOG_FORMAT") == JSON else TextFormatter()
    )
    logger.addHandler(handler)
    return logger


def _emit(
    logger: logging.Logger,
    secrets: Sequence[str],
    level: int,
    fields: dict[str, object],
) -> None:
    clean = {
        k: redact_secrets(v, secrets) if isinstance(v, str) else v
        for k, v in fields.items()
    }
    logger.log(level, "", extra={"fields": clean})


class LogSpanProcessor(SpanProcessor):
    """Emits one console line when a span ends."""

    def __init__(self, logger: logging.Logger, secrets: Sequence[str]) -> None:
        self._logger = logger
        self._secrets = tuple(secrets)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        failed = span.status.status_code.name == "ERROR" or "error" in attributes
        outcome = attributes.get("outcome") or ("error" if failed else "ok")
        start, end = span.start_time or 0, span.end_time or 0
        name = span.name
        _emit(
            self._logger,
            self._secrets,
            logging.ERROR if failed else logging.INFO,
            {
                "ts": _stamp(datetime.fromtimestamp(end / 1e9, UTC)),
                "run_id": str(attributes.get("run_id", "")),
                "step": name,
                "tool": name.removeprefix(TOOL_PREFIX)
                if name.startswith(TOOL_PREFIX)
                else "",
                "outcome": str(outcome),
                "duration_ms": max(0, (end - start) // 1_000_000),
            },
        )


class RunLog:
    """Run-level lines (refusals, errors, the summary) in the same format."""

    def __init__(
        self, logger: logging.Logger, run_id: str, secrets: Sequence[str] = ()
    ) -> None:
        self._logger = logger
        self._run_id = run_id
        self._secrets = tuple(secrets)

    def _line(self, level: int, message: str, outcome: str) -> None:
        _emit(
            self._logger,
            self._secrets,
            level,
            {
                "ts": _stamp(datetime.now(UTC)),
                "run_id": self._run_id,
                "step": CLI_STEP,
                "tool": "",
                "outcome": outcome,
                "duration_ms": 0,
                "msg": message,
            },
        )

    def info(self, message: str) -> None:
        self._line(logging.INFO, message, "ok")

    def error(self, message: str) -> None:
        self._line(logging.ERROR, message, "error")
