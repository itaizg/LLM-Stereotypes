# Contract: crawler CLI changes

Changes to spec 003's [CLI contract](../../003-crawler-agent/contracts/cli.md). Anything not listed here is unchanged.

## `python -m crawler approve [--config FILE] [--revoke]`

- Without `--revoke`: prints the same budget description as `run` and asks `Approve this standing budget for 30 days? [y/N]` on the TTY. On `y`, it inserts a standing approval, revokes any earlier active one, and prints `approval_id` and `expires_at`. With no TTY or any other answer, it exits 3 and writes nothing.
- With `--revoke`: revokes the active approval, if any. Exit 0.
- Needs `PIPELINE_DSN`.

## `python -m crawler run` — new options

- `--approval tty|standing` (default `tty`, today's behaviour). `standing` skips the prompt and checks the standing approval ([data-model.md](../data-model.md)) before any paid call.
- `--run-id ID`: uses the given id. It must match the `new_run_id` format and must not already exist in `crawler_runs`, or the run exits 2.

## `python -m crawler trigger [--port 8765]`

Serves the [run trigger API](trigger-api.md).

## Exit codes (changes)

| Exit | Meaning |
|---|---|
| 2 | usage error only (was: usage error **or** budget not approved) |
| 3 | **new:** budget not approved (TTY refusal, or no valid standing approval). Nothing is written to the DB; `summary.json` has `status: "not_approved"`. |
| 4 | **new:** busy. Another run holds the run lock. Nothing is spent. `summary.json` has `status: "busy"`. |

Exits 0, 1, 5 and 6 are unchanged. SIGTERM makes the run end as `failed`, with `summary.json` `"terminated": true`.

## Environment (additions)

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset (tracing export off) | Langfuse OTLP endpoint (existing variable, now with a real exporter) |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | `Authorization=Basic …` for Langfuse. A secret, redacted everywhere. |
| `CRAWLER_TRACE_CONTENT` | `full` | `full` or `metadata` (FR-010a) |
| `CRAWLER_LOG_FORMAT` | `text` | `text` or `json` (FR-014) |
| `CRAWLER_RESULTS_DIR` | `results/crawler` | run artifact root (FR-005a) |

If `OTEL_EXPORTER_OTLP_ENDPOINT` is set but the exporter can't be loaded, the run continues. The console gets an error line, and `summary.json` gets `trace_export.error` (FR-012). Today that case is silently skipped.
