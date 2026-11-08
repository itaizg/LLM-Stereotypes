# Contract: Committed Manifests

These three files are committed. They hold every human decision and every fetched hash.
None of them may contain question text: the corpus guard scans them like any other
tracked file.

## `pipeline/catalog.yaml` (human-authored)

```yaml
version: 1
translation:
  model: claude-sonnet-5-5          # MT model; recorded on every MT row
  base_url: https://api.anthropic.com/v1/   # part of the MT cache key (research R10)
defaults:
  rate_limit_seconds: 5
exams:
  enem:
    name: Exame Nacional do Ensino Médio
    body: INEP
    language: pt-BR
    publishes_keys: true             # FR-006; needs evidence below
    keys_evidence: {source_id: src_…, note: "Gabaritos published with each edition"}
    domains: [www.gov.br, download.inep.gov.br]
    rate_limit_seconds: 10           # optional override
    seeds:                           # index pages / sitemaps for discovery (R1)
      - url: https://www.gov.br/inep/…/provas-e-gabaritos
        complete_listing: true       # the body's complete official archive; only then
                                     #   can absence be `index_absence` evidence
    parse:                           # generic parser config (R8)
      item_marker: '^QUESTÃO\s+(\d+)'
      choice_markers: [A, B, C, D, E]
      columns: 2
      key_table: '^(\d+)\s+([A-E])$'
    # parse_english: {…}             # optional; overrides `parse` keys for this exam's
                                     #   official English versions (role `english`)
    card_notes: |                    # copied into the dataset card
      Contamination: ENEM papers are widely mirrored online and very likely in training data.
editions:
  - edition_id: enem-2019-day1-azul
    exam: enem
    year: 2019
    sitting: day1-azul
    kind: administered
    status: ingested
    release_date: 2019-11-13
    official_url: https://…
    lineage: enem
    language: pt-BR
    evidence:
      - {type: release_page, source_id: src_…}
  - edition_id: gaokao-2015-national-iii
    exam: gaokao
    year: 2015
    sitting: national-iii
    kind: administered
    status: not_released
    lineage: gaokao-national
    language: zh-Hans
    evidence:
      - {type: statement, source_id: src_…, note: "NEEA notice: paper not published"}
  - edition_id: common_test-2021-main
    exam: common_test
    year: 2021
    sitting: main
    kind: administered
    status: ingested
    # …
```

An official English version of a paper is **not** an edition. It is approved with
`review --role english --edition <original edition>` and attached to the original edition
as an artifact (data-model `artifacts`), so it adds no edition and no cell coverage. The
original edition may add `english_item_map: {"<section>:<item>": "<section>:<item>"}`
when the English numbering differs. Without a map, items pair by identical
`(section, item)` and must pass the structural check (research R10). An English paper
whose questions differ from the original is a separate edition with `language: en`.
Official versions in other languages are not catalogued (a known gap in the dataset card).

Rules: the `catalog` stage validates everything in data-model.md, `exam_editions`, before
writing. `not_released` needs a `statement`, or an `index_absence` item whose seed is
`complete_listing: true`. Exam keys in `exams` define the scope (FR-001). Adding a Gaokao provincial paper
later is just another `editions` entry, with no schema change (FR-002).

## `pipeline/ledger.jsonl` (machine-written, append-only)

One JSON object per line, in canonical JSON (sorted keys, compact). Lines are never edited
or removed. A test checks that the file is a prefix-extension of its version at
`git merge-base HEAD origin/main` (research R15). Every event has `seq`, `kind`, and `at` (UTC ISO-8601; for audit only, never part
of any hash or ID). `seq` is allocated, and the line appended and fsynced, while the
pipeline write lock is held (research R16), so `seq` is strictly increasing with no ties.
`catalog` fails on a duplicate or decreasing `seq`.

| `kind` | Fields |
|--------|--------|
| `candidate` | `source_id`, `url`, `domain`, `method` (`index_crawl`\|`manual`), `found_on`, `auto_decision` (`rejected` + reason when off-allowlist, else null) |
| `review` | `source_id`, `decision` (`approved`\|`rejected`), `edition_id`, `role`, `published`, `reason` |
| `fetch` | `source_id`, `outcome`, `sha256` (when fetched), `media_type`, `bytes`, `http_status`, and when fetched `etag` and `last_modified` (the response's validators, used for conditional refresh; absent on older lines) |
| `handcheck` | `exam_id`, `seed`, `items: [{question_id, content_hash, key_labels, stem_ok, choices_ok, key_ok, forced}]` (`forced` marks a previously failed question added to a recovery sample, research R14) |
| `translation_review` | `language`, `seed`, `items: [{subject_id, translation_id, score}]` (score 0–3) |

The current decision for a source is that of its latest `review` event. `catalog` replays
the ledger in `seq` order.

## `pipeline/samples/allowlist.yaml`

```yaml
samples:
  - path: pipeline/samples/enem_2019_sample.json
    exam: enem
    source: {edition_id: enem-2019-day1-azul, artifact_sha256: …}
    question_count: 20
    licence: "CC BY 4.0"            # or "public domain"
    licence_evidence: {url: https://…, statement: "…official licence text…"}
```

The guard fails (exit 3) when:
1. a tracked file outside this list contains a corpus fingerprint, after JSON and escape
   decoding (research R13), or is a tracked `.pdf`, `.hwp`, `.parquet`, or audio file;
2. the questions committed for one exam across all its samples exceed
   `SAMPLE_CAP_PER_EXAM` (50, defined once in `pipeline/corpus.py`);
3. an entry has an empty `licence` or `licence_evidence`.

The file starts with `samples: []`. Whether any exam's licence permits a sample is decided
per source from the official statement, never assumed (spec Assumptions).

## `pipeline/fingerprints.txt`

Sorted, unique, lower-case 16-hex-digit hashes, one per line, regenerated by `snapshot`
(research R13). There is no header. The guard treats the file as data and does not scan it.
