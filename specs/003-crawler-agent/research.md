# Research: Crawler Agent (Phase 0)

Every Technical Context unknown is resolved below. Format: Decision / Rationale /
Alternatives. Vendor API shapes and package versions are confirmed against current docs
when the task that adds them runs, and exact versions are pinned in `uv.lock` then.

## R1. Agent runtime: one LangGraph node, not a DeepAgent

- **Decision:** a LangGraph `StateGraph` with two nodes, `agent` (the LLM call) and
  `tools` (runs the five tools). A conditional edge loops between them until the agent
  stops or a budget guard ends the run. The graph's state is the run's compact worklist,
  not the transcript.
- **Rationale:** the brief names LangGraph. The constitution's DeepAgent trigger
  (2+ subagents and more than 4 steps, or context near 80%) is not met: there are no
  subagents, and compaction per task (R5) keeps context small. A hand-built graph keeps
  the budget guards and the gate rules as visible code, where a prebuilt ReAct helper
  would hide them.
- **Alternatives:**
  - `create_react_agent`: less code, but budget checks would end up in hooks.
  - DeepAgent: unjustified at this size; revisit only if the trigger is measured.
  - A plain loop without LangGraph: simpler, but it departs from the acquisition-graph
    design the brief sets.

## R2. LLM access and cost metering

- **Decision:** `langchain-anthropic` for the agent model. The site, page and topic
  classifier is a separate, cheaper Claude model called with structured output (a
  Pydantic schema). Model ids and per-token prices live in the run config
  (`crawler/config.py` defaults, overridable). Cost is computed from each response's
  reported token usage times those prices, accumulated in the run state, and checked
  **before** each paid call: the call is refused if the worst-case cost (max tokens)
  would pass $10, or $0.02 for one classifier call.
- **Rationale:** native tool use and usage reporting make metering exact. Keeping prices
  in config avoids hard-coding numbers that change. The check-before-call rule makes the
  $10 limit hard, not best-effort.
- **Alternatives:** the OpenAI-compatible endpoint, as `catalog.yaml`'s `llm` block uses
  for translation. Rejected for the agent, because tool-calling support there is a
  compatibility layer. It may still be reused for the classifier if that proves simpler.

## R3. Search: two providers over plain `httpx`, with failover

- **Decision:** `search.py` has a `SearchProvider` Protocol and two adapters:
  - **Tavily:** `POST /search`.
  - **Brave:** `GET /res/v1/web/search` with the `X-Subscription-Token` header.

  `web_search(query, provider, lang)` uses the configured primary. On a 5xx response, a
  timeout after retries, or a quota error (429 or 432 with exhaustion), it marks the
  provider down for the rest of the run, records a `provider_switch` event and continues
  on the other provider (FR-007a). Both providers count toward one shared query budget,
  and a query is counted when it is sent. Results are normalised to `(url, title,
  snippet_len, rank)`, and the snippet text is dropped (R12).
- **Rationale:** two thin REST calls do not justify two SDKs (KISS).
- **Alternatives:** the vendor SDKs (more dependencies, no gain); a metasearch layer
  (YAGNI).

## R4. Gates live in code, not in the prompt

- **Decision:**
  1. `probe_url` records its result in run state, keyed by normalised URL.
  2. `sample_content` refuses (`not_run_unreachable`) any URL without a passing probe in
     this run.
  3. `read_terms` needs no gate.
  4. `write_proposal` takes the URL and the LLM's proposed labels (origin, exam name and
     year as stated). It takes the check results **from run state**, never from the LLM,
     and computes `validation_status`, the reason code, the transient or permanent class
     (FR-016a) and the material fingerprint itself.

  A proposal for a URL that was never probed is stored as `failed`, with checks 2–4
  `not_run`.
- **Rationale:** FR-011, FR-012 and FR-016 are safety and correctness rules. An LLM that
  skips or misreports a check must not be able to produce a `passed` proposal, so the
  replay eval can assert the gates even with an adversarial fake model.
- **Alternatives:** a fixed deterministic pipeline in which the LLM only plans queries.
  Rejected, because the brief defines five LLM-callable tools and wants the agent to
  choose which results to spend probes on. That choice is where the LLM adds value.

## R5. Iterations, compaction and termination

- **Decision:**
  - **Iterations:** one iteration is one `agent` node call. The graph's
    `recursion_limit` is set to `2 × 100 + 1`, and an explicit counter ends the run
    cleanly at 100, so the run never hits the `GraphRecursionError` path.
  - **Compaction:** a task is one search query and the work on its results. At each task
    boundary, the message list is replaced by a system message plus a compact summary
    (worklist counts, budget left, the last N proposals' ids and statuses).
  - **50% rule:** if token usage passes 50% of the model window inside a task, the
    oldest tool messages are trimmed to one-line summaries.
  - **Before compaction:** task results are already persisted (the proposals in
    Postgres, memory updates in XERJ), so nothing is lost.
  - **Termination:** five consecutive failures of the same tool raise `Escalation`. The
    run is recorded as `escalated`, and the CLI exits 5.
- **Rationale:** these meet the constitution's loop, compaction and escalation
  constraints with plain code paths that tests can drive.
- **Alternatives:** LangGraph checkpointing plus summarisation middleware. That is more
  machinery than one node needs, and there is no resume requirement, because a stopped
  run reports partial completeness and is not retried.

## R6. Shared host-slot limiter: atomic reservation on `host_requests`

- **Decision:** `store.reserve_host_slot(conn, host, interval_s) -> wait_s`, as one
  statement:

  ```sql
  INSERT INTO host_requests (host, last_request_at) VALUES (%s, clock_timestamp())
  ON CONFLICT (host) DO UPDATE SET last_request_at =
    GREATEST(clock_timestamp(), host_requests.last_request_at + make_interval(secs => %s))
  RETURNING extract(epoch FROM last_request_at - clock_timestamp())
  ```

  The row lock serialises reservations, and the database clock is the one clock for
  every process. The caller sleeps `max(0, wait_s)` and then sends. Every request goes
  through it: probe, robots.txt, sample, terms and each redirect hop. Search API calls
  are exempt, because they have their own rate limits.
- **Rationale:** FR-010 asks for spacing across *concurrent* runs and the downloader.
  Today's `PostgresHostLedger` reads once and is safe only under the pipeline write lock,
  which the crawler does not hold (it must not block `pipeline` commands for hours).
  Reusing the table lets `pipeline fetch` join the same limiter by switching to
  `reserve_host_slot`. That switch is listed as a follow-up for the downloader feature,
  and until then the crawler and `pipeline fetch` are not run at the same time.
- **Alternatives:**
  - An advisory lock per host: holds a connection while sleeping.
  - Redis: new infrastructure.
  - A per-process limiter: violates FR-010.

## R7. Network safety (FR-008)

- **Decision:** `netsafe.py` does the following for every hop:
  1. Resolve with `getaddrinfo`.
  2. Reject the request if **any** returned address is private, loopback, link-local,
     multicast, reserved, unspecified, in CGNAT space (`100.64.0.0/10`), IPv4-mapped to
     one of those, or a known metadata address (`169.254.169.254`, `fd00:ec2::254`).
  3. Connect to the **checked address**, with `Host` and TLS SNI set to the hostname
     (`httpx` request extension `sni_hostname`). This closes DNS-rebinding gaps.
  4. Follow redirects manually: at most 5 hops, each re-run through steps 1–3 and
     through robots.txt and the slot limiter.

  Only `http` and `https` are allowed, on ports 80 and 443. Reason codes:
  `non_public_address`, `bad_scheme`, `too_many_redirects`.
- **Rationale:** SC-003 requires zero such requests. Checking the hostname alone is not
  enough when DNS returns a private address.
- **Alternatives:** an egress proxy (infrastructure); trusting `httpx`'s redirect
  following (no per-hop check).

## R8. Import step: ledger events, the catalog stays hand-owned

- **Decision:** `pipeline import-decisions FILE` validates the file
  ([contracts/decisions-file.md](contracts/decisions-file.md)), then, under the pipeline
  write lock, for each decision:
  1. **Stale check:** reject it as `stale` if its `revision` is not the proposal's
     current revision.
  2. **Rejection:** record it on `crawler_proposals` only. Nothing goes to the ledger.
  3. **Approval:** for each URL in the content group, append to `pipeline/ledger.jsonl`:
     - a `candidate` event (method `crawler`, carrying `proposal_id`, `revision`, the
       labels and the evidence);
     - when the exam is new, an `exam_added` event (name, language, domain, provenance:
       decision id and proposal evidence; FR-028);
     - when the domain is third-party and not yet allowlisted for that exam, a
       `domain_allowlisted` event;
     - a `review` event with `edition_id` and `role`.

  Catalog loading becomes `catalog.yaml` ⊕ the ledger's `exam_added` and
  `domain_allowlisted` events. Repeating an import appends nothing, using the existing
  `review` repeat rule and event-level dedupe keys.

  **FR-021b:** when a newer material revision changes the content hash or key status of
  a URL that has an approved `review`, the import step appends `review_reopened`. That
  returns the source to `pending`, and every question and key from it is flagged
  `reprocess` in the store when the ledger is replayed.
- **Rationale:** the ledger is already the append-only, committed decision log with
  provenance and idempotency. Machine edits to `catalog.yaml` would destroy its comments
  and blur who decided what.
- **Alternatives:**
  - Rewrite `catalog.yaml` with a YAML round-trip library: lossy, and a new dependency.
  - An overlay file `catalog.approved.yaml`: a second source of truth.
- **Note:** an approval needs `role` (paper or key, …) as well as exam and edition,
  because the existing `review` requires it. The decisions file carries it.

## R9. Memory: XERJ behind a Protocol, code-managed

- **Decision:** `CrawlerMemory` Protocol: `recall_queries()`, `recall_dead_hosts(now)`,
  `remember(entries)`. The `XerjMemory` adapter calls XERJ's REST API for namespace
  `/_memory/agent-crawler`, with a 5 s timeout, 2 retries and at most 20 results per
  call, and `XERJ_URL` and `XERJ_TOKEN` from the environment.
  - **Reads:** at run start.
  - **Writes:** at each task boundary.
  - **Entries:** typed records (`query_tried`, `dead_host` with `until`, `query_yield`),
    validated with Pydantic when read.

  If XERJ is unreachable at start, the run proceeds **without memory**. It records
  `memory: unavailable` on the run and on its tickets' completeness notes, because memory
  is an optimisation (US4, P4). The run never writes memory anywhere else.

  FR-016a's re-check policy lives in Postgres (`crawler_url_status`), not in memory,
  because it is a correctness rule.
- **Rationale:** the constitution routes all memory through the memory layer, and the
  brief fixes the code-managed mode. XERJ is not installed on this machine, so the
  endpoint paths are confirmed in the task that adds the adapter. The Protocol keeps the
  rest of the design independent of them.
- **Alternatives:** a local JSON file (private memory, forbidden); a memory tool (needs a
  sixth tool, and the brief says not to).

## R10. Tracing: OpenTelemetry, local by default

- **Decision:** `opentelemetry-sdk`. Each run creates a root span (`crawler.run`) with
  attribute `run_id`. Each graph step and tool call is a child span carrying `run_id`,
  the tool name, latency (`latency_s`), outcome and reason code. The default exporter
  writes JSONL to `results/crawler/<run_id>/trace.jsonl`. If
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set, an OTLP exporter is added (for example a
  self-hosted Langfuse). A span processor drops attributes not on an allowlist, so page
  text and secrets cannot leak into traces (FR-020, secrets rule).
- **Rationale:** vendor-neutral, works offline, and testable with the in-memory exporter.
- **Alternatives:** LangSmith (hosted, vendor-specific); Langfuse SDK only (ties tracing
  to one backend; it is still reachable via OTLP).

## R11. Units in contracts

- **Decision:** every quantity field is named `<measure>_<unit>` (`size_bytes`,
  `latency_s`, `wait_s`, `cost_usd`, `sample_bytes`) and declared with
  `Field(json_schema_extra={"unit": "<unit>"})`, so the exported JSON Schema carries the
  unit. A contract test asserts that every numeric field in the contracts declares a
  unit, except the dimensionless ones (counts, confidences, shares), which declare
  `"unit": "1"`.
- **Rationale:** this is the constitution's "explicit unit field" option without a
  `pint` dependency. The test makes it enforceable.
- **Alternatives:** `pint`-typed fields (a heavier dependency, awkward in JSON).

## R12. Untrusted content and the LLM

- **Decision:** tools return structured fields only:
  - counts, hashes, URLs and enums;
  - titles truncated to 120 characters;
  - the terms excerpt (at most 300 characters).

  Search snippets and page text never reach the agent model. The classifier receives at
  most 4,000 characters of de-tagged sample text, wrapped in a delimiter, under a system
  prompt that says it is data to classify. Its output is schema-constrained enums with
  confidences, so injected text cannot steer anything beyond one label. That label can
  in turn only make a source *reviewable*, never approved.
- **Rationale:** FR-031 and FR-020. Keeping question text out of the agent's context
  also keeps it out of traces.
- **Alternatives:** an LLM-based injection detector (more cost, weaker guarantee than
  not passing the text at all).

## R13. Deterministic signals: MCQ structure, language, key status, content hash

- **Decision:**
  - **Structure:** after de-tagging (HTML) or pdfplumber text of the first 3 pages, a
    regex-and-scan detector finds stems followed directly by 3–5 choice lines. It covers
    these markers:
    - `A)` / `(A)` / `A.`;
    - `①–⑤`;
    - `ㄱ`-style Korean markers;
    - full-width letters;
    - Hebrew `א.`–`ה.`.

    It reports the item count, the choice counts and their consistency.
  - **Question marks:** the share of stems ending in `?` or `？`.
  - **Language:** `lingua` with confidence; below 0.8 the language is `unknown`.
  - **Keywords:** lexicon hits per category, matched case-folded and NFKC-normalised.
  - **Key status:** checked in this order:
    1. `linked_file` from anchor text or filename hits;
    2. `inline` from answer-marker patterns;
    3. `likely` when the run has already seen an `answer_key` page on the same site;
    4. `not_found` otherwise.
  - **Content hash:** SHA-256 of the NFKC-normalised, whitespace-collapsed sample text,
    so a mirror that serves the same PDF bytes or the same text groups with the
    original (FR-023a).
- **Rationale:** these are cheap, testable checks on synthetic fixtures. Only site type,
  page type and topic need the LLM.
- **Alternatives:** an LLM judging MCQ shape (costly; the brief requires structural
  evidence).

## R14. Evals

- **Decision:**
  - **Offline replay (CI):** `tests/crawler/fixtures/replay/` holds *synthetic* pages and
    PDFs written for the eval. Each one mimics a case:
    - an official past paper;
    - a prep-site mirror;
    - a numbered-list article;
    - a news piece quoting questions;
    - a forum thread;
    - a script-only page;
    - a mismatched PDF;
    - a robots-disallowed path;
    - a private-address redirect;
    - a login wall;
    - an injection-bearing page.

    It also holds recorded-shape search responses. A scripted fake chat model drives the
    graph, including an adversarial script that tries to sample before probing and to
    claim `passed`. Assertions cover SC-003 to SC-006 and SC-009, plus the expected
    status and reason for every fixture.
  - **Live eval (manual):** `crawler/evals/live_set.yaml` lists URLs only, each with its
    expected outcome. Positives are the ledger's known ENEM, Common Test and SAT sources;
    negatives are hand-picked known-bad URLs. `python -m crawler.evals.live` computes
    precision and recall (SC-001, SC-002) over a real run and writes the summary to
    `results/crawler/evals/`.
- **Rationale:** FR-035, and copyright: real exam text is never committed.
- **Alternatives:** VCR-style cassettes of real sites (they would commit third-party
  content, including question text).

## R15. Review page and decisions file

- **Decision:**
  - **The page:** `crawler review RUN_ID` writes a single self-contained HTML file. Every
    crawled string goes through `html.escape`. The embedded JSON data uses
    `json.dumps(...).replace("<", "\\u003c")`, and a strict CSP meta tag blocks remote
    loads and inline handlers (`default-src 'none'; script-src 'sha256-…'; style-src
    'sha256-…'`).
  - **Rows:** one row per content group, with filters by label and flag, and
    approve/reject controls (exam, edition, role and reason fields).
  - **Saving:** "Save decisions" creates a Blob and downloads `decisions-<run_id>.json`.
    The maintainer runs `pipeline import-decisions` on it.
- **Rationale:** a local file needs no server or auth (single maintainer). The team
  checklist's XSS rule is met by escaping, and CSP is defence in depth.
- **Alternatives:** a local web server writing the file directly (more code, an open
  port); Jinja2 (a dependency for one template).
