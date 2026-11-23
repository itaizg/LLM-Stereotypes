# Feature Specification: Crawler Agent for Exam Source Discovery

**Feature Branch**: `crawler-agent-mission`

**Created**: 2026-11-23

**Status**: Draft

**Input**: User description: "this mission brief and the task at hand". The brief is
[docs/briefs/crawler-agent.md](../../docs/briefs/crawler-agent.md): the first agent of the
acquisition graph. It searches the open web for exam-like multiple-choice sources, both
raw PDFs and web pages. It checks that each one is reachable and downloadable, labels it,
and writes catalog proposals. A human approves them on a review page, and a deterministic
downloader fetches only approved sources.

## User Scenarios & Testing *(mandatory)*

The actors are the **maintainer**, who starts runs, approves budgets and reviews
proposals, and the **downloader**, the next step, which consumes approved sources.

### User Story 1 - Discover and validate candidate sources (Priority: P1)

The maintainer starts a crawler run with an approved budget. The crawler searches the
open web for exam-like multiple-choice sources. The six catalog exams and their open gaps
are searched first, then any other exam-like source in any language. For each candidate,
the crawler checks reachability, downloadability and multiple-choice shape, and records
answer-key status. It stores each candidate as a labelled proposal with the evidence for
every check.

**Why this priority**: this is the agent's core value. Without validated, labelled
proposals there is nothing to review or download. On its own it already replaces manual
searching and manual URL checking.

**Independent Test**: run the crawler offline against recorded search and HTTP responses.
Check that the expected proposals are produced with every label and check result, that
known-bad URLs fail with the right reason, and that no request goes to a forbidden
target.

**Acceptance Scenarios**:

1. **Given** an approved run budget and recorded search results containing an official
   past-paper PDF, **When** the run completes, **Then** a proposal exists for that PDF
   with status `passed`, kind `pdf`, its detected language, page type `past_paper`, a key
   status with evidence, and the run's identifier.
2. **Given** a search result that is a news article quoting three questions as a
   numbered list, **When** it is validated, **Then** its proposal is `failed` with a
   reason code stating it does not look like a multiple-choice source.
3. **Given** a candidate whose path robots.txt disallows, **When** it is validated,
   **Then** the proposal is `failed` with reason `blocked`, checks 2–4 are recorded as
   `not_run`, and the page content is never requested.
4. **Given** a search result or a redirect that resolves to a private, loopback,
   link-local or cloud-metadata address, **When** the crawler would request it, **Then**
   the request is refused and the proposal is `failed` with a reason code naming the
   refusal.
5. **Given** a run whose search budget runs out mid-run, **When** the limit is reached,
   **Then** the run stops cleanly, reports which limit it hit and its completeness, and
   keeps the proposals written so far.

---

### User Story 2 - Review proposals and record decisions (Priority: P2)

After a run, the maintainer opens a generated review page listing every `passed`
proposal, one row per content group (identical content found at several URLs): its
labels, check results, evidence links, and flags for restrictive terms of use or a
missing keyword lexicon. For each row, the maintainer approves or rejects it, choosing
the exam and edition it belongs to. The decisions are saved, each tied to the
proposal revision it was made on.

**Why this priority**: human approval is the gate between automated discovery and
anything that downloads or enters the catalog. It turns proposals into trusted inputs.

**Independent Test**: generate the review page from a fixed set of proposals, record a
mix of approvals and rejections, and check that the saved decisions hold the right
proposal, revision, decision and exam/edition fields.

**Acceptance Scenarios**:

1. **Given** a completed run with 12 `passed` and 30 `failed` proposals, **When** the
   review page is generated, **Then** it lists exactly the 12 `passed` proposals, one row
   per content group, with their labels, check results, evidence links and flags.
2. **Given** a proposal whose site's terms restrict scraping or research use, **When** it
   is shown for review, **Then** it carries a visible restrictive-terms flag and an
   excerpt of the terms, and the maintainer may still approve or reject it.
3. **Given** a crawled page whose title contains markup or script, **When** the review
   page shows it, **Then** the text is shown as inert text and never executed.
4. **Given** the maintainer approves a proposal, **When** the decision is saved, **Then**
   it records the proposal identifier, the revision shown, the decision, and the exam and
   edition chosen.
5. **Given** an official PDF and two prep-site mirrors with identical sampled content,
   **When** the review page is generated, **Then** they appear as one row with the
   official URL as primary and the mirrors as alternates. Approving the row approves all
   three.

---

### User Story 3 - Import decisions and hand approved sources on (Priority: P3)

The maintainer imports the saved decisions. Approved proposals become reviewed sources in
the pipeline's records, keeping their labels and evidence. An approved third-party domain
joins the allowlist. Catalog entries then follow the existing review flow. Only approved
sources become available to the downloader.

**Why this priority**: it completes the hand-off. It depends on Stories 1 and 2, and until
it exists, approved proposals cannot reach the catalog or the downloader.

**Independent Test**: import a decisions file over a fixed set of proposals, including
one decision on an outdated revision. Check which sources become downloader-eligible,
that labels and evidence are preserved, and that the stale decision is rejected.

**Acceptance Scenarios**:

1. **Given** a decision approving revision 2 of a proposal whose current revision is 3,
   **When** decisions are imported, **Then** that decision is rejected as stale and the
   proposal stays unreviewed.
2. **Given** an approved proposal on a third-party domain, **When** decisions are
   imported, **Then** the source is recorded as reviewed with its labels and evidence,
   and its domain is allowlisted for that exam.
3. **Given** a proposal that is `passed` but not approved, **When** the downloader looks
   for work, **Then** that proposal is not offered to it.
4. **Given** the same decisions file imported twice, **When** the second import runs,
   **Then** nothing changes and no duplicate sources are created.

---

### User Story 4 - Learn across runs (Priority: P4)

Across runs, the crawler remembers which queries it already tried, which hosts are dead,
and how many sources each query yielded. A new run spends its budget on new ground
instead of repeating work.

**Why this priority**: it improves cost and yield over time, but each run is useful
without it.

**Independent Test**: run twice over the same recorded responses. Check that the second
run skips queries and hosts recorded by the first and does not create duplicate
proposals.

**Acceptance Scenarios**:

1. **Given** a previous run marked a host as dead, **When** a new run finds a URL on that
   host, **Then** the host is not probed again within the dead-host retention period, and
   the skip is recorded.
2. **Given** a previous run already issued a query, **When** a new run plans its queries,
   **Then** that query is not issued again unless the maintainer asks for a refresh.

---

### Edge Cases

- A search result redirects through several hosts, and one hop is private or disallowed:
  the request chain stops at that hop and the reason is recorded.
- A PDF is served with a mismatched content type, for example `text/html` with PDF bytes:
  the downloadability check fails with a mismatch reason.
- A page shows its questions only after a script runs: the downloadability check fails
  with reason `script_only`.
- A page timed out in one run: it is re-checked in a run at least 7 days later, up to 3
  times. A page rejected as not multiple choice is never re-checked without a refresh
  (FR-016a).
- The page's language cannot be detected with enough confidence, or no keyword lexicon
  exists for it: the proposal is flagged `lexicon_missing` for the reviewer. It is not
  failed on that basis.
- The same URL is found in two runs, or the proposal write is retried after a timeout:
  one proposal and one hand-off ticket exist for that URL and revision.
- Two crawler runs, or a crawler run and the downloader, target the same host at the
  same time: requests to that host stay at least 5 seconds apart across all of them.
- A tool fails five times in a row: the run stops and escalates to the maintainer instead
  of retrying.
- One search provider goes down or runs out of quota mid-run: the run switches to the
  other provider, records the switch, and continues within the same query limit
  (FR-007a).
- Retrieved web content or stored memory contains text that reads like an instruction:
  it is treated as data and never acted on.
- An answer key exists only as a separate file linked from the page: key status is
  `linked_file`, with that link as evidence.
- The maintainer approves sources for an exam that is not in the catalog: the exam is
  created from the first approved decision. A later approval for the same exam adds
  editions and does not create a second exam.
- An approved source's answer key changes after approval, for example a corrected key
  is published: a new revision is created, the source returns to review, and the
  questions it keys are marked for reprocessing (FR-021b).
- An official and a third-party key disagree for the same question: both stay recorded
  with their origin, and neither overwrites the other.

## Requirements *(mandatory)*

### Functional Requirements

**Runs and budgets**

- **FR-001**: A crawler run MUST be started manually by the maintainer, from a run
  configuration whose budget the maintainer approves at that time. The approval MUST be
  recorded with the run identifier. Scheduled or unattended runs are out of scope.
- **FR-002**: A run MUST stop at the first limit reached: 500 search queries, $10 of
  LLM spend (the agent model and the classifier together), or 100 agent iterations. It MUST report which limit it hit and its
  completeness, and MUST NOT retry automatically.
- **FR-003**: Five consecutive failures of the same tool MUST stop the run and escalate
  to the maintainer.
- **FR-004**: Every tool and memory operation MUST have its own stated timeout, retry
  count and size limit (cost limit too, for paid calls), and MUST be stopped when a limit
  is exceeded.

**Discovery**

- **FR-005**: The crawler MUST search for exam-like multiple-choice sources in any
  language. It MUST search the six catalog exams (SAT, Psychometric, CSAT, Gaokao, Common
  Test, ENEM) and their recorded catalog gaps first.
- **FR-006**: Search queries MUST be seeded from a committed, reviewed keyword lexicon
  covering at least the six exams' languages.
- **FR-007**: The crawler MUST accept both raw files (PDF) and web pages as candidates.
- **FR-007a**: If one search provider fails or runs out of quota during a run, the
  crawler MUST switch to the other provider for the rest of the run and record the
  switch. Queries on both providers MUST count toward the same query limit. If both
  providers fail, FR-003 applies.

**Network safety**

- **FR-008**: The crawler MUST refuse any request whose host resolves to a private,
  loopback, link-local, multicast or cloud-metadata address. It MUST check every redirect
  hop again, and follow at most 5 hops.
- **FR-009**: The crawler MUST honour robots.txt for its user agent. It MUST NOT bypass
  logins, paywalls or CAPTCHAs, and MUST NOT retry a source recorded as `blocked`.
- **FR-010**: Requests to the same host MUST be at least 5 seconds apart across all
  concurrent crawler runs and the downloader.

**Validation**

- **FR-011**: Each candidate MUST go through four checks in order, and every result MUST
  be recorded:
  1. **Reachable:** a successful response after redirects, with no login, paywall or
     CAPTCHA, and robots.txt allowing the path.
  2. **Downloadable.**
  3. **Looks like multiple choice.**
  4. **Answer key located.**
- **FR-012**: If the reachability check fails, checks 2–4 MUST be recorded as `not_run`,
  and the candidate's content MUST NOT be requested.
- **FR-013**: The downloadability check MUST pass only if one of these holds:
  - for a PDF, its file signature and declared content type agree;
  - for a web page, its questions are present in the served page or in a linked file,
    not only after a script runs.
- **FR-014**: The multiple-choice check MUST pass only if all three of these hold:
  - the sample has at least 5 items, each a question stem followed directly by a block
    of 3–5 choices, with the same number of choices across items;
  - the page is classified as a past paper, practice test, question bank or answer key
    with confidence of at least 0.7;
  - if a lexicon exists for the page's detected language, the sample has at least one
    exam-instruction phrase.
  A plain numbered list, or an article that only quotes questions, MUST NOT pass.
- **FR-015**: The answer-key check MUST record one status with its evidence:
  - `linked_file`: a link whose anchor text or filename matches answer-key terms;
  - `inline`: answer markers appear in the sample;
  - `likely`: the same site has an answer-key page;
  - `not_found`: none of the above.
  This check MUST NOT fail a candidate.
- **FR-016**: A candidate's validation status MUST be `passed` only when checks 1–3 pass.
  Otherwise it MUST be `failed`, with a reason code.
- **FR-016a**: Every failure reason code MUST be classed as transient (timeouts, server
  errors) or permanent (blocked, not multiple choice, script-only, non-public address,
  type mismatch). A later run MUST re-check a transiently failed URL only after 7 days,
  and at most 3 times in all. A permanently failed URL MUST NOT be re-checked unless the
  maintainer asks for a refresh.
- **FR-017**: Content sampling MUST be bounded to at most 2 MB or the first 3 PDF pages.
- **FR-018**: The crawler MUST detect each sampled page's language with a confidence. If
  confidence is below 0.8, the language MUST be `unknown`. If the language is unknown or
  has no lexicon, the proposal MUST be flagged `lexicon_missing`.

**Proposals and hand-off**

- **FR-019**: Every proposal MUST carry the following labels:
  - origin (`official` or `third_party`; proposed, to be confirmed by the maintainer);
  - kind (`pdf` or `web_page`);
  - language, site type, page type and topic, each with its confidence where classified;
  - keyword counts per lexicon category;
  - exam name and year, only as stated on the page (`null` otherwise, never guessed);
  - key status;
  - terms-of-use URL, a short excerpt and a restrictive flag;
  - revision and run identifier.
- **FR-020**: Proposals and run outputs MUST NOT contain question text, in stored
  records, hand-off tickets, traces or version control. They store counts, hashes and
  URLs instead.
- **FR-021**: A proposal and its hand-off ticket MUST be written together, keyed by
  normalised URL and revision. A retry MUST return the existing ticket, and no partial
  write may leave a proposal without its ticket.
- **FR-021a**: A proposal MUST get a new revision only on a material change: a change in
  the sampled content's hash, its validation status, its failure reason, its key status, or any label value.
  A change in classifier confidence alone MUST NOT create a revision.
- **FR-021b**: When a new revision of an already-approved source changes its sampled
  content or its key status, the source MUST go back to review. Every question and key
  already derived from it MUST be marked for reprocessing downstream. A changed answer
  key MUST always trigger reprocessing of the questions it keys.
- **FR-022**: Each hand-off ticket MUST record its input, output, completeness, owner,
  validation status and run identifier. It MUST validate against the agent's declared
  output contract. Every measured quantity in it (sizes, durations, costs) MUST carry
  its unit.
- **FR-023**: The crawler MUST NOT edit the exam catalog, approve anything, or download a
  full file.

**Review and import**

- **FR-023a**: Proposals whose sampled content has the same hash MUST form one content
  group. The group's primary URL MUST be an `official` one when there is one, otherwise
  the first found. The other URLs MUST be kept as alternates, each with its own labels
  and its own origin.
- **FR-024**: Each run MUST generate a review page with one row per content group of
  `passed` proposals. It
  MUST show the labels, check results, evidence links, and the restrictive-terms and
  `lexicon_missing` flags. All text derived from crawled content MUST be shown as inert
  text.
- **FR-025**: The maintainer MUST be able to approve or reject each content group on
  the review page, and one decision MUST cover every URL in the group. Each decision MUST record the proposal identifier, the revision shown and
  the decision. An approval MUST also record the exam and edition it belongs to, and
  the origin the maintainer confirms for each URL in the group.
- **FR-026**: An import step MUST read saved decisions and reject any decision made on a
  superseded revision. It MUST record approved proposals as reviewed sources with their
  labels and evidence, record rejected ones as rejected reviewed sources with their
  reason, and add approved third-party domains to that exam's allowlist. Every
  decision MUST be recorded in the committed ledger. A later decision on the same group
  and revision MUST supersede the earlier one. Importing the same decisions twice MUST
  change nothing.
- **FR-027**: Only proposals approved through the import step MUST be eligible for the
  downloader. A `passed` validation status alone MUST NOT make a source eligible.
- **FR-028**: Approving a source for an exam not yet in the catalog MUST add that exam
  to the catalog automatically. The new entry MUST record its name, language and
  allowlisted domain from the decision, plus the approving decision and the proposal's
  evidence as its provenance. Approval is the only way a new exam enters the catalog;
  the crawler alone never adds one.
- **FR-029**: An approved third-party source MUST be usable downstream for both
  questions and answer keys. Every question and key that comes from it MUST be labelled
  `third_party` along with its source. The dataset card MUST report, per exam, how many
  questions and keys are official and how many are third-party.

**Memory, safety and observability**

- **FR-030**: The crawler MUST remember across runs the queries tried, dead hosts and
  yield per query, in its own memory namespace. Memory MUST be read and written by the
  agent's own code at task boundaries, not through an agent tool.
- **FR-031**: Content retrieved from the web or from memory MUST be treated as data, and
  instructions found in it MUST NOT be followed.
- **FR-032**: Every agent step and tool call MUST be traced under the run identifier
  that appears on the hand-off tickets.
- **FR-033**: Review pages and decision files MUST be excluded from version control.

**Governance and quality**

- **FR-034**: Spec 001 MUST be amended before crawler code merges:
  - FR-006: admit open scope, with new exams added by approval (FR-028);
  - FR-007 and FR-008: admit labelled third-party sources and the import step (FR-026);
  - FR-012: admit labelled third-party answer keys (FR-029).
- **FR-035**: Evaluations MUST be written before agent code, in two forms:
  - an offline replay of recorded search and HTTP responses, run in CI;
  - a live evaluation on a hand-labelled URL set, run manually.
- **FR-036**: Deterministic crawler code MUST meet the project's 85% test-coverage gate.

### Key Entities

- **Crawler run**: one execution. It has an identifier, an approved budget (with
  approver and time), the limits used, completeness, and the stop reason.
- **Candidate**: a URL found by search or by following a link, before validation.
- **Proposal**: one URL the crawler found and sampled (not fully downloaded), with its
  labels, the results and evidence of each check, its validation status and reason, and
  a revision. It is unique per normalised URL and revision. The revision advances only
  on a material change (FR-021a).
- **Hand-off ticket**: the crawler's output record for one proposal. It holds input,
  output, completeness, owner, validation status and run identifier, and is unique with
  its proposal.
- **Content group**: proposals whose sampled content is identical. It has one primary
  URL (official when available) and its alternates, and is the unit of review.
- **Review decision**: the maintainer's approve or reject on one content group, made on
  the revisions shown. An approval adds the exam and edition.
- **Exam (catalog)**: as in spec 001, plus the provenance of an exam added by approval:
  the approving decision and the proposal's evidence.
- **Reviewed source**: an approved proposal imported into the pipeline's records, keeping
  its labels and evidence. Only these are eligible for the downloader.
- **Keyword lexicon**: per-language phrases in four categories: exam, item, instruction
  and answer key. It seeds queries and supports validation.
- **Host slot**: a shared reservation that spaces requests to one host by at least 5
  seconds, across all runs and the downloader.
- **Crawler memory**: queries tried, dead hosts and yield per query, kept across runs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On the hand-labelled evaluation set, at least 90% of sources the crawler
  marks `passed` are true multiple-choice sources (precision).
- **SC-002**: On the same set, the crawler finds and passes at least 80% of the known
  good sources: the recorded ENEM, Common Test and SAT sources (recall).
- **SC-003**: Zero requests, across the evaluation set and every recorded run, go to a
  robots-disallowed path, a page behind a login or paywall, or a non-public address.
- **SC-004**: 100% of proposals carry every required label and each check's evidence,
  and 100% of hand-off tickets validate against the output contract.
- **SC-005**: Re-running the crawler on the same inputs, or retrying any write, creates
  0 duplicate proposals or tickets.
- **SC-006**: No run exceeds its approved budget or any tool's limits. Every run that
  stops at a limit reports the limit and its completeness.
- **SC-007**: The maintainer can review a run of 50 `passed` content groups in under 15
  minutes using the review page alone, without opening each source separately.
- **SC-008**: 100% of decisions made on superseded revisions are rejected at import.
  0 unapproved sources reach the downloader.
- **SC-009**: Question text appears in 0 committed files, tickets or traces.

## Clarifications

### Session 2026-11-23

- Q: The maintainer approves a crawler source for an exam not yet in the catalog. What
  happens? → A: The exam is added to the catalog automatically, with the approval and
  the proposal's evidence as its provenance (FR-028).
- Q: How may an approved third-party source be used downstream? → A: For both questions
  and answer keys, labelled `third_party`, with the dataset card reporting official and
  third-party counts per exam. Spec 001 FR-012 is amended accordingly (FR-029, FR-034).
- Q: When should a crawler proposal get a new revision? → A: Only on a material change:
  the sampled content's hash, validation status, key status, or a label value; confidence
  shifts alone don't count. A changed content or key on an approved source sends it back
  to review, and a changed answer key always triggers reprocessing of the questions it
  keys (FR-021a, FR-021b).
- Q: When several URLs carry the same file, how should the crawler present them? → A:
  Group proposals with the same sampled-content hash into one review row. The official
  URL is primary, mirrors are alternates, and one decision covers the group (FR-023a,
  FR-024, FR-025).
- Q: How should crawler runs be started? → A: Manually only. The maintainer starts each
  run and approves its budget at that time; no scheduled or unattended runs (FR-001).
- Q: If one search provider fails or runs out of quota mid-run, what should the crawler
  do? → A: Switch to the other provider for the rest of the run and record the switch;
  both count toward the same 500-query limit (FR-007a).
- Q: Should later runs check again a URL that failed in an earlier run? → A: Only
  transient failures (timeouts, server errors), after 7 days and at most 3 times;
  permanent failures are never re-checked without a refresh (FR-016a).

## Assumptions

- There is one maintainer, who starts runs, approves budgets and reviews proposals. No
  multi-user roles or authentication are needed.
- The review page is a local file opened by the maintainer. Decisions are saved to a
  file the maintainer keeps next to it, and the import step reads that file.
- The downloader is a separate, deterministic step, specified in its own feature. This
  feature only defines what makes a source eligible for it and the shared host spacing
  it must use.
- The thresholds (at least 5 items, 0.7 classification confidence, 0.8 language
  confidence, 90% precision and 80% recall) were set with the brief. They can be revised
  after the first live evaluation.
- The dead-host retention period defaults to 30 days.
- Two web search providers are available behind one search capability, and their keys
  are supplied by environment.
- Exam catalog conventions from spec 001 still hold: editions, release dates and whether
  a source is official are never guessed, and are confirmed by the maintainer. The only
  change is that a new exam can now enter the catalog through an approval (FR-028).
- When an official and a third-party answer key both exist for a question, a downstream
  consumer prefers the official one. That choice is made in the dataset's flattened
  view, which spec 001 owns.
- The keyword lexicon is created and reviewed before implementation. It is a
  prerequisite, not an output, of this feature.
- The agent framework, memory layer and tracing follow the constitution's "Constraints
  for agentic work" (version 1.4.0).
