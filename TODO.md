# TODO

Deferred work that has been decided on but is out of scope for the change that deferred it.
Highest priority first.

## High priority

- [ ] **Export multi-answer questions to suites.** The exam dataset stores answer keys as
  sets of choice labels (spec 001, FR-012). Multi-answer questions, such as Gaokao
  "select all that apply" items, are stored but not exported, because harness scoring
  expects exactly one correct letter. To export them: teach `harness/scoring.py` to
  extract a set of letters and score an exact set match, then remove the single-answer
  filter from the export. Check that existing single-answer suites still score identically,
  with a regression test. Do not touch the dataset itself, since it already holds the sets.
