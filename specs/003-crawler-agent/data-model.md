# Data Model: Crawler Agent (Phase 1)

There are three kinds of model:
- **Pydantic models** in `crawler/contracts.py` and `pipeline/decisions.py`.
- **Postgres tables** in `pipeline/schema.sql`, which hold operational crawler state.
- **Ledger events** in `pipeline/ledger.jsonl`, which record committed decisions.

All thresholds live in `crawler/config.py`.

## Enums

| Enum | Values |
|---|---|
| `Kind` | `pdf`, `web_page` |
| `Origin` | `official`, `third_party` (proposed; confirmed on approval) |
| `SiteType` | `exam_body`, `prep`, `education`, `news`, `blog`, `forum`, `other` |
| `PageType` | `past_paper`, `practice_test`, `question_bank`, `answer_key`, `article`, `other` |
| `Topic` | `math`, `reading`, `writing`, `science`, `language`, `social_studies`, `other` |
| `KeyStatus` | `linked_file`, `inline`, `likely`, `not_found` |
| `CheckResult` | `passed`, `failed`, `not_run` |
| `ValidationStatus` | `pending`, `passed`, `failed` (constitution ticket enum) |
| `ReasonCode` (permanent) | `blocked`, `login_wall`, `paywall`, `captcha`, `non_public_address`, `bad_scheme`, `too_many_redirects`, `http_4xx`, `type_mismatch`, `script_only`, `not_mcq_structure`, `not_mcq_page_type`, `no_instruction_phrase`, `not_probed` |
| `ReasonCode` (transient) | `timeout`, `http_5xx`, `connection_error`, `too_large_unsampled` |
| `Flag` | `lexicon_missing`, `restrictive_terms`, `memory_unavailable`, `reopened_after_approval` |
| `RunStatus` | `running`, `completed`, `stopped_at_limit`, `escalated`, `failed` |
| `StopLimit` | `queries`, `cost`, `iterations` |
| `Decision` | `approve`, `reject` |

Reason codes come from one mapping in `config.py`, which classes each as transient or
permanent (FR-016a).

## Pydantic contracts (`crawler/contracts.py`)

Quantities follow research R11: each field name ends in its unit, and the schema carries
the unit.

### RunConfig and Budget

- **`run_id`**: `cr_<UTC yyyymmddThhmmss>_<4 hex>`.
- **`budget`**: `max_queries = 500`, `max_cost_usd = 10.0`, `max_iterations = 100`,
  `max_consecutive_failures = 5`.
- **`host_interval_s`**: 5.0. **`concurrency`**: 8.
- **`primary_provider`**: `tavily` or `brave`.
- **`agent_model`** and **`classifier_model`**: ids, plus `price_in_usd_per_mtok` and
  `price_out_usd_per_mtok`.
- **`classifier_max_cost_usd`**: 0.02.
- **`refresh`**: bool. It allows re-issuing queries and re-checking permanent failures
  (US4, FR-016a).
- **`exams_first`**: the six exam keys plus open gaps, read from the catalog and
  `TODO.md`.
- **`approved_by`** and **`approved_at`**: set by the CLI after the TTY confirmation
  (FR-001).

### Tool I/O

Field lists are in [contracts/tools.md](contracts/tools.md). In brief:
- **`SearchHit`**: `url`, `title` (≤120 chars), `rank`, `provider`, `query`.
- **`ProbeResult`**:
  - `url`, `final_url`, `status`, `content_type`, `size_bytes | None`, `hops`;
  - `robots_allowed`, `login_signal`, `paywall_signal`, `captcha_signal`;
  - `latency_s`, `reachable: CheckResult`, `reason: ReasonCode | None`.
- **`SampleResult`**:
  - `kind`, `sample_bytes`, `pages_read`, `downloadable: CheckResult`;
  - `item_count`, `choice_counts: list[int]`, `choices_consistent`,
    `question_mark_share`;
  - `language`, `language_confidence`;
  - `site_type`, `page_type`, `topic`, each with a confidence;
  - `keyword_counts: dict[category, int]`, `instruction_hits`;
  - `key_status`, `key_evidence`, `content_hash`, `cost_usd`, `latency_s`.
- **`TermsResult`**: `terms_url | None`, `excerpt` (≤300), `restrictive`.

### Labels (FR-019)

`origin`, `kind`, `language`, `site_type`, `page_type`, `topic` (with confidences),
`keyword_counts`, `exam_name_stated | None`, `exam_year_stated | None`, `key_status`,
`terms_url`, `terms_excerpt`, `terms_restrictive`, `flags: set[Flag]`.

### Proposal

- **Identity:** `proposal_id` is `prop_<sha256(normalised_url)[:16]>`. The URL fields
  are `url` and `normalised_url`.
- **Run and content:** `revision: int ≥ 1`, `run_id`, `content_hash | None`.
- **Labels:** `labels`.
- **Checks:** `checks: {reachable, downloadable, mcq, key_located}`, each a
  `CheckResult` with its evidence.
- **Outcome:** `validation_status`, `reason | None`, `reason_class: transient |
  permanent | None`.
- **Fingerprint:** `material_fingerprint`, the SHA-256 of `(content_hash,
  validation_status, key_status, labels without confidences)`.

**Validation rules:**
- `validation_status = passed` ⇔ `reachable`, `downloadable` and `mcq` all passed
  (FR-016).
- `reachable = failed` ⇒ the other three checks are `not_run`, and `content_hash` is
  null (FR-012).
- `mcq = passed` requires all three of:
  - `item_count ≥ 5` and `choices_consistent`, with each choice count in 3–5;
  - a `page_type` among the four exam types, with confidence ≥ 0.7;
  - `instruction_hits ≥ 1`, unless the language is `unknown` or has no lexicon. In that
    case the proposal carries the flag `lexicon_missing` (FR-014, FR-018).
- `key_located` is never `failed` (FR-015).
- No field may hold question text. Free-text fields are capped: title 120, excerpt 300,
  evidence URLs only.

### HandoffTicket

Defined in [contracts/handoff-ticket.md](contracts/handoff-ticket.md): `input`,
`output`, `completeness`, `owner = "crawler"`, `validation_status`, `run_id`.

## Postgres tables (added to `pipeline/schema.sql`)

### `crawler_runs`

| Column | Type | Notes |
|---|---|---|
| `run_id` | text PK | |
| `config` | jsonb | the `RunConfig`, with no secrets |
| `approved_by`, `approved_at` | text, timestamptz | NOT NULL (FR-001) |
| `status` | text | `RunStatus` |
| `stop_limit` | text NULL | `StopLimit`, set when the status is `stopped_at_limit` |
| `queries_used`, `iterations_used` | int | |
| `cost_usd` | numeric(8,4) | |
| `completeness` | numeric(4,3) | the share of planned tasks done |
| `memory_available` | bool | |
| `provider_switches` | jsonb | `[{at, from, to, reason}]` (FR-007a) |
| `started_at`, `ended_at` | timestamptz | |

### `crawler_proposals`

| Column | Type | Notes |
|---|---|---|
| `proposal_id` | text | |
| `revision` | int | PK is (`proposal_id`, `revision`) |
| `normalised_url` | text | UNIQUE (`normalised_url`, `revision`) (FR-021) |
| `url`, `domain` | text | |
| `run_id` | text | FK → `crawler_runs` |
| `content_hash` | text NULL | indexed; the content group key (FR-023a) |
| `labels`, `checks` | jsonb | |
| `validation_status`, `reason`, `reason_class` | text | |
| `material_fingerprint` | text | |
| `review_state` | text | `unreviewed`, `approved`, `rejected`, `stale_superseded` |
| `created_at` | timestamptz | |

### `crawler_tickets`

| Column | Type | Notes |
|---|---|---|
| `proposal_id`, `revision` | | PK, FK → `crawler_proposals`; written in the same transaction (FR-021) |
| `ticket` | jsonb | the validated `HandoffTicket` |
| `run_id` | text | |

### `crawler_url_status`

The FR-016a re-check policy.

| Column | Type | Notes |
|---|---|---|
| `normalised_url` | text PK | |
| `last_reason`, `last_reason_class` | text | |
| `transient_attempts` | int | at most 3 |
| `last_checked_at` | timestamptz | |

### `host_requests` (existing)

No schema change. The table gains `store.reserve_host_slot()` (research R6).

### Existing tables

- **`sources.discovery_method`:** its CHECK constraint adds `'crawler'`.
- **`questions` and `answer_keys`:** a `needs_reprocess boolean DEFAULT false` column,
  set by replaying a `review_reopened` event (FR-021b).
- **Origin labels:** questions and keys already carry their source. `third_party` origin
  is derived from the source's ledger `candidate` event (FR-029). How the dataset card
  counts are reported is spec 001's flattened view.

## Ledger events (`pipeline/manifests.py`)

| Kind | Fields | Written by |
|---|---|---|
| `candidate` (existing) | + `method: "crawler"`, `proposal_id`, `revision`, `origin`, `labels_digest`, `evidence: [url…]` | import-decisions |
| `review` (existing) | unchanged: `source_id`, `decision`, `edition_id`, `role`, `published`, `reason`; + `decision_id` | import-decisions |
| `exam_added` (new) | `exam_key`, `name`, `language`, `domain`, `decision_id`, `proposal_id`, `revision`, `evidence` | import-decisions (FR-028) |
| `domain_allowlisted` (new) | `exam_key`, `domain`, `origin`, `decision_id` | import-decisions (FR-026) |
| `review_reopened` (new) | `source_id`, `proposal_id`, `from_revision`, `to_revision`, `changed: [content|key]` | import-decisions (FR-021b) |

**Dedupe keys** make every repeat import a no-op (FR-026):
- `exam_added`: `exam_key`;
- `domain_allowlisted`: (`exam_key`, `domain`);
- `review_reopened`: (`source_id`, `to_revision`).

## Relationships

```text
crawler_runs 1 ── * crawler_proposals 1 ── 1 crawler_tickets
crawler_proposals * ── 1 content group (same content_hash; derived, not a table)
content group 1 ── 0..1 review decision (decisions file) ──> ledger events ──> sources / exams view
crawler_proposals.normalised_url 1 ── 1 crawler_url_status
```

## State transitions

**Proposal `review_state`:**

```text
unreviewed ──approve (current revision)──> approved
unreviewed ──reject──> rejected
unreviewed ──decision on old revision──> (decision rejected as stale; state unchanged)
any ──new material revision written──> previous row: stale_superseded; new row: unreviewed
approved ──new revision changes content_hash or key_status──> new row unreviewed
         + flag reopened_after_approval; import appends review_reopened (FR-021b)
```

**Run `status`:**

```text
(approved at TTY) running ──all tasks done──> completed
running ──first of queries/cost/iterations hit──> stopped_at_limit (stop_limit set; no retry)
running ──5 consecutive failures of one tool──> escalated (exit 5)
running ──unhandled error──> failed
```

**URL re-check (FR-016a)**, at planning time in a later run:
- permanent failure: skip, unless `refresh`;
- transient failure: re-check only if `last_checked_at` is at least 7 days ago and
  `transient_attempts` < 3;
- passed: skip, unless `refresh` or the content may have changed. This is the cheap
  probe-only path, which compares `size_bytes` and `ETag` and samples only on a change.

## Crawler memory (XERJ `/_memory/agent-crawler`)

| Entry type | Fields | Use |
|---|---|---|
| `query_tried` | `query`, `lang`, `provider`, `run_id`, `at` | skip on re-plan unless `refresh` (US4) |
| `query_yield` | `query`, `passed_count`, `failed_count`, `run_id` | rank query templates |
| `dead_host` | `host`, `reason`, `until` (= at + 30 days), `run_id` | skip probing until `until` (US4 scenario 1) |

## Keyword lexicon (`crawler/lexicon.yaml`)

```yaml
version: 1
languages:
  en:
    exam: [exam, test, paper, …]
    item: [question, item, …]
    instruction: ["choose the best answer", "which of the following", …]
    key: [answer key, answers, …]
  pt: { exam: [...], item: [...], instruction: [...], key: [gabarito, …] }
  # he, ko, zh, ja: required before code (FR-006)
```

A loader test checks that all four categories are non-empty for each of the six
languages.
