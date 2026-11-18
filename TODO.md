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
