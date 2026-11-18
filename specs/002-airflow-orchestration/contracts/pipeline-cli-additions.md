# Contract: Pipeline CLI additions for orchestration

These are additions to spec 001's [CLI contract](../../001-exam-data-aggregation/contracts/cli.md).
They land in the pipeline before any orchestration file. When a PR implements one of them, the
same PR edits spec 001's `contracts/cli.md` so that contract stays the single authority.

## P1. `pipeline exams [--snapshot V]`

| Form | Reads | Writes | Prints |
|------|-------|--------|--------|
| `pipeline exams` | `catalog.yaml` (`--catalog`) | nothing; no lock, no database | one exam id per line, sorted, then `exams: count=N` |
| `pipeline exams --snapshot V` *(ships with spec 001 export)* | `<blob root>/snapshots/V/exam_status.parquet` | nothing; no lock, no database | one line per exam that the snapshot does **not** mark `unverified`, sorted, then `exams: count=N snapshot=V` |

Exit codes:
- 0: success, including an empty list.
- 2: unknown snapshot version, or `--exam`/`--edition` given (they make no sense here).
- 3: the catalog file fails to load.

The plain form does not validate the catalog's evidence; `catalog` does that.

## P2. `pipeline candidates [--exam X]`

| Reads | Writes | Prints |
|-------|--------|--------|
| `ledger.jsonl` (`--ledger`), `catalog.yaml` | nothing; no lock, no database | one line per pending candidate, then `candidates: pending=N` |

Line format:

```text
candidate <source_id> exam=<exam_id> domain=<domain>
```

- **Pending** means the source's latest decision is `pending`, that is, no `review`
  event. The ledger replay is the same one `review` uses.
- **Exam attribution**: a candidate belongs to every exam whose catalog `domains` list
  its domain, and appears once per such exam. This one rule covers crawled and manual
  candidates alike. A manual candidate's `candidate` event does not record the edition
  given with `--add-url`, and a pending candidate is always on some exam's allowlist,
  since off-allowlist links are auto-rejected.
- **Ordering**: sorted by `(exam_id, source_id)`, so the output is deterministic.
- **`--exam X`**: lists only that exam's candidates. An exam not in the catalog is a
  usage error (exit 2).
- **Content**: no URL path and no page text, so the output is safe for task logs
  (FR-014).

## P3. Request spacing across invocations

The spec 001 contract line "the write lock means only one `fetch` runs, so the
per-domain interval holds across invocations" becomes true for every command that sends
requests (`discover`, `fetch`). The interval holds between the last request of one
command and the first request of the next, using the `host_requests` table
([data-model.md](../data-model.md)). No flag is added.

## P4. `python -m pipeline.corpus [PATH…]`

With no arguments the guard behaves as it does now: it checks tracked files and the
samples allowlist.

With one or more paths, it also scans every regular file under them, tracked or not,
for `corpus_text` hits only. The allowlist and binary-suffix rules apply to tracked
files only. Exit 3 on any hit, as before.

It is used to check Dagster's run storage (SC-009). Until a snapshot has published
fingerprints, `pipeline/fingerprints.txt` is empty and the check cannot find anything;
the quickstart says so.

## Summary lines the assets parse

The assets parse only these lines. All other output is logged verbatim and never parsed.

| Command | Line (regular expression) | Used by |
|---------|---------------------------|---------|
| `exams` | `^exams: count=\d+( snapshot=snap-\d{4})?$`, with the exam ids above it | `select_exams`, `exportable_exams` |
| `candidates` | `^candidate (\S+) exam=(\S+) domain=(\S+)$`, `^candidates: pending=\d+$` | `exam_branch.discover`, `pending_summary` |
| `snapshot` *(spec 001 US4 must print exactly these)* | `^snapshot: published (snap-\d{4})\b` or `^snapshot: unchanged \((snap-\d{4})\)$` | `announce_snapshot` |

Changing one of these lines is a breaking change to this contract. It must update the
stage parser and its tests in the same PR.
