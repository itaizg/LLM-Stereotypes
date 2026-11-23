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
        {"proposal_id": "prop_1f3c…", "revision": 1, "primary": true},
        {"proposal_id": "prop_77ab…", "revision": 2, "primary": false}
      ],
      "decision": "approve",                 // approve | reject
      "origin": "official",                  // confirmed by the maintainer
      "exam": {"key": "enem", "new": false}, // new: true creates the exam (FR-028)
      "new_exam": null,                      // when new: {"name": "...", "language": "pt"}
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
- **`approve`:** needs `edition_id`, `role`, `exam`, and `new_exam` when `exam.new`. It
  follows the same role and published rules as `acquire.review`.
- **`reject`:** needs `reason`.
- **`exam.key`:** matches `^[a-z][a-z0-9_]{1,40}$`. `new` must agree with the catalog
  view: an existing key with `new: true` is an error.
- **Members:** every member's `proposal_id` exists. All members share `content_hash`.

## Import outcome per decision (FR-026)

| Condition | Result |
|---|---|
| Any member's `revision` ≠ its current revision | `stale`: nothing written for the whole decision (SC-008) |
| `reject` | `crawler_proposals.review_state = rejected` for the members; no ledger event |
| `approve` | Ledger events in order: `exam_added` (if new and not yet added), `domain_allowlisted` (third-party domains not yet allowed), `candidate` (method `crawler`) for each member, `review`; members set to `approved` |
| Exactly repeated decision | No change (idempotent) |

The command prints one line per decision (`decision_id outcome`) and a summary. Exit
codes follow the pipeline's: 0 ok; 2 usage or validation error; 3 integrity refusal;
4 lock held.
