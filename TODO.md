# TODO

Deferred work that has been decided on but is out of scope for the change that deferred it.
Highest priority first.

## High priority

- [ ] **Score questions with more than four choices.** `harness/scoring.py` and
  `harness/backends/mock.py` recognise only the letters A–D. Exported exam banks (spec 001,
  FR-012) include single-answer questions with any number of choices: ENEM and CSAT have
  five, and some Common Test items have more. Until this is fixed, a question whose gold
  answer is E or later is scored as an extraction failure, so accuracy on those questions is
  under-reported. The export prints how many questions this affects. To fix: derive the
  valid letters from each question's own choice count, both in answer extraction and in
  MockBackend's wrong-answer draw. Add a regression test showing that existing four-choice
  suites score identically. Do not change the export, which already writes the letters.
  The choice count is per question, not per exam: `parse.choice_markers` in
  `pipeline/catalog.yaml` only describes the paper layout for extraction.
- [ ] **Export multi-answer questions to suites.** The exam dataset stores answer keys as
  sets of choice labels (spec 001, FR-012). Multi-answer questions, such as Gaokao
  "select all that apply" items, are stored but not exported, because harness scoring
  expects exactly one correct letter. To export them: teach `harness/scoring.py` to
  extract a set of letters and score an exact set match, then remove the single-answer
  filter from the export (the `cardinality(joined.key_labels) <> 1` → `multi_answer`
  exclusion in `pipeline/schema.sql`). Check that existing single-answer suites still score
  identically, with a regression test. Do not touch the dataset itself, since it already
  holds the sets. Do not add an exam-wide `answer_mode`: one exam can mix single- and
  multi-answer items, so the key's own label count decides.

## Exam catalog gaps (spec 001)

The 2026-11-19 acquisition run covered ENEM 2015–2025 and the Common Test 2024–2025. These
cells still need a decision or a source:

- [ ] **Gaokao: find an official paper and answer-key source, or change the scope.** NEEA
  (gaokao.neea.edu.cn) publishes expert commentary (试题评析) on the national papers, but no
  official page with the papers or the answer keys was found; they circulate through news
  sites, which are not allowlisted. FR-006 needs an official key source. Next: check MoE
  and provincial examination-authority sites. If none exists, gaokao leaving the scope is
  a spec change.
- [ ] **Psychometric: not ingested.** NITE's practice-test page shows no answer keys
  (FR-006), and its terms forbid copying or use "including for research" without NITE's
  written consent. Ingest only with NITE's consent and an official key source, or change
  the scope.
- [ ] **CSAT key evidence.** Every CSAT edition is `blocked` (robots.txt on both KICE
  hosts disallows all paths), so no KICE answer key can be cited and
  `keys_evidence` stays unmet. The catalog keeps refusing until the scope or FR-006 says
  how an exam whose keys cannot be fetched is handled.
- [ ] **SAT: a release year for each practice test.** Practice Tests 4–11 (test and
  answer-explanation PDFs) are fetched from satsuite.collegeboard.org but have no edition,
  because FR-005 assigns a practice test to its year of official release and no College
  Board page states one. The files' own dates disagree: PDF creation 2022 (test 4),
  2023 (5–6), 2024 (7–10), 2025 (11); the answer PDFs' copyright line says 2022 (4–6),
  2025 (7–10), 2026 (11). Find an official release date (a College Board announcement),
  or decide which file date counts. Test 11 may fall outside 2015–2025.
- [ ] **Common Test 2015–2023.** DNC hosts only the last three years of papers. The
  earlier years were released but are no longer on an official page, which fits none of
  `ingested`, `not_released` or `blocked`. Needs a status decision (spec change) or an
  official archive.

## Later: Dagster orchestration

Deferred from the orchestration mission brief for the exam pipeline. The first version runs
the acquisition stages only, locally: `dagster dev` on the Mac or the compose stack.

- [ ] **Orchestrate harness runs with Dagster.** Run `harness run`, `report` and `compare`
  as jobs, for example when a new suite bank is exported. Keep the harness free of Dagster
  imports, as for the pipeline.
- [ ] **Get the catalog to pass validation.** `pipeline catalog` exits 3 today: every
  exam's `keys_evidence` must cite an approved source, and 66 grid cells have no edition.
  Work it through `discovered`/`fetched` and `pipeline review`, then run the
  `validate_catalog` job until it exits 0. Only that stage and the stages after it need a
  valid catalog; discovery and fetch do not (research D7).
- [ ] **Confirm on a real backfill** that the one-slot writer pool prevents overlap across
  two backfills.
- [ ] **Deploy beyond one machine.** Cloud hosting, Kubernetes, or a remote executor
  instead of the local run launcher. The single-writer pool and the per-host rate limit must
  still hold across workers.
- [ ] **Automation.** Runs are manual. If the catalog ever needs refreshing on a cadence,
  add a schedule or `AutomationCondition` then; exam years are catalog data, not run dates.
