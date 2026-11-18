# Team Context — 002-airflow-orchestration

> **Superseded in part (amendment A3, 2026-11-18).** The orchestrator is Dagster, not
> Airflow: assets partitioned by exam, one uv environment (the `orchestration` extra), no
> sensors or schedules. Read [research.md](research.md) D1–D5 for the decision record and
> [contracts/assets.md](contracts/assets.md) and [contracts/stack.md](contracts/stack.md)
> for the current contracts. Mapping: DAG → asset group; `select_exams` → static
> partitions; `pipeline_writer` pool → Dagster pool; `pending_summary` → `pending_review`
> check; Clear failed → Re-execute from failure. The text below is kept as the original
> record, and where it names Airflow concepts the mapping above applies.

Discovered by `/team-discover` (before_plan hook) on 2026-11-09 from
`/Users/itaizemah-goren/team-ai-directives` (CDR index: 34 entries, all `stable`, last
updated 2026-08-12; 6 default skills). The index no longer uses an `Accepted` status, so
`stable` rows are treated as accepted.

Feature context: domain = workflow orchestration of an existing data pipeline;
technology = Python 3.13, Apache Airflow 3.1 (official Docker image, LocalExecutor),
Postgres (one server, two databases), Docker Compose, GitHub Actions; patterns = DAGs over
idempotent CLI stages, per-exam dynamic branches, single-writer pool, asset-triggered
downstream DAG, resumable runs; actions = trigger, filter, retry, resume, warn, log.

## Discovered Team Context

| ID | Module | Type | Descriptor | Relevance |
|----|--------|------|------------|-----------|
| rule-rules-orchestration-airflow_dag_patterns | context_modules/rules/orchestration/airflow_dag_patterns.md | Rule | Airflow DAG structure, imports, registration, KubernetesPodOperator | High |
| CDR-2026-007 | context_modules/personas/senior_python_developer.md | Persona | Python, data/ML, testing specialist | High |
| CDR-2026-027 | context_modules/rules/testing/python/pytest_patterns.md | Rule | Writing and organizing Python tests with pytest | High |
| CDR-2026-005 | context_modules/personas/devops_engineer.md | Persona | CI/CD, Infrastructure as Code, GitOps specialist | Medium |
| CDR-2026-011 | context_modules/rules/devops/github_actions.md | Rule | GitHub Actions workflow standards | Medium |
| CDR-2026-022 | context_modules/rules/style-guides/file_organization.md | Rule | File organization, sizing, and code structure | Medium |
| CDR-2026-020 | context_modules/rules/security/pre_commit_checklist.md | Rule | Pre-commit security checklist | Medium |
| rule-rules-style-guides-python-pep8_and_docstrings | context_modules/rules/style-guides/python/pep8_and_docstrings.md | Rule | PEP 8 and docstring standards | Medium |
| example-examples-testing-pytest_class_based | context_modules/examples/testing/pytest_class_based.md | Example | Class-based pytest structure | Medium |
| rule-rules-architecture-dependency_injection | context_modules/rules/architecture/dependency_injection.md | Rule | Dependency injection for testable code | Low |
| CDR-2026-021 | context_modules/rules/security/sql_injection_prevention.md | Rule | Preventing SQL injection | Low |
| github-actions | skills (default) | Skill | GitHub Actions CI/CD pipeline patterns | Medium |

_Searched 34 CDR entries and 6 default skills (8 external skills checked by category; none
match); 12 matches found._

### High-relevance directives (summary of loaded content)

- **Airflow DAG patterns** (written for Airflow 2 on Kubernetes). What applies here:
  - DAG files live in a `dags/` package; helpers sit in their own modules with unit tests.
  - Shared `default_args` with bounded retries and a retry delay.
  - TaskGroups for logical grouping; `>>` for dependencies.
  - Manual-trigger DAGs use no schedule, with `catchup=False`.
  - Validate that every DAG imports without errors.
  - XCom carries small data only.

  **Overridden by the spec and constitution** (they take precedence):
  - `schedule_interval`, `days_ago` and `globals()[dag_id]` registration are Airflow 2
    APIs. Airflow 3.1 uses `schedule=`, a fixed `start_date`, and the `@dag` decorator
    from `airflow.sdk` (FR-023).
  - `Variable.get(...)` for configuration and connection strings is forbidden: secrets
    and settings come from environment variables only (FR-015).
  - KubernetesPodOperator, namespaces and Kubernetes secrets are out of scope (local
    LocalExecutor only; Kubernetes is a TODO.md item).
  - Failure/success callbacks for alerting are not used: failures surface in the UI only
    (FR-027).
- **Senior Python Developer persona**: PEP 8, type hints, thorough automated tests.
- **pytest patterns**: class-based grouping, shared fixtures in `conftest.py`,
  `parametrize` for input tables, mock external dependencies (here: the stage command
  runner), never the code under test.

### Medium-relevance notes

- **DevOps Engineer persona**: the Compose file and image extension are versioned code;
  CI is separate from running the stack; secrets never in source control.
- **GitHub Actions rule / skill**: the Airflow tests get their own CI job (FR-022).
- **File organization (CDR-2026-022)**: files stay under 800 lines.
- **PEP 8 and docstrings**: type hints on public functions; docstrings explain purpose.
  The project's `CLAUDE.md` adds that docstrings explain *why*.
- **Class-based pytest example**: group related scenarios in `Test…` classes with
  fixture factories.

### Changes from Previous Discovery

- **New**: rule-rules-orchestration-airflow_dag_patterns — Airflow DAG patterns (Rule),
  High. It was not in the index at the before_specify discovery.
- **New**: rule-rules-style-guides-python-pep8_and_docstrings (Rule), Medium.
- **New**: example-examples-testing-pytest_class_based (Example), Medium.
- **New**: rule-rules-architecture-dependency_injection (Rule), Low.
- **Changed**: the index format — 34 `stable` entries instead of 10 `Accepted` ones.
- **Unchanged**: CDR-2026-005, 007, 011, 020, 021, 022, 027 and the github-actions skill
  keep their relevance.

Project constitution (`.specify/memory/constitution.md`) and `CLAUDE.md` take precedence
over team directives where they differ.
