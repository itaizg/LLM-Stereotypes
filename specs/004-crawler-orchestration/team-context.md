## Discovered Team Context

| ID | Module | Type | Descriptor | Relevance |
|----|--------|------|------------|-----------|
| — | context_modules/constitution.md | Constitution | Core principles (observability, surface every failure, deterministic routing, security by default) | High |
| CDR-2026-020 | context_modules/rules/security/pre_commit_checklist.md | Rule | Pre-commit security checklist (no hardcoded secrets) | High |
| CDR-2026-007 | context_modules/personas/senior_python_developer.md | Persona | Python, data/ML, testing specialist | Medium |
| CDR-2026-027 | context_modules/rules/testing/python/pytest_patterns.md | Rule | Standards for writing and organizing Python tests with pytest | Medium |
| CDR-2026-005 | context_modules/personas/devops_engineer.md | Persona | CI/CD, Infrastructure as Code, GitOps specialist | Medium |

_Searched 10 CDR entries, 5 matches found (including the constitution)._

Key directives applied to the spec: constitution §2 (n8n makes no routing decisions; that logic stays in code), §3 (log every step, and never report success when something was skipped), §4 and CDR-2026-020 (env-var secrets, none in workflow exports).

### Changes from Previous Discovery (before_plan, 2026-11-25)

- **New**: CDR-2026-005 — DevOps engineer persona (Docker Compose for n8n/Langfuse, future Kubernetes path in todo.md), Medium.
