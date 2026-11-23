# Implementation Plan: Crawler Agent for Exam Source Discovery

**Branch**: `crawler-agent-mission` (Spec Kit feature id `003-crawler-agent`) | **Date**:
2026-11-23 | **Spec**: [spec.md](spec.md) | **Brief**:
[docs/briefs/crawler-agent.md](../../docs/briefs/crawler-agent.md)

**Input**: Feature specification from `specs/003-crawler-agent/spec.md`

## Summary

A new optional package, `crawler/`, holds one LangGraph agent. The agent has five
LLM-callable tools: `web_search`, `probe_url`, `sample_content`, `read_terms` and
`write_proposal`. It searches the open web for exam-like multiple-choice sources and
writes labelled **proposals** with **hand-off tickets** into new tables in the existing
pipeline Postgres store.

The LLM chooses what to search and which results to look at. It never decides a gate:
- The tools enforce the check order. `sample_content` refuses a URL whose `probe_url`
  result in this run did not pass reachability, which is FR-012 enforced at the tool and
  not left to a prompt.
- `write_proposal` computes `validation_status` and the reason code from the recorded
  check results. It never takes them from the LLM (research R4).

After a run, `crawler review` renders a static HTML page with one row per content group.
The page's buttons build a decisions JSON file that the browser downloads. A new pipeline
command, `pipeline import-decisions`, then turns approvals into ledger events:
- a crawler candidate and a review;
- a new exam, when the exam is not yet in the catalog;
- an allowlisted third-party domain.

The existing ledger → store flow does the rest. The downloader (a separate feature) sees
only sources approved this way.

Request spacing moves from "read once under the write lock" to an atomic **host-slot
reservation**: one SQL statement on the existing `host_requests` table, timed by the
database clock. Concurrent crawler runs, and later the downloader, therefore space
requests to a host correctly (research R6). Network safety checks every resolved address
and every redirect hop, and connects to the address it checked (research R7).

Memory (queries tried, dead hosts, yield per query) is code-managed, in the XERJ
namespace `/_memory/agent-crawler`, behind a `CrawlerMemory` Protocol (research R9).
Tracing is OpenTelemetry spans keyed by `run_id` (research R10).

Evals come first (FR-035):
- **Offline replay** runs in CI over *synthetic* recorded pages, with injected HTTP,
  search, LLM, clock and memory fakes. Real exam pages are never committed (FR-020).
- **Live eval** is a manual run on a committed list of labelled URLs.

**Prerequisites** that block crawler code (FR-034, brief rule 5):
- spec 001 amendments to FR-006, FR-007/FR-008 and FR-012;
- constitution amendment 1.5.0 (see Constitution Check);
- a reviewed `crawler/lexicon.yaml`.

## Technical Context

**Language/Version**: Python 3.13, managed with `uv` (`uv.lock`, `uv sync --locked`).

**Primary Dependencies**: a new optional `crawler` extra (research R1, R2, R3, R10):
- `langgraph` and `langchain-anthropic` run the agent loop.
- `pydantic` v2 defines the contracts. It is already in `uv.lock` as a transitive
  dependency, and is pinned explicitly here.
- `lingua-language-detector` detects language offline, with a confidence.
- `opentelemetry-sdk` handles tracing.

It reuses the `pipeline` extra: `httpx`, `psycopg`, `beautifulsoup4` and `pdfplumber`.

Two choices add no package:
- The Tavily and Brave search APIs are called over plain `httpx`, with no vendor SDKs.
- The review page is built with stdlib `html.escape` and `string.Template`, with no
  template engine.

**Storage**: the existing pipeline Postgres store:
- **New tables:** `crawler_runs`, `crawler_proposals`, `crawler_tickets` and
  `crawler_url_status`.
- **Reused table:** `host_requests`, which becomes the shared slot limiter.
- **Committed ledger** (`pipeline/ledger.jsonl`): new event kinds `exam_added` and
  `domain_allowlisted`, a new discovery method `crawler`, and a new kind
  `review_reopened` for FR-021b.
- **XERJ** holds crawler memory.
- **Local, gitignored:** `results/crawler/<run_id>/` holds the review page, the decisions
  file, the run summary and the trace file.

**Testing**: pytest under `tests/crawler/`, using:
- `httpx.MockTransport` replaying synthetic recordings;
- a scripted fake chat model and a fake classifier;
- a virtual clock, a fake resolver and an in-memory `CrawlerMemory`;
- loopback Postgres, as `tests/pipeline` uses (`PIPELINE_TEST_DSN`).

There is no network, no keys and no GPU (Principle III). Coverage counts toward the 85%
gate.

**Target Platform**: the maintainer's macOS machine (Apple Silicon) for live runs, and
Ubuntu CI for tests.

**Project Type**: a CLI-driven agent package (`python -m crawler`) plus a pipeline CLI
addition (`pipeline import-decisions`).

**Performance Goals**: a run that reaches its first limit (normally 100 agent
iterations, each with up to 20 parallel tool calls) finishes in under 2 hours. The 5 s per-host spacing bounds throughput, not CPU. Requests
to different hosts run concurrently, with at most 8 in flight.

**Constraints**:
- **Budget:** 500 queries, $10 and 100 iterations per run, with the per-tool limits from
  the brief (FR-002, FR-004).
- **Spacing:** at least 5 s between requests to one host across processes (FR-010).
- **Sampling:** at most 2 MB, or the first 3 PDF pages (FR-017).
- **Data:** no question text in any record, ticket, trace or commit (FR-020).
- **Context:** compacted at task boundaries or above 50% (constitution).

**Scale/Scope**:
- **Per run:** at most 100 iterations × 20 tool calls = 2,000 tool calls. At about
  three calls per URL (probe, sample, write), that is a few hundred distinct URLs, from
  tens of queries. The iteration limit normally binds before the 500-query limit
  (research R5).
- **Review:** tens to low hundreds of `passed` groups per run.
- **Language:** a lexicon for at least the six exams' languages (en, he, ko, zh, ja, pt).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (below).*

| Rule | Status | How the design meets it |
|---|---|---|
| I. Typed code, ty | **Pass, with amendment** | `crawler/` is fully annotated and checked by ty. Untyped JSON (search APIs, XERJ, LLM output) is parsed into Pydantic models at the boundary. The tool contracts are `Protocol`s. **The Principle I path list must add `crawler/`** (amendment 1.5.0). |
| II. ruff | Pass | No new configuration. `crawler/` lints under the existing rule set. |
| III. Tests, GPU-free, no network | Pass | All tests inject a transport, resolver, clock, chat model, classifier and memory. Replay fixtures are synthetic. Postgres is loopback only. Every behaviour change in `pipeline/` (import-decisions, the slot reservation, the ledger kinds) ships with a failing-first test. |
| IV. KISS / YAGNI / DRY | Pass | One agent node with no subagents, so no DeepAgent. Search goes over `httpx`, not two SDKs. No template engine and no web server: the review page is static. The decisions-file schema is defined once, in `pipeline/decisions.py`, and the crawler imports it. Thresholds are defined once, in `crawler/config.py`. |
| V. Structure | Pass | Dependencies run one way: `crawler → pipeline`. `pipeline/` and `harness/` never import `crawler/`, which a guard test enforces (as `tests/pipeline/test_guard.py` does today). Each tool module has one responsibility. Fakes stand in for the real implementations (Liskov). |
| Quality gates | **Pass, with amendment** | A new CI job, `crawler`, runs pytest on `tests/crawler`, with ty covered by the pre-commit ty hook (which installs the `crawler` extra). Its coverage file `.coverage.crawler` is picked up by the `coverage gate` combine, which reads every downloaded `.coverage.*` file. `[tool.coverage.run] source` adds `crawler`. **The Quality Gates section must name the job** (amendment 1.5.0). |
| Dependencies | **Pass, with amendment** | The new optional `crawler` extra is never pulled by the core, `model`, `pipeline` or `orchestration` installs. **Project Constraints → Dependencies must name it** (amendment 1.5.0). The heavyweight-framework ban was already lifted in 1.1.0. |
| Secrets | Pass | `TAVILY_API_KEY`, `BRAVE_API_KEY`, `ANTHROPIC_API_KEY` and `XERJ_TOKEN` come from env only. They are redacted from traces and errors, using `store.redact` as the pattern. |
| Eval-driven development | Pass | Task order: the replay eval harness and labelled sets come first, then the tools, then the graph (FR-035). |
| ≥85% coverage | Pass | `crawler` is added to the coverage source in the change that adds the package. |
| Contracts (Pydantic; Pandera/Soda) | Pass | Pydantic covers every tool input and output, the proposal, the ticket, the decision and the run config. **Pandera and Soda: N/A.** There is no in-memory dataframe and no warehouse table. |
| Hand-off ticket fields | Pass | `input`, `output`, `completeness`, `owner="crawler"`, `validation_status` (`pending | passed | failed`) and `run_id` ([contracts/handoff-ticket.md](contracts/handoff-ticket.md)). |
| Units | Pass | Every quantity field is named with its unit (`size_bytes`, `latency_s`, `cost_usd`, `wait_s`), and Pydantic `Field(json_schema_extra={"unit": …})` puts the unit in the contract (research R11). |
| Compact outputs | Pass | Tickets hold references and counts. Logs go to spans. |
| No A2A in graph | Pass | One node. Hand-off is via the shared store and tickets. |
| 3–5 tools; contract and limits each; idempotent | Pass | Five tools ([contracts/tools.md](contracts/tools.md)). Read tools have no side effects. `write_proposal` is keyed by (normalised URL, revision), and a retry returns the existing ticket. |
| Memory access mode | Pass | Code-managed, as the brief states. It is not a tool. |
| Max iterations, N-failure escalation, HITL | Pass | `recursion_limit` is derived from the 100-iteration budget. Five consecutive failures of one tool stop the run with exit code 5 (escalate). The budget is approved at a TTY prompt before any paid call, and recorded. Approval of sources is a human act on the review page. The crawler does nothing irreversible. |
| Context compaction | Pass | Each search query is one task. Message history resets at each task boundary to a compact worklist summary, and is trimmed if it passes 50% of the window (research R5). |
| Memory through memory-architecture.md; memory is data | Pass | The XERJ adapter is the only memory path. Recalled entries are typed records that are never put into the prompt as instructions. Web text reaches the LLM only as quoted, length-capped tool output, never question text (research R12). |
| Tracing by run_id | Pass | Every node step and tool call is a span carrying `run_id` (research R10). |

**Gate result: PASS.** Three items need a MINOR constitution amendment (1.4.0 → 1.5.0):
add `crawler/` to Principle I's paths, the `crawler` CI job to Quality Gates, and the
`crawler` extra to Dependencies. Under Governance, an amendment that adds a gate must
land with its tooling, so the amendment, the CI job and the coverage source entry ship
in the same PR as the package skeleton. Nothing here is a violation needing
justification, so Complexity Tracking is empty.

### Re-check after Phase 1 design

The data model, contracts and quickstart add no new dependency, tool, process or
framework beyond the table above. Two design results bear on gates, and both hold:
- The import step writes only ledger events. The catalog stays hand-owned (research
  R8), so `catalog.yaml` keeps its comments and review flow.
- The decisions file is validated at the boundary (Pydantic, size cap) before any
  write, which satisfies the team pre-commit checklist on input validation.

**Still PASS.**

## Project Structure

### Documentation (this feature)

```text
specs/003-crawler-agent/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R14
├── data-model.md        # Phase 1: entities, tables, ledger events, state machines
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── tools.md             # the five tool contracts and limits
│   ├── handoff-ticket.md    # ticket and proposal output contract
│   ├── decisions-file.md    # review page → import step file format
│   └── cli.md               # crawler CLI and pipeline import-decisions
├── team-context.md      # team-discover output
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
crawler/                      # new optional package (extra: crawler); never imported by harness/ or pipeline/
├── __init__.py
├── __main__.py               # CLI: run, review, status (contracts/cli.md)
├── lexicon.yaml              # prerequisite: reviewed, per-language, 4 categories
├── config.py                 # RunConfig, Budget, thresholds (the single home of every number)
├── contracts.py              # Pydantic: tool I/O, Proposal, HandoffTicket, labels, units
├── lexicon.py                # load and validate the lexicon; keyword counting
├── netsafe.py                # resolve + address policy + pinned connect + redirect walk
├── slots.py                  # atomic host-slot reservation over host_requests
├── search.py                 # web_search: Tavily and Brave adapters, failover, query budget
├── probe.py                  # probe_url: status, final URL, type, size, robots, login/paywall/CAPTCHA
├── sample.py                 # sample_content: bounded read, MCQ structure, language, keywords, key status
├── classify.py               # site/page/topic classifier (LLM, cost-metered)
├── terms.py                  # read_terms
├── proposals.py              # write_proposal: revisioning, grouping hash, ticket, status computation
├── memory.py                 # CrawlerMemory Protocol, XERJ adapter
├── tracing.py                # OpenTelemetry setup, run_id span attributes, redaction
├── graph.py                  # LangGraph: state, agent node, tool node, budget guards, compaction
├── review.py                 # static review page renderer (content groups)
└── evals/
    ├── replay.py             # offline replay driver (CI)
    ├── live.py               # live eval driver (manual)
    └── live_set.yaml         # labelled URLs only: url, expected passed/failed, reason

pipeline/
├── decisions.py              # new: decisions-file Pydantic model + import logic
├── __main__.py               # + `import-decisions` command
├── manifests.py              # + ledger kinds exam_added, domain_allowlisted, review_reopened; method `crawler`
├── acquire.py                # catalog view = catalog.yaml ⊕ ledger additions
├── schema.sql                # + crawler_* tables; sources.discovery_method adds 'crawler'; exam_id key rule
├── migrations/003_crawler.sql # brings existing stores to schema version 3
├── acquire.py                # fetch skips crawler sources with a newer, unreviewed revision
└── store.py                  # + reserve_host_slot(), used by PostgresHostLedger too; versioned apply_schema

tests/
├── crawler/
│   ├── conftest.py           # fakes: transport, resolver, clock, chat model, classifier, memory, DB
│   ├── fixtures/replay/      # SYNTHETIC pages + search responses (no real exam text)
│   ├── test_netsafe.py  test_slots.py  test_search.py  test_probe.py
│   ├── test_sample.py   test_classify.py  test_terms.py  test_proposals.py
│   ├── test_graph.py    test_review.py  test_memory.py  test_guard.py
│   └── test_replay_eval.py   # FR-035 offline eval, asserts SC-003..SC-006, SC-009
└── pipeline/
    └── test_decisions.py     # import-decisions: stale revision, idempotency, new exam, allowlist

.github/workflows/ci.yml      # + `crawler` job (uploads `.coverage.crawler`)
pyproject.toml                # + crawler extra; coverage source + "crawler"; packages include crawler*
.specify/memory/constitution.md  # 1.5.0 amendment (paths, CI job, extra)
specs/001-exam-data-aggregation/spec.md  # FR-006, FR-007/008, FR-012 amendments (FR-034)
```

**Structure Decision**: `crawler/` is a top-level package beside `pipeline/` and
`orchestration/`, with its own optional extra and its own CI job, following how
`orchestration/` was added in spec 002. The import step belongs to `pipeline/`, because
the pipeline owns the ledger, the catalog view and the decisions it accepts. The crawler
imports the pipeline (`decisions`, `store.connect`, `web.decode_html`,
`web.extract_links`, `web.USER_AGENT`, `web.tls_context`), never the reverse.

## Complexity Tracking

No violations. The three constitution amendments above are scope updates for a new
package, not deviations, and land with the tooling that enforces them.
