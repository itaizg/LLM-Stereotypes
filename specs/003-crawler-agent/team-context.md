# Team Context — crawler agent

Discovered by `/team-discover` (before_specify hook) on 2026-11-23 from
`~/team-ai-directives` (CDR index: 34 entries, all `stable`, last
updated 2026-08-12; 6 default and 8 external skills). The index uses `stable` instead of
`Accepted`, so `stable` rows are treated as accepted, as in spec 002.

Feature context: domain = web discovery and validation of exam question sources, feeding
a dataset catalog with human review; technology = Python 3.13, LangGraph agent, web
search APIs (Tavily, Brave), HTTP probing, Pydantic contracts, Postgres pipeline store,
GitHub Actions; patterns = single-node agent with 5 bounded tools, handoff tickets,
human-in-the-loop approval, idempotent upserts, shared rate limiter, eval-driven
development; actions = search, probe, sample, classify, label, propose, review, import.

## Discovered Team Context

| ID | Module | Type | Descriptor | Relevance |
|----|--------|------|------------|-----------|
| CDR-2026-007 | context_modules/personas/senior_python_developer.md | Persona | Python, data/ML, testing specialist | High |
| CDR-2026-027 | context_modules/rules/testing/python/pytest_patterns.md | Rule | Writing and organizing Python tests with pytest | High |
| CDR-2026-020 | context_modules/rules/security/pre_commit_checklist.md | Rule | Pre-commit security checklist (secrets, input validation, rate limiting, error handling) | High |
| example-examples-prompts-mission_brief | context_modules/examples/prompts/mission_brief.md | Example | Mission Brief prompt template (goal, success criteria, constraints) | Medium |
| rule-rules-architecture-dependency_injection | context_modules/rules/architecture/dependency_injection.md | Rule | Dependency injection for testable, loosely coupled code | High |
| CDR-2026-022 | context_modules/rules/style-guides/file_organization.md | Rule | File organization, sizing, and code structure | Medium |
| rule-rules-style-guides-python-pep8_and_docstrings | context_modules/rules/style-guides/python/pep8_and_docstrings.md | Rule | PEP 8 and docstring standards | Medium |
| example-examples-testing-pytest_class_based | context_modules/examples/testing/pytest_class_based.md | Example | Class-based pytest structure | Medium |
| CDR-2026-011 | context_modules/rules/devops/github_actions.md | Rule | GitHub Actions workflow standards | Low |
| CDR-2026-021 | context_modules/rules/security/sql_injection_prevention.md | Rule | Preventing SQL injection | High |
| evaluation | skills (external) | Skill | Build evaluation frameworks for agent systems | High |
| advanced-evaluation | skills (external) | Skill | LLM-as-a-judge scoring, pairwise comparison, rubrics | Medium |
| context-engineering-fundamentals | skills (external) | Skill | Context windows and context anatomy in agent systems | Medium |
| context-degradation | skills (external) | Skill | Context failure patterns: poisoning, distraction, clash | Medium |
| github-actions | skills (default) | Skill | GitHub Actions CI/CD pipeline patterns | Low |
| web-design-guidelines | skills (external) | Skill | Web interface review: accessibility, UX (review page) | Low |

_Searched 34 CDR entries, 6 default skills and 8 external skills; 16 matches found._

### Changes from Previous Discovery

Re-run by the before_plan hook on 2026-11-23. The CDR index and skill registry are
unchanged; the plan's design raises two rules.

- **Changed**: CDR-2026-021 SQL injection prevention, relevance Low → High (the proposal
  store, host-slot limiter and import step all write to Postgres).
- **Changed**: dependency_injection, relevance Medium → High (tools and clients are
  injected so the offline replay eval can swap recorded responses in).
- **New**: web-design-guidelines (external skill), Low, for the review page.

## High-relevance modules (full directive text)

### Persona — Senior Python Developer (CDR-2026-007)

- Write clean, efficient, and maintainable Python code following PEP 8 standards.
- Implement robust testing strategies including unit tests, integration tests, and TDD.
- Optimize code for performance and scalability.
- Mentor junior developers and contribute to team knowledge sharing.
- Rule references: PEP 8 and docstrings; pytest patterns; SQL injection prevention.
- Collaboration: thorough code reviews, pair programming on complex features, automated
  testing and CI/CD, clear communication of technical decisions and trade-offs.

### Rule — Python Testing with Pytest (CDR-2026-027)

## Checklist
- Use pytest for all new test suites
- Organize tests with class-based grouping for related test scenarios
- Use fixtures in conftest.py for shared test data
- Apply pytest.mark.parametrize for testing multiple inputs
- Mock external dependencies (databases, APIs, file systems) not code under test
- Follow TDD when appropriate: write failing test, implement to pass, refactor
- Use descriptive test method names following pytest conventions

## Test Organization

### Rule — Pre-Commit Security Checklist (CDR-2026-020), parts that apply

- **No hardcoded secrets:** no API keys, passwords, tokens or credentialed connection
  strings in source (search provider keys and LLM keys come from env vars).
- **SQL injection:** parameterized queries only; no string concatenation in SQL (proposal
  store, host-slot limiter, import step).
- **Input validation:** validate all inputs at system boundaries with schema-based
  validation, length and type constraints, fail fast with clear errors (URLs from search
  results, decision files from the review page).
- **XSS:** escape user-generated and web-derived content before rendering (the review
  page shows labels, excerpts and URLs taken from crawled sites).
- **Rate limiting** and **error handling:** error messages and logs must not leak
  sensitive data; sensitive data is not logged.
- **Dependencies:** pinned versions, minimal dependencies, audited.

### Rule — Prevent SQL Injection (CDR-2026-021), parts that apply

- Always use parameterized queries; never interpolate or concatenate strings in SQL.
- Validate inputs at the boundary layer before any database operation.
- Least privilege on database credentials.
- Regression tests with malicious payloads (e.g. `' OR 1=1 --`).

### Rule — Dependency Injection

Services and components receive their dependencies instead of creating them. Constructor
injection is the primary method; dependencies are interfaces (here, `typing.Protocol`) so
they can be mocked or swapped.

## How this applies to the spec

- Web-derived text shown on the review page is untrusted and must be escaped.
- Every input crossing a boundary (search results, URLs, decisions files) is validated
  against a schema before use.
- Evals are designed with the `evaluation` skill's framing: labelled sets, precision and
  recall, offline replay; LLM-as-judge only where a deterministic check cannot decide.
- Every SQL statement in the crawler store, limiter and import step uses psycopg
  parameters; tests include a malicious-payload case.
- Search providers, the HTTP client, the LLM classifier, the clock and the store are
  injected Protocols, so replay fixtures replace them in CI without network.
