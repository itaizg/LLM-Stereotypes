# Contract: run trigger HTTP API

Served by `python -m crawler trigger [--port 8765]`, bound to `127.0.0.1` (R1). This contract doesn't depend on where the crawler runs. A later Kubernetes launcher implements the same requests and responses ([todo.md](../todo.md)).

**Auth (FR-001b):** every endpoint except `/healthz` needs `Authorization: Bearer <CRAWLER_TRIGGER_TOKEN>`, compared with `hmac.compare_digest`. A missing or wrong token gets `401`, with no body detail. If the token env var is unset or shorter than 32 characters, the server refuses to start (exit 2).

**Environment:** whatever `crawler run` needs (inherited by the child process), plus `CRAWLER_TRIGGER_TOKEN`, `CRAWLER_MAX_RUNTIME_S` (default 10800) and `CRAWLER_RESULTS_DIR` (optional).

## `GET /healthz`

`200 {"ok": true}`. No auth. It reveals nothing beyond liveness.

## `POST /runs`

Body (optional): `{"config": "<path relative to repo>"}`. No other fields; unknown fields get `422`.

1. Creates `run_id` and starts `python -m crawler run --approval standing --run-id <id> [--config …]` in a new process group. Writes `trigger.json`.
2. Waits up to 10 s for an early exit (approval and the busy lock are checked before anything is spent).

| Result | Response |
|---|---|
| still running after 10 s | `202 {"run_id", "status": "running", "deadline_at"}` |
| exit 3 | `403 {"run_id", "outcome": "not_approved", "reason"}` (the reason says which check failed: none active, expired, pricing changed, or limits above the cap) |
| exit 4 | `409 {"run_id", "outcome": "skipped", "reason": "run_in_progress"}` |
| exit 2 | `422 {"run_id", "outcome": "failed", "reason"}` (names a missing env var, never its value) |
| exit 0/1/5/6 within 10 s | `200` with the status body below |

## `GET /runs/{run_id}`

Built from `trigger.json` + `summary.json` + the `crawler_runs` row (R7). It holds no in-memory state.

```json
{
  "run_id": "…",
  "status": "running | finished",
  "outcome": "completed | failed | escalated | stopped_at_limit | not_approved | skipped | timed_out | null",
  "exit_code": 0,
  "started_at": "…", "ended_at": "…", "deadline_at": "…",
  "summary_path": "results/crawler/<run_id>/summary.json",
  "cost_usd": 0.0,
  "trace_export": {"mode": "full", "spans_failed": 0, "error": null}
}
```

An unknown `run_id` gets `404`. The exit code → outcome table is in [data-model.md](../data-model.md).

## Deadline (FR-006)

At `deadline_at`, the trigger sends SIGTERM to the process group, then SIGKILL 30 s later. It sets `outcome: "timed_out"` in `trigger.json`, whatever the exit code.

## Not in the contract

There's no cancel endpoint, no run listing and no config upload. They aren't needed now (YAGNI); `crawler status` covers listing.
