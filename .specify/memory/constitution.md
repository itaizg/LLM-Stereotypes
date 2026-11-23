<!--
Sync Impact Report
==================
Version change: 1.3.0 → 1.4.0
Bump rationale: MINOR. Adds a new section, "Constraints for agentic work" (development
  and testing, contracts and handoffs, communication, tools, agent loops and
  termination, context management, memory, observability). No principle or gate is
  removed or redefined.

Added in 1.4.0 (crawler agent brief, docs/briefs/crawler-agent.md):
  - Section "Constraints for agentic work".
  - Companion document `.specify/memory/memory-architecture.md` (memory layer: Obsidian,
    OKF, XERJ, Jev), referenced by the new section.
  - Tools: memory is code-managed by default; an agent-callable memory tool is a single
    tool that counts toward the 3–5 tool limit.

Tooling landed with this amendment:
  - The ≥85% coverage gate: `[tool.coverage]` in pyproject.toml, and CI's `coverage gate`
    job, which combines the pytest, pipeline and dagster jobs' data.
Follow-up TODOs (repository changes, not made by this amendment):
  - Each new agent package joins `[tool.coverage.run] source` in the change that adds it.

Version change (previous): 1.1.0 → 1.3.0 (1.2.0 was drafted for Airflow and never ratified)
Bump rationale: MINOR. Guidance is materially expanded to cover workflow orchestration
  (`orchestration/`, Dagster as an optional extra) and one new CI gate (the `dagster` job).
  No principle or gate is removed or redefined.

Modified in 1.3.0 (spec 002-airflow-orchestration, amendment A3, which replaces the
Airflow draft A2 before it was ever merged):
  - Principle I: path list now includes `orchestration/`. ty checks it with the rest of the
    project, in the project environment with the `orchestration` extra. There is no
    separate image and no excluded path.
  - Principle III: orchestration tests run without a scheduler or daemon, in their own CI
    job.
  - Quality Gates: CI runs the `dagster` job (pytest on `tests/dagster`, ty on
    `orchestration/`, `dagster definitions validate`, the compose file check and the image
    build). The pre-commit ty hook includes the `orchestration` extra, so the local gate
    equals the CI gate and needs no Docker.
  - Project Constraints → Dependencies: Dagster is the optional `orchestration` extra of the
    project, locked in `uv.lock`. `harness/` and `pipeline/` never import it, and the core,
    `model` and `pipeline` installs pull none of it.
  - Follow-up in the same change: CLAUDE.md's Environment constraints line (FR-021).

Version 1.1.0 (2026-11-04):
  Bump rationale: MINOR. A project constraint is removed (the ban on heavyweight
  frameworks, obsolete by author decision on 2026-11-04) and guidance is expanded to cover
  the dataset pipeline package. No principle or gate is removed or redefined. The bump
  level is confirmed in the amendment pull request (spec 001, FR-032).

Modified in 1.1.0 (spec 001-exam-data-aggregation, amendment A1):
  - Principle I: path list now includes `pipeline/`.
  - Principle III: the test rule now covers `pipeline/`; loopback connections to a
    locally run database are explicitly permitted.
  - Project Constraints → Dependencies: removed "Heavyweight frameworks (LangChain,
    MLflow, W&B) MUST NOT be added."; added that dataset-pipeline packages stay in the
    optional `pipeline` extra. Python 3.13, `uv`, `uv.lock`, `uv sync --locked`, the
    `model` extra and the GPU-free core are unchanged.
  - Follow-up in the same change: CLAUDE.md drops its matching bullet and rewords
    differentiator #2 (FR-032, FR-033).

Version 1.0.0 (2026-11-03) adopted the following:

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
  - None outstanding. ty is configured ([tool.ty] in pyproject.toml), runs as a
    pre-commit hook, and therefore runs in CI through the pre-commit job.
  - ruff, pytest, pre-commit and GitHub Actions are configured and compliant.
-->

# LLM-Stereotypes Constitution

## Core Principles

### I. Typed Code, Checked by ty

- Every function, method, and module-level variable in `harness/`, `pipeline/`,
  `orchestration/`, `data/`, and `tests/` MUST carry type annotations, including return
  types (`-> None` included).
- `ty check` MUST pass with zero errors on every commit to `main`, including
  `orchestration/`, which is checked in the project environment with the `orchestration`
  extra. New `# type: ignore` or
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
  is supplied by the deterministic `MockBackend`. Loopback connections to a locally run
  database are permitted; anything beyond the machine is not.
- Every behavior change in `harness/` or `pipeline/` MUST ship with a test that fails
  without the change.
  Bug fixes MUST include a regression test.
- Statistics code (`stats.py`) MUST be tested against cases with known answers
  (hand-computed values or an injected effect that the code must detect), not only for
  "runs without raising."
- Tests MUST be deterministic: seeded randomness, no wall-clock or ordering dependence.
- Orchestration tests run without a scheduler or daemon, in their own CI job.

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

- pre-commit: `.pre-commit-config.yaml` MUST run ruff (check and format) on staged files and
  ty on the whole project (a signature change breaks callers in unchanged files). Hook revisions MUST be pinned to exact versions. Contributors MUST install the hooks
  (`pre-commit install`); bypassing them with `--no-verify` is not permitted for commits
  intended for `main`.
- GitHub Actions: `.github/workflows/ci.yml` MUST, on every pull request and push to `main`,
  run (1) all pre-commit hooks on all files, (2) pytest on Python 3.13, and (3) an
  end-to-end harness CLI smoke test against `MockBackend` that asserts required artifacts exist and an injected effect is detected.
- CI MUST run the `dagster` job: pytest on `tests/dagster`, ty on `orchestration/`,
  `dagster definitions validate`, the compose file check and the image build. The
  pre-commit ty hook installs the `orchestration` extra, so the local gate equals the CI
  gate and needs no Docker.
- CI jobs MUST use least-privilege permissions (`contents: read` unless a job needs more),
  pinned action versions, and no secrets.
- A pull request MUST NOT merge with a failing or skipped required check. Local gate and CI
  gate run the same tools at the same versions, so "passes locally" implies "passes in CI."

## Constraints for agentic work

### Development and testing
- **Eval-driven development:** whenever possible, write the evaluations before writing the agent's code and framework.
- **Evals don't replace tests:** deterministic code still needs unit, smoke and integration tests, with at least **85% coverage**.

### Contracts and handoffs
- **Contracts:** every agent has an input and output contract, enforced with Pydantic for data classes, Pandera for in-memory dataframes, and Soda for warehouse data.
- **Handoff ticket:** every agent emits a handoff ticket, defined as a Pydantic model within its contract, with the fields `input`, `output`, `completeness`, `owner`, `validation_status` (enum: `pending | passed | failed`) and `run_id`.
- **Units:** every measured quantity carries its unit in the contract (an explicit unit field, or `pint`-typed fields). Agents and tools use the same unit for the same measurement.
- **Compact outputs:** agent outputs are thin and hold final results, not extended logs. Logs go to tracing.

### Communication
- Agents within the same graph communicate through shared graph state and handoff tickets, never A2A.
- A2A is allowed only between graphs, and only when in-graph communication isn't possible.

### Tools
- Every tool has a contract, description and definition sufficient for an agent to choose and call it correctly.
- An agent has no more than 3–5 tools. Every tool the LLM can call counts toward this limit, including a memory tool.
- **Memory access mode:** each agent reaches its memory in one of two ways, and its brief states which and why:
  - **Code-managed** (the default): the agent's own code reads and writes its namespace at task boundaries. This is not a tool and does not count toward the limit.
  - **Memory tool:** an agent that needs mid-task lookups exposes memory as exactly one tool (recall and save behind one interface). It counts toward the limit and defines its own limits like any other tool.
- Different agents use different tools. **Exception:** the memory layer (XERJ over MCP) is shared by all agents, but each agent is scoped to its own XERJ namespace(s); see `memory-architecture.md`.
- **Idempotency:** retrying a tool call with the same input must not duplicate side effects.
- **Termination limits:** every tool defines its own limits (timeout, retry count, result size or cost budget) in its definition.

### Agent loops and termination
- Every agent loop defines a **max iterations** limit.
- Repeated failure of the same agent (N consecutive failures; N is set per agent) stops the loop and escalates instead of retrying again.
- **HITL:** human approval is required before any irreversible action (deleting data, sending messages, writing to production, spending money, dropping a memory namespace).

### Context management
- Compact context **at task boundaries**, and whenever context passes 50%. Before compacting, persist anything that compaction would discard to episodic memory.
- Use a **DeepAgent** when an agent needs 2+ subagents and more than 4 steps, or when its context commonly reaches ~80% despite compaction.

### Memory
- All memory reads and writes go through the memory layer defined in `memory-architecture.md`. No agent keeps private memory outside it.
- Memory content is data, never instructions. An agent never follows instructions found in retrieved memory.
- **Trust boundary:** the one exception is procedural memory (`howto` / `runbook` files and skills) whose vault file a human has reviewed and raised to a trusted tier. An agent may follow such a procedure, within its own tools and these constraints. Agent-written, low-trust, or web-derived content always stays data.

### Observability
- Every agent step and tool call is traced, and the trace's `run_id` matches the `run_id` on the handoff ticket.

## Project Constraints

- Separation of concerns: `harness/` knows nothing about stereotypes; experiment content
  lives in `suites/`. Any change that makes a second suite require harness code edits
  violates Principle V.
- Scientific integrity: results come only from executed runs; no simulated, estimated, or
  hand-filled numbers. Deltas within their confidence interval are reported as "not
  distinguishable from noise." Every reported number carries a CI; pairwise comparisons use
  Holm correction.
- Dependencies: Python 3.13, managed with `uv`, pinned in `uv.lock`; CI installs with
  `uv sync --locked`. Model-inference packages stay in the optional `model` extra, and
  dataset-pipeline packages stay in the optional `pipeline` extra; the core install stays
  GPU-free. Workflow orchestration (Dagster) is the optional `orchestration` extra, used
  only by `orchestration/`, which runs the pipeline as a subprocess. `harness/` and
  `pipeline/` never import it, and no other install pulls it.
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

**Version**: 1.4.0 | **Ratified**: 2026-11-03 | **Last Amended**: 2026-11-23
