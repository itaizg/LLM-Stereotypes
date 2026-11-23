# Contract: hand-off ticket (crawler output)

This is the crawler's output contract (constitution, "Hand-off ticket"; FR-022). There
is one ticket per (proposal_id, revision). It is stored in `crawler_tickets.ticket`
together with its proposal, and it validates against `crawler.contracts.HandoffTicket`.
The ticket is thin: it holds references and outcomes, not logs.

```jsonc
{
  "owner": "crawler",                       // Literal
  "run_id": "cr_20261123T101500_a3f9",      // equals the trace run_id (FR-032)
  "validation_status": "passed",            // pending | passed | failed
  "input": {
    "url": "https://example.org/exam/2024/paper.pdf",
    "query": "ENEM 2024 prova azul pdf",    // or null when found by link
    "provider": "tavily",                   // tavily | brave | null
    "found_on": null                        // referring URL when found by link
  },
  "output": {
    "proposal_id": "prop_1f3c9a0b2d4e5f60",
    "revision": 1,
    "normalised_url": "https://example.org/exam/2024/paper.pdf",
    "content_hash": "sha256:…",             // null if not sampled
    "reason": null,                          // ReasonCode when failed
    "reason_class": null,                    // transient | permanent | null
    "key_status": "linked_file",
    "flags": ["restrictive_terms"],
    "checks": {
      "reachable":    {"result": "passed", "latency_s": 0.84, "size_bytes": 2310455},
      "downloadable": {"result": "passed", "sample_bytes": 1048576},
      "mcq":          {"result": "passed", "item_count": 12, "page_type_confidence": 0.91},
      "key_located":  {"result": "passed", "evidence": "https://example.org/exam/2024/gabarito.pdf"}
    },
    "cost_usd": 0.0041
  },
  "completeness": {
    "checks_run": 4,                         // of 4
    "share": 1.0,                            // unit "1"
    "notes": []                              // e.g. "memory_unavailable", "stopped_at_limit:cost"
  }
}
```

## Rules

- **`validation_status`:** `passed` only when reachable, downloadable and mcq all passed
  (FR-016). A ticket is `pending` only in memory, before `write_proposal` commits. A
  stored ticket is always `passed` or `failed`.
- **`checks_run`:** counts checks whose result is not `not_run`. When reachability fails,
  `checks_run = 1` and the other three are `not_run` (FR-012).
- **Units:** every quantity field carries its unit in the name and in the schema
  (research R11).
- **No question text:** there is no free-text field beyond enums, URLs and hashes
  (FR-020). A contract test serialises a ticket built from a synthetic MCQ fixture and
  asserts that none of the fixture's stems or choices appear in it.
- **Run status:** a run's own summary (`crawler_runs`) carries the run-level
  `completeness` and `stop_limit` (FR-002, SC-006). Tickets reference it only through
  `run_id` and notes.
