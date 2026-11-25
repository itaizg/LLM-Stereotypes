# Contract: the five crawler tools

These are the only LLM-callable tools (constitution: 3–5 tools). Memory is code-managed
and is not a tool. Each tool has:
- a Pydantic input and output model in `crawler/contracts.py`;
- a description, which is the text the model sees;
- limits enforced in code. Exceeding a limit returns a typed error result. It never
  raises into the graph.

Every tool call is a span carrying `run_id` (FR-032). Every outbound HTTP request goes
through `netsafe` (address and redirect policy) and `slots` (host spacing).

## Shared rules

- **Errors:** a tool that fails returns `{"error": ReasonCode, "retryable": bool}`. Five
  consecutive errors from the same tool escalate the run (FR-003). A success resets that
  tool's counter.
- **No question text:** outputs hold counts, hashes, URLs, enums, confidences, titles
  (≤120 chars) and the terms excerpt (≤300 chars) only (FR-020, research R12).
- **Idempotency:** the four read tools have no side effects beyond the run's in-memory
  check cache. `write_proposal` is idempotent per (normalised URL, revision).
- **Budget reservation:** tool calls in one iteration run concurrently, so budgets are
  reserved, not just checked. Before a call starts, it atomically reserves its
  worst-case allowance from the run state, under one `asyncio.Lock`: one query for
  `web_search`, and the worst-case cost (max tokens × price) for a classifier or agent
  call. A call that cannot reserve is refused as if the budget were spent. After the
  call, the unused part of the reservation is released. In-flight calls can therefore
  never push the run past 500 queries or $10 (FR-002).

## 1. `web_search(query: str, provider: "tavily" | "brave" | None, lang: BCP-47 | None) -> list[SearchHit]`

- **Description:** "Search the web for pages or PDFs that may contain exam-style
  multiple-choice questions. Returns ranked URLs and titles only."
- **Limits:**
  - at most 20 results per call;
  - 10 s timeout;
  - 2 retries, on timeouts and 5xx;
  - each call counts 1 toward the 500-query run budget. It is refused with
    `query_budget_exhausted` once the budget is spent.
- **Failover:** if the provider is unavailable or out of quota, the tool switches to the
  other provider for the rest of the run and records the switch (FR-007a). If both are
  down, it returns an error, which counts toward escalation.
- **Memory:** a query already in `query_tried` returns `skipped_known_query` unless the
  run has `refresh` set (US4).

## 2. `probe_url(url: str) -> ProbeResult`

- **Description:** "Check whether a URL is reachable for us: follows redirects safely,
  checks robots.txt, and looks for login, paywall or CAPTCHA signals. Reads at most
  64 KB."
- **Limits:**
  - 10 s timeout per hop;
  - 2 retries, on transient errors only;
  - at most 64 KB read;
  - at most 5 redirect hops, each re-checked.
- **Refusals, which are recorded and send no request to the target:**
  - `non_public_address`, `bad_scheme`, `too_many_redirects`;
  - `blocked` (robots.txt);
  - a dead host (memory) or a permanent failure (`crawler_url_status`), unless the run
    has `refresh` set.
- **Result:**
  - `reachable = passed` only for a 2xx final status, with robots allowing the path and
    no login, paywall or CAPTCHA signal;
  - it records the probe in run state, which unlocks `sample_content` for that URL
    (research R4);
  - for a URL that passed before, `unchanged = true` when `size_bytes` and `ETag` match
    the stored `last_size_bytes` and `last_etag` in `crawler_url_status`. The agent then
    skips sampling, and `write_proposal` takes the no-op path (data-model.md, "URL
    re-check").

## 3. `sample_content(url: str) -> SampleResult`

- **Description:** "Read a bounded sample of a reachable URL and measure whether it
  looks like a multiple-choice exam source. Returns counts, labels and hashes — never
  the text."
- **Precondition:** a passing `probe_url` for this URL in this run. Otherwise the tool
  returns `not_run_unreachable` and sends no request (FR-012).
- **Limits:**
  - at most 2 MB, or the first 3 PDF pages (FR-017);
  - 30 s timeout;
  - 1 retry;
  - classifier cost of at most $0.02 per call, checked before the call. Over the budget,
    the classifier is not called: the MCQ check is `not_run` and the reason is
    `not_classified`, a transient reason. A budget shortfall therefore never records a
    permanent failure, and a later run re-checks the URL (FR-016a).
- **Signals:** structure, question-mark share, language, site type, page type, topic,
  keyword counts and key status (research R13). The thresholds are in
  `crawler/config.py`.
- **Downloadable:**
  - **PDF:** the `%PDF-` signature and the content type agree. Otherwise the result is
    `type_mismatch`.
  - **Web page:** the items are present in the served HTML, or a linked file has the
    exam or key terms. Otherwise the result is `script_only`.

## 4. `read_terms(domain: str) -> TermsResult`

- **Description:** "Find the site's terms of use or licence page and say whether it
  restricts scraping, copying or research use."
- **Limits:** 10 s timeout, 1 retry, at most 256 KB read, and an excerpt of at most 300
  characters. The result is cached per domain per run.
- **Effect:** a source is never failed for its terms. `restrictive = true` adds the flag
  `restrictive_terms` for the reviewer.

## 5. `write_proposal(url: str, origin: Origin, exam_name_stated: str | None, exam_year_stated: int | None) -> HandoffTicket`

- **Description:** "Store the proposal for a URL you have checked. Check results are
  taken from this run's records, not from you."
- **Limits:** 5 s timeout and 3 retries. It is one transaction for the proposal and its
  ticket (FR-021).
- **Behaviour:**
  1. It assembles labels and checks from run state, and computes `validation_status`,
     `reason`, `reason_class` and `material_fingerprint` (research R4).
  2. **No prior row:** it writes revision 1. For an existing URL it first locks the
     latest row (`SELECT … FOR UPDATE`), which serialises it against other runs and
     against `pipeline import-decisions`.
  2a. **Older observation:** each proposal stores `observed_at`, the database time of
     the slot reservation for the sample request (research R6). If this observation
     is older than the latest revision's `observed_at`, it writes nothing, returns the
     existing ticket and records the note `older_observation`. A slow run therefore
     cannot supersede newer content with an older sample.
  3. **Same fingerprint as the latest revision:** it writes no proposal or ticket and
     returns the existing ticket (idempotent). Step 5 still runs.
  4. **Different fingerprint** (a material change, FR-021a): it writes revision n+1 and
     marks revision n `stale_superseded`. If revision n was `approved` and the content
     hash or key status changed, the new row is flagged `reopened_after_approval`
     (FR-021b).
  5. On every call, including the no-op path, it upserts `crawler_url_status` from the
     reason and its class (FR-016a).
- **Guard:** the stated exam name and year must appear in the sampled page's title or
  its text tokens. A guessed value is replaced with `null` and the replacement is
  recorded (brief: "never guessed").
