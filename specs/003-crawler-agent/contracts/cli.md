# Contract: command line

## `python -m crawler run [--config FILE] [--refresh] [--provider tavily|brave]`

1. Loads the `RunConfig` (defaults from `crawler/config.py`, then FILE overrides), the
   catalog view and the lexicon. A lexicon that does not validate is a usage error.
2. **Budget approval (FR-001, HITL):** the command prints the budget (queries, USD,
   iterations), the models and prices, and the exams searched first. It then asks
   `Approve this budget? [y/N]` on the TTY. Without a TTY, or on any answer but `y`, it
   exits 2 and writes nothing. On approval, it records `approved_by` (`$USER`) and
   `approved_at` in `crawler_runs`.
3. Runs the graph. Progress goes to stderr: one line per task.
4. Writes `results/crawler/<run_id>/summary.json`, with status, stop limit, counts,
   `cost_usd` and completeness, and `trace.jsonl`.

**Environment:**
- `PIPELINE_DSN` (as the pipeline);
- the key of the primary provider, at least one of `TAVILY_API_KEY` and
  `BRAVE_API_KEY`;
- `ANTHROPIC_API_KEY`;
- optional: `XERJ_URL` and `XERJ_TOKEN`, and `OTEL_EXPORTER_OTLP_ENDPOINT`.

Missing required keys give exit 2 and name the variable, never its value.

| Exit | Meaning |
|---|---|
| 0 | completed |
| 1 | failed (unexpected error) |
| 2 | usage error, or budget not approved |
| 5 | escalated: five consecutive failures of one tool (FR-003) |
| 6 | stopped at a limit (FR-002); partial completeness, not retried |

## `python -m crawler review RUN_ID [--out PATH]`

Renders `results/crawler/<run_id>/review.html`, with one row per content group of
`passed` proposals from that run (FR-024). Read-only on the store. Exit 0, or exit 2 for
an unknown run.

## `python -m crawler status [RUN_ID]`

Read-only. With no argument, lists recent runs. With a run, prints its summary and the
counts by status and reason.

## `python -m crawler.evals.live [--set crawler/evals/live_set.yaml]`

A manual live eval (FR-035). It runs the probe and sample tools over the labelled URLs,
using the same approval prompt and budget rules, then writes
`results/crawler/evals/<run_id>.json` with precision and recall (SC-001, SC-002).

## `pipeline import-decisions FILE` (new pipeline command)

Behaviour and exit codes are in [decisions-file.md](decisions-file.md). It holds the
pipeline write lock, appends ledger events, and then reconciles the store as other
`pipeline` stages do.
