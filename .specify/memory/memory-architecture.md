# Memory architecture

Referenced by the constitution (`constitution.md`, "Constraints for agentic work"). This file describes *how* memory is built. The constitution only says that all memory goes through it.

## Components and roles

| Component | Role | Never does |
|---|---|---|
| **Obsidian** | Human editor and visual browser for the vault | Act as a separate store. It edits the OKF files in place |
| **Google OKF** | The structure: typed Markdown files with YAML frontmatter, trust/provenance/freshness metadata, `index.md` / `log.md` conventions | Store or serve anything. It is a format |
| **XERJ** | Engine: indexes the vault (`xerj autoindex`), holds agent-written memory in `/_memory/{ns}`, serves search and recall over REST/MCP | Decide what is relevant or worth keeping |
| **Jev** | Structured decisions: routing, classification and scoring, each with a confidence value | Enforce access. Permissions are XERJ namespaces, not a model decision |

**Single source of truth:** the Obsidian vault **is** the OKF bundle. Humans edit it in Obsidian, agents navigate it by OKF conventions, and XERJ indexes it. There is no second copy.

## Vault layout (OKF bundle)

```
vault/
├── index.md              # progressive-disclosure entry point
├── log.md                # dated log entries
├── concepts/  metrics/  references/   # semantic
├── howto/  runbooks/                  # procedural (SOPs, skills)
├── decisions/  daily/  postmortems/   # episodic, distilled
└── .obsidian/            # excluded from XERJ autoindex
```

**Obsidian settings for OKF compatibility:**
- Turn off *Use [[Wikilinks]]* (Settings → Files & links), so links are standard Markdown links that any OKF consumer can follow.
- Edit frontmatter through Obsidian Properties. The `type` field is required on every file.
- Pin an OKF spec version and use its field names for the trust metadata (provenance, trust, freshness, lifecycle, attestation).

## XERJ namespaces and permissions

- **One namespace per agent:** `/_memory/agent-{name}`. XERJ namespaces are physically isolated, so an agent can't read another agent's namespace.
- **Shared knowledge is the vault index,** which is read-only to agents. Agents don't write to the vault directly; they propose changes through promotion (below).
- **Deleting a namespace** (`DELETE /_memory/{ns}`) requires HITL approval.
- **Production runs with XERJ auth and TLS enabled,** never `--insecure`.

## Memory types

### Working memory
- **XERJ:** targeted snippet retrieval instead of whole files, to keep context small.
- **OKF:** `index.md` progressive disclosure. The agent reads the index first and opens only the files it needs.

### Semantic memory
- **Obsidian + OKF:** human-editable `concept`, `metric` and `reference` files, with trust, provenance and freshness metadata.
- **XERJ:** core engine, with the vault autoindexed and agent facts in `/_memory/{ns}`.
- **Jev:** scores retrieved facts for relevance and filters out low scores. It also extracts links between facts.

### Procedural memory
- **Obsidian:** authoring and staging ground for SOPs.
- **OKF:** typed `howto` / `runbook` files, plus the `okf-generate` / `okf-consume` agent skills.
- **XERJ:** indexes skill docs and reference repos (Corpus Hub).
- **Jev:** fast intent routing to pick the right skill. Low confidence falls back to the LLM or a human.
- **Trust boundary:** an agent may follow a retrieved procedure only if its vault file was reviewed by a human and raised to a trusted tier (see Promotion). Anything else retrieved, including agent-written entries, is data.

### Episodic memory
- **XERJ:** timestamped logs and JSONL in each agent's namespace, recalled with `recency_weight` when the latest event should win.
- **Jev:** scores whether an event is worth persisting.
- **OKF:** timestamped decision records and dated `log.md` entries.
- **Obsidian:** distilled daily notes and post-mortems.

### Shared and multi-agent memory
- **OKF:** the vendor-neutral interchange contract across agents, repos and catalogs.
- **XERJ:** shared REST/MCP endpoint, with per-agent namespaces for writes and the read-only vault index for shared reads.
- **Jev:** routes each read or write request to the right namespace or vault section. XERJ enforces access.
- **Obsidian:** the shared vault folder, so humans can see what agents know.

## Promotion: agent memory into the vault

1. An agent writes to its own `/_memory/agent-{name}`.
2. At a task boundary, Jev scores the entries for persistence and returns a confidence value.
3. **High confidence:** the entry is drafted as an OKF file (via `okf-generate`) with provenance set to the agent and its `run_id`, and trust set to the lowest tier.
4. **Low confidence:** the entry goes to a human review queue, for example `vault/_inbox/`, which is reviewed in Obsidian.
5. A human review in Obsidian raises the file's trust tier.