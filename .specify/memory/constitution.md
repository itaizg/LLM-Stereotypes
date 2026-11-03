<!--
Sync Impact Report
==================
Version change: (unfilled template) → 1.0.0
Bump rationale: first ratified constitution; all principles and sections are new.

Principles (template slot → adopted title):
  PRINCIPLE_1 → I. Typed Code, Checked by ty
  PRINCIPLE_2 → II. One Linter and Formatter: ruff
  PRINCIPLE_3 → III. Tests Are the Contract: pytest, GPU-free
  PRINCIPLE_4 → IV. Simplicity: KISS, YAGNI, DRY
  PRINCIPLE_5 → V. Structure: SOLID, Law of Demeter, Low Coupling, Convention over Configuration

Added sections:
  - Quality Gates and Automation (pre-commit, GitHub Actions)
  - Project Constraints (harness/suite separation, scientific integrity, dependencies)
  - Governance

Removed sections: none

Interpretation notes:
  - "CoC" is read as Convention over Configuration (it sits among design principles).
    If Code of Conduct was meant, amend via a MINOR bump adding a contributor-conduct section.
  - "couple/decouple" is read as: low coupling between modules, high cohesion within them.

Follow-up TODOs (repository changes, not made by this amendment):
  - ty is mandated (Principle I) but not yet configured: no [tool.ty] in pyproject.toml,
    no ty hook in .pre-commit-config.yaml, no ty step in .github/workflows/ci.yml.
  - ruff, pytest, pre-commit and GitHub Actions are already configured and compliant.
-->

# LLM-Stereotypes Constitution

## Core Principles

### I. Typed Code, Checked by ty

- Every function, method, and module-level variable in `harness/`, `data/`, and `tests/`
  MUST carry type annotations, including return types (`-> None` included).
- `ty check` MUST pass with zero errors on every commit to `main`. New `# type: ignore` or
  `# ty: ignore` comments MUST name the specific rule and give a one-line reason.
- `Any` is permitted only at true trust boundaries (parsed YAML/JSON, third-party responses)
  and MUST be narrowed to a concrete type (dataclass, `TypedDict`, `Protocol`) before it
  crosses into harness logic.
- Protocols (e.g., `ModelBackend`) MUST be typed so that a non-conforming backend fails the
  type check, not a run.

Rationale: the harness is read and defended line by line; types are the cheapest
documentation that cannot drift, and ty catches contract breaks before a 10K-generation run.

### II. One Linter and Formatter: ruff

- `ruff check` and `ruff format --check` MUST pass on every commit. ruff is the sole linter
  and formatter; black, isort, flake8, and pylint MUST NOT be added.
- Rule selection lives only in `pyproject.toml` under `[tool.ruff]`. Changes to the selected
  rule set are deliberate commits with a stated reason, never incidental.
- Per-line `# noqa` MUST name the rule code. Excluding paths is reserved for `legacy/`, which
  is preserved untouched by project rule.

Rationale: one tool, one config file, zero style debate in review.

### III. Tests Are the Contract: pytest, GPU-free

- All tests MUST use pytest and live under `tests/`.
- Tests MUST run with no GPU, no model weights, no network, and no API keys. Model behavior
  is supplied by the deterministic `MockBackend`.
- Every behavior change in `harness/` MUST ship with a test that fails without the change.
  Bug fixes MUST include a regression test.
- Statistics code (`stats.py`) MUST be tested against cases with known answers
  (hand-computed values or an injected effect that the code must detect), not only for
  "runs without raising."
- Tests MUST be deterministic: seeded randomness, no wall-clock or ordering dependence.

Rationale: the statistics are the product; an untested contrast is an unverified claim.

### IV. Simplicity: KISS, YAGNI, DRY

- KISS: choose the most direct construct that solves the current problem. A plain function
  beats a class; a dataclass beats a metaclass; the standard library beats a dependency.
- YAGNI: do not build for a hypothetical second consumer. An abstraction, option, or hook is
  added only when a concrete current use requires it. The trigger is the second real
  instance, not the anticipated one.
- DRY: each fact (a prompt, a threshold, a file layout, a schema) has exactly one
  authoritative location. Prompts live in suite configs; artifact paths and names are
  defined once in the harness. Duplication of *knowledge* MUST be removed; incidental
  similarity of code that may diverge MAY remain.
- When KISS/YAGNI and DRY conflict, prefer the simpler code and accept small duplication
  until a third occurrence proves the shared shape.

Rationale: "small enough to own end-to-end" is a stated differentiator of this repo;
every speculative layer erodes it.

### V. Structure: SOLID, Law of Demeter, Low Coupling, Convention over Configuration

- Single Responsibility: each module has one reason to change (`scoring.py` extracts and
  scores, `stats.py` compares arms, `cache.py` stores responses). A module mixing two such
  concerns MUST be split.
- Open/Closed: new suites MUST be addable through config files alone; new backends through
  a new module implementing `ModelBackend`. Neither requires editing runner, stats, or report
  code.
- Liskov: every backend MUST be substitutable for any other behind the protocol, including
  `MockBackend`; backend-specific branching in core code is a violation.
- Interface Segregation: protocols expose only what callers use; no "god" interfaces.
- Dependency Inversion: core harness code depends on protocols and plain data, never on a
  concrete backend, SDK, or inference engine.
- Law of Demeter: a unit talks only to its direct collaborators. Chains reaching through
  another object's internals (`run.config.suite.conditions[0].prompt.text`) MUST be replaced
  by passing the needed value or asking the owner for it.
- Coupling and cohesion: modules are loosely coupled (communicate through typed data and
  protocols, no shared mutable state, no import cycles) and highly cohesive (things that
  change together live together). `harness/` MUST NOT import from `suites/` or contain
  suite-specific knowledge.
- Convention over Configuration: a suite is a directory with `conditions.yaml` and
  `questions.json`; a run writes `results/<run_id>/` with a fixed artifact set. Sensible
  defaults (e.g., `n_samples`, temperature) live in one place; flags exist only to override
  them. New configuration knobs require a current need (see YAGNI).

Rationale: the harness/suite split is the core design claim of the repo; these rules are
how that claim stays true as the code grows.

## Quality Gates and Automation

- pre-commit: `.pre-commit-config.yaml` MUST run ruff (check and format) and ty on staged
  files. Hook revisions MUST be pinned to exact versions. Contributors MUST install the hooks
  (`pre-commit install`); bypassing them with `--no-verify` is not permitted for commits
  intended for `main`.
- GitHub Actions: `.github/workflows/ci.yml` MUST, on every pull request and push to `main`,
  run (1) all pre-commit hooks on all files, (2) pytest across every supported Python
  version (3.11 minimum), and (3) an end-to-end harness CLI smoke test against
  `MockBackend` that asserts required artifacts exist and an injected effect is detected.
- CI jobs MUST use least-privilege permissions (`contents: read` unless a job needs more),
  pinned action versions, and no secrets.
- A pull request MUST NOT merge with a failing or skipped required check. Local gate and CI
  gate run the same tools at the same versions, so "passes locally" implies "passes in CI."

## Project Constraints

- Separation of concerns: `harness/` knows nothing about stereotypes; experiment content
  lives in `suites/`. Any change that makes a second suite require harness code edits
  violates Principle V.
- Scientific integrity: results come only from executed runs; no simulated, estimated, or
  hand-filled numbers. Deltas within their confidence interval are reported as "not
  distinguishable from noise." Every reported number carries a CI; pairwise comparisons use
  Holm correction.
- Dependencies: Python 3.11+, managed with `uv` and pinned. Heavyweight frameworks
  (LangChain, MLflow, W&B) MUST NOT be added. Model-inference packages stay in the optional
  `model` extra; the core install stays GPU-free.
- Device handling: detect MPS → CUDA → CPU at runtime; never hardcode a device. Record
  device and backend in every run manifest.
- Secrets: environment variables only; never committed, never printed or logged.
- `legacy/` is preserved untouched and is exempt from Principles I, II, and V.

## Governance

- This constitution governs engineering practice in this repository. Where it conflicts
  with ad-hoc habit, the constitution wins; `CLAUDE.md` provides runtime guidance and
  project direction and MUST stay consistent with it.
- Amendments are made by pull request that edits this file, updates the Sync Impact Report,
  and bumps the version. Any amendment that introduces a new gate MUST land together with,
  or be immediately followed by, the tooling change that enforces it.
- Versioning (semantic): MAJOR for removing or redefining a principle or gate; MINOR for a
  new principle or section or materially expanded guidance; PATCH for clarifications and
  wording.
- Compliance: every pull request review MUST check the change against Principles I–V and the
  Quality Gates. Deviations require an explicit, written justification in the PR
  description; unjustified complexity is grounds to reject.
- Spec Kit plans (`/speckit-plan`) MUST include a Constitution Check that evaluates the
  design against these principles before implementation begins.

**Version**: 1.0.0 | **Ratified**: 2026-11-03 | **Last Amended**: 2026-11-03
