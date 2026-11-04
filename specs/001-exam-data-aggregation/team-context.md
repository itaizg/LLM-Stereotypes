# Team Context — 001-exam-data-aggregation

Discovered by `/team-discover` (before_specify hook) on 2026-11-04 from
`/Users/itaizemah-goren/team-ai-directives`.

Feature context: domain = data acquisition / dataset curation (exam questions);
technology = Python 3.13, Postgres, columnar snapshots, object storage, distributed
batch jobs; patterns = staged ETL pipeline, content-addressed raw storage, idempotent
jobs, provenance tracking; actions = discover, fetch, extract, translate, key, export.

## Discovered Team Context

| ID | Module | Type | Descriptor | Relevance |
|----|--------|------|------------|-----------|
| CDR-2026-007 | context_modules/personas/senior_python_developer.md | Persona | Python, data/ML, testing specialist | High |
| CDR-2026-027 | context_modules/rules/testing/python/pytest_patterns.md | Rule | Standards for writing and organizing Python tests with pytest | High |
| CDR-2026-021 | context_modules/rules/security/sql_injection_prevention.md | Rule | Preventing SQL injection across all languages | High |
| rule-rules-style-guides-python-pep8_and_docstrings | context_modules/rules/style-guides/python/pep8_and_docstrings.md | Rule | PEP 8 conventions and docstring standards for Python | Medium |
| CDR-2026-022 | context_modules/rules/style-guides/file_organization.md | Rule | File organization, sizing, and code structure | Medium |
| CDR-2026-020 | context_modules/rules/security/pre_commit_checklist.md | Rule | Pre-commit security checklist | Medium |
| rule-rules-architecture-dependency_injection | context_modules/rules/architecture/dependency_injection.md | Rule | Dependency injection for testable, loosely coupled code | Medium |
| CDR-2026-011 | context_modules/rules/devops/github_actions.md | Rule | GitHub Actions workflow standards | Low |
| CDR-2026-004 | context_modules/personas/data_analyst.md | Persona | SQL, dashboards, reporting specialist | Low |
| github-actions | skills (default) | Skill | GitHub Actions CI/CD pipeline patterns | Low |

_Searched 34 CDR entries and 6 default skills; 10 matches found._

### High-relevance directives (summary of loaded content)

- **Senior Python Developer persona**: PEP 8, thorough automated testing (unit +
  integration, TDD), refers to the pytest, PEP 8 and SQL-injection rules below.
- **pytest patterns**: pytest for all suites; class-based grouping of related scenarios;
  shared fixtures in `conftest.py`; `pytest.mark.parametrize` for input tables; mock
  external dependencies (databases, APIs, file systems), never the code under test.
- **SQL injection prevention**: parameterized queries only, never string interpolation;
  validate inputs at the boundary; least-privilege (read-only where possible) DB
  credentials; regression tests with malicious payloads (e.g. `' OR 1=1 --`).

Project constitution (`.specify/memory/constitution.md`) and `CLAUDE.md` take precedence
over these team directives where they differ (e.g. the project's offline/GPU-free test
rule is stricter than the generic "mock external dependencies" guidance).

### Changes from Previous Discovery

- First discovery for this feature; no previous file.
