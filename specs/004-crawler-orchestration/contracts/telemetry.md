# Contract: telemetry (spans, content modes, console lines)

## Span names (unchanged from spec 003)

`crawler.run` (root) → `agent.step` → `tool.<name>`, plus the classifier call inside the tools that classify. `run_id` is stamped on every span.

## Attributes added

| Span | Attribute | Value |
|---|---|---|
| `crawler.run` | `langfuse.trace.name` | `crawler.run` |
| `crawler.run` | `langfuse.session.id` | the run id |
| `agent.step`, classifier | `langfuse.observation.type` | `generation` |
| `agent.step`, classifier | `gen_ai.request.model` | e.g. `claude-opus-5-5` |
| `agent.step`, classifier | `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` | int |
| `agent.step`, classifier | `gen_ai.usage.cost` | float, USD (the same number as the existing `cost_usd`) |
| `agent.step`, classifier | `prompt_sha256` / `response_sha256` | hex; present in both modes |
| `agent.step`, classifier | `langfuse.observation.input` / `langfuse.observation.output` | **`full` mode only**: redacted text, cut to 64 KB, with `…[truncated]` appended when cut |

Also set on generations, so Langfuse needs no guessing about token and cost fields:

| Attribute | Value |
|---|---|
| `langfuse.observation.usage_details` | JSON string `{"input": n, "output": n}` |
| `langfuse.observation.cost_details` | JSON string `{"total": usd}` |

On a failed step or tool (`mark_failed`): span status ERROR, `langfuse.observation.level=ERROR` and `langfuse.observation.status_message=<error code>`.

**Checked against Langfuse's OpenTelemetry docs (T028, https://langfuse.com/integrations/native/opentelemetry):** `langfuse.session.id`, `langfuse.trace.name`, `langfuse.observation.type`, `langfuse.observation.input`/`output` (JSON strings), `langfuse.observation.level`, `langfuse.observation.status_message`, `gen_ai.request.model`, `langfuse.observation.usage_details`, `langfuse.observation.cost_details` and `gen_ai.usage.cost` are documented. The docs give `gen_ai.usage.*` only as a prefix, so `gen_ai.usage.input_tokens`/`output_tokens` are not documented by name, and the key shapes of `usage_details`/`cost_details` are not given. Both are set as a pair for that reason; quickstart D.3 confirms what the Langfuse UI shows.

**Endpoint and headers:** the exporter posts to `<OTEL_EXPORTER_OTLP_ENDPOINT>/v1/traces`. The self-hosted OTLP endpoint needs Langfuse >= 3.22.0. Langfuse's docs add `x-langfuse-ingestion-version=4` to the headers for real-time ingestion; put it in `OTEL_EXPORTER_OTLP_HEADERS` next to `Authorization=Basic …`. `CRAWLER_TRACE_CONTENT` accepts `full` or `metadata`; any other value is treated as `metadata`.

## Content routing (FR-010a, FR-010b)

`CONTENT_KEYS = {"langfuse.observation.input", "langfuse.observation.output"}`

| Sink | Gets CONTENT_KEYS? |
|---|---|
| OTLP → Langfuse | yes, in `full` mode |
| `trace.jsonl` | never |
| console lines | never |

Secrets are redacted **when the attribute is set**, so every sink gets redacted values.

## Export failure (FR-012)

- The OTLP exporter is wrapped to count exported and failed spans, and to keep the first error class name.
- At shutdown, the BatchSpanProcessor flushes with a 5 s timeout.
- The counts go into `summary.json` `trace_export`. If any span fails, one ERROR console line is printed: `trace export failed: <n> spans, <error>`.
- An export failure never changes the run's exit code.

## Console line (FR-013, FR-014)

One line per finished span, on stderr.

- Text (default):
  ```
  2026-11-25T02:00:13.412Z INFO  run=cr-20261125-020000-ab12 step=tool.probe tool=probe outcome=ok duration_ms=812
  ```
- JSON (`CRAWLER_LOG_FORMAT=json`):
  ```json
  {"ts": "…", "level": "INFO", "run_id": "…", "step": "tool.probe", "tool": "probe", "outcome": "ok", "duration_ms": 812}
  ```

Fields:
- `level` is ERROR when the span status is ERROR, or when it has an `error` attribute or a non-`ok` `outcome` that counts as a failure (not in spec 003's `NOT_FAILURES`). Otherwise it's INFO.
- `tool` is empty for non-tool spans.
- `outcome` is the span's `outcome` attribute, or `ok`/`error` from its status.
- A line never contains CONTENT_KEYS or secret values.

Run-level lines (approval result, the summary, reopened sources) go through the same logger, with `step=crawler.cli`.
