# Contract: decisions file (review page → `pipeline import-decisions`)

The review page downloads `decisions-<run_id>.json`, which the maintainer keeps next to
the page under `results/crawler/<run_id>/` (gitignored, FR-033). Its model is
`pipeline.decisions.DecisionsFile`. The pipeline owns its input, and the crawler imports
the model to build the page.

```jsonc
{
  "format": "crawler-decisions/1",
  "run_id": "cr_20261123T101500_a3f9",
  "decided_at": "2026-11-24T09:12:00Z",
  "decisions": [
    {
      "decision_id": "dec_<sha256(run_id|group content_hash|revisions)[:16]>",
      "content_hash": "sha256:…",           // the content group (FR-023a)
      "members": [                           // every URL in the group, with the revision shown
        {"proposal_id": "prop_1f3c…", "revision": 1, "primary": true,  "origin": "official"},
        {"proposal_id": "prop_77ab…", "revision": 2, "primary": false, "origin": "third_party"}
      ],                                     // origin: confirmed per URL by the maintainer
      "decision": "approve",                 // approve | reject
      "exam": {"key": "enem", "new": false}, // new: true creates the exam (FR-028)
      "new_exam": null,                      // when new: {"name": "...", "body": "...", "language": "pt"}
      "edition_id": "enem_2024_d1_azul",
      "role": "paper",                       // REVIEW_ROLES, as `pipeline review`
      "published": null,                     // ISO date, required for key_erratum
      "reason": null                         // required when decision = reject
    }
  ]
}
```

## Validation (at the boundary, before any write)

- **File:** the size is at most 5 MB. JSON is parsed into the model, and unknown fields
  are rejected (`extra="forbid"`). `format` must match.
- **`approve`:** needs `edition_id`, `role`, `exam`, and `new_exam` (name, body,
  language) when `exam.new`. It follows the same role and published rules as
  `acquire.review`. Every member's current revision must be `validation_status =
  passed` and not `stale_superseded`: a hand-edited file cannot approve a failed
  proposal (FR-027). A new exam also needs a member whose `key_status` is `linked_file`
  or `inline`, because the `exams` table requires `publishes_keys`.
- **`reject`:** needs `reason`.
- **`exam.key`:** matches `^[a-z][a-z0-9_]{1,40}$`. `new` must agree with the catalog
  view: an existing key with `new: true` is an error.
- **Members:** every member's `proposal_id` exists and carries an `origin`. All
  members share `content_hash`.

## Import outcome per decision (FR-026)

| Condition | Result |
|---|---|
| Any member's `revision` ≠ its current revision | `stale`: nothing written for the whole decision (SC-008) |
| `reject` | Ledger events: `candidate` (method `crawler`) for each member not yet a candidate, then `review` with `decision: rejected` and the reason; members set to `rejected` |
| `approve` | Ledger events in order: `exam_added` (if new and not yet added), `domain_allowlisted` (the domain of each `third_party` member, if not yet allowed), `candidate` (method `crawler`, with that member's `origin`) for each member not yet a candidate, `review` with `decision: approved`; members set to `approved` |
| Exactly repeated decision | No change (idempotent): the members' latest `review` event already has the same decision, edition, role and reason |
| Changed decision on the same revision | A new `review` event; the latest review decides, as in `pipeline review`, and `review_state` follows it. `exam_added` and `domain_allowlisted` events are not withdrawn: they record catalog facts, not the source's status |

The page does not compute `decision_id`, `content_hash` or the member list: they come
from `crawler.review.decision_skeletons()` and are embedded per group (research R15).
`decision_id` names the group and revisions, not the outcome, so a changed decision
keeps its id. Idempotency is judged against the latest `review` event, not the id.

## Concurrency with crawler runs

The stale check and the write happen in one Postgres transaction that first locks the
members' latest `crawler_proposals` rows (`SELECT … FOR UPDATE`). `write_proposal`
takes the same row lock before it writes revision n+1. So either the import commits
first, and the crawler's new revision then supersedes an `approved` row and is flagged
`reopened_after_approval`, or the crawler commits first, and the import sees the new
revision and reports `stale`. A decision never approves a revision that is no longer
current. The ledger append happens inside that transaction, after the check.

## Atomic commit per decision

A decision's ledger events are written as one block: all lines are built first, then
appended with a single `write` and `fsync`. The loader ignores a torn final line, as it
does today. Each event carries its `decision_id`, and the ledger fold applies a
decision's `exam_added`, `domain_allowlisted` and `candidate` events only when the
same `decision_id` has its closing `review` event. A crash mid-decision therefore
exposes nothing: the catalog view, allowlist and sources are unchanged. Re-running the
import completes the decision, because every event kind has a dedupe key: the ones in
[data-model.md](../data-model.md), plus (`proposal_id`, `revision`) for `candidate`.
The store is updated only after the append and is reconciled from the ledger, as other
`pipeline` stages are.

## Reopen sweep (FR-021b)

Every run of `pipeline import-decisions` first appends a `review_reopened` event for
each proposal flagged `reopened_after_approval` whose (`source_id`, `to_revision`) is
not yet in the ledger. The ledger fold (`pipeline/manifests.py`) treats
`review_reopened` as resetting the source to `pending` and clearing its approvals, so
the source leaves the downloader's eligible set until a new decision is imported. (The
fold today reads any non-`approved` review as a rejection, so a `pending` review event
would be wrong.) It does this whatever the file holds.

The sweep is not the only guard. Between the crawler writing a reopening revision and
the next import, the downloader must not fetch the changed file: for a source whose
`candidate` method is `crawler`, `pipeline fetch` also checks `crawler_proposals` and
skips the source when a revision newer than the approved one exists, reporting
`skipped_reopened`. So suspension takes effect as soon as the revision is committed. `pipeline import-decisions --reopen-only` runs the sweep with no file.
`crawler run` prints how many sources it reopened and that command.

The command prints one line per decision (`decision_id outcome`) and a summary. Exit
codes follow the pipeline's: 0 ok; 2 usage or validation error; 3 integrity refusal;
4 lock held.
