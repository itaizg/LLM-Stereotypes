# Feature Specification: Crawler Orchestration and Tracing

**Feature Branch**: `004-crawler-orchestration`

**Created**: 2026-11-25

**Status**: Draft

**Input**: User description: "Use the mission brief at specs/004-crawler-orchestration/mission.md: Orchestrate & trace crawler agent #1. Goals: n8n starts crawler runs on a schedule or webhook; Langfuse traces every run, LLM call and tool step; structured console logs mirror each step. Non-goals: changing crawler logic, n8n making routing decisions, hosted deployment. Constraints: self-hosted; secrets via env vars only; harness core imports neither tool. Success: one n8n-started run shows a complete Langfuse trace and matching console logs; failures always surface, never silent."

Mission brief: [mission.md](mission.md). Builds on spec 003 ([crawler agent](../003-crawler-agent/spec.md)).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Start a crawler run without sitting at a terminal (Priority: P1)

The maintainer wants crawler runs to start on a schedule (for example nightly) or from
an on-demand trigger, instead of typing the command by hand. A self-hosted workflow
tool starts the existing crawler command, waits for it to finish, and records the
outcome (completed, failed, escalated, stopped at a limit, or not approved).

**Why this priority**: Without an unattended start there is nothing to orchestrate; the
tracing and logging stories add value to runs that this story makes routine.

**Independent Test**: Trigger the workflow by hand once; confirm the crawler ran, the
workflow shows the crawler's exit outcome, and a run directory exists for that run.

**Acceptance Scenarios**:

1. **Given** a configured schedule, **When** the scheduled time arrives, **Then** exactly one crawler run starts and the workflow records its run id and exit outcome.
2. **Given** the on-demand trigger, **When** the maintainer calls it, **Then** a run starts and the caller gets the run id back.
3. **Given** a run is already in progress, **When** another trigger fires, **Then** the second start is refused and recorded as skipped, not run in parallel.
4. **Given** the crawler exits with any non-zero outcome, **When** the workflow finishes, **Then** the outcome is shown as a failure (or as the specific outcome), never as success.

---

### User Story 2 - See the whole run as one trace (Priority: P2)

After a run, the maintainer opens one trace for it in a self-hosted LLM-observability
tool and sees every agent step, every LLM call (model, tokens, cost, latency) and every
tool call (search, probe, sample, memory) nested under the run, with failures marked.

**Why this priority**: The crawler already writes a local trace file, but reading it by
hand doesn't scale. A browsable trace is how the maintainer judges cost and behaviour
across runs.

**Independent Test**: Run the crawler once (started by hand or by the workflow) with
tracing configured; open the trace by run id and check that each step in the local
trace file has a matching entry.

**Acceptance Scenarios**:

1. **Given** tracing is configured, **When** a run finishes, **Then** one trace exists, keyed by the run's run id, holding all of the run's steps.
2. **Given** an LLM call in the run, **When** it is viewed in the trace, **Then** it shows the model, input and output token counts, cost and latency.
3. **Given** a tool call fails, **When** it is viewed, **Then** it is marked as an error with the failure reason (with secrets redacted).
4. **Given** the tracing service is unreachable, **When** a run happens, **Then** the run still completes, the local trace file is still complete, and the export failure is reported on the console and in the run summary.

---

### User Story 3 - Read the run live on the console (Priority: P3)

While a run is in progress (in a terminal or in the workflow's execution log), the
maintainer sees one structured line per step: time, run id, step, tool, outcome and
duration. The same steps that appear in the trace appear on the console.

**Why this priority**: The console is the first place anyone looks, and it is the only
view available when the tracing service is down.

**Independent Test**: Run the crawler and compare its console lines with the local
trace file: every step appears in both, with the same run id.

**Acceptance Scenarios**:

1. **Given** a run, **When** each step finishes, **Then** one structured line is printed for it, carrying the run id.
2. **Given** a step fails, **When** it is logged, **Then** the line is at error level and names the failure reason.
3. **Given** the default output, **When** a person reads it, **Then** it is readable without tooling; a machine-readable form is available by setting.

---

### Edge Cases

- The workflow tool is down at the scheduled time: the run is missed. Missed runs are visible as a gap in the run history, and nothing catches them up automatically.
- The standing approval expires, or the config changes, between scheduled runs: the next run exits as not approved, spends nothing, and sends a notification (FR-015a) saying approval must be renewed.
- The run trigger is unreachable or rejects the token: the workflow records a failure. It does not retry silently.
- The crawler hangs: the workflow stops it after a maximum run time and records the timeout as a failure.
- Required secrets are missing from the workflow's environment: the crawler exits as a usage error naming the variable, never its value, and the workflow shows a failure.
- The tracing service is slow or rejects data: the run is not blocked or slowed past a bounded wait, and the dropped export is reported.
- A run stops at a budget limit (partial completeness): this is shown as its own outcome, not as success or as a crash.
- Budget approval: an unattended run has no one to type approval, so it uses the standing cap (FR-004). A run over the cap, or with no cap set, is refused before it spends anything, and the refusal shows as "not approved".

## Clarifications

### Session 2026-11-25

- Q: How is budget approval given for unattended runs? → A: A standing pre-approved budget cap, approved once by a person. Runs within the cap proceed, and runs over it are refused (FR-004).
- Q: Where does n8n run, and how does it start the crawler on the Mac? → A: Option B. n8n runs in Docker and calls a local trigger endpoint, which runs the crawler on the Mac. The endpoint's start/status contract is written so a later Kubernetes Job launcher can replace it. Containerizing the crawler (option D) is deferred to [todo.md](todo.md).
- Q: Should Langfuse store LLM prompt and response text, or only metadata? → A: Full text by default, with a config setting that switches to metadata-only (FR-010a/b).
- Q: Should a failed unattended run notify the maintainer? → A: Yes. One notification per run that doesn't complete, and none on success (FR-015a). The channel is chosen during planning.
- Q: How long does a standing budget approval stay valid? → A: 30 days, or less if the cap, the models or the prices change (FR-004).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The workflow MUST start the existing crawler command unchanged in behaviour; it MUST NOT choose exams, tools, retries or next steps (those stay in the crawler).
- **FR-001a**: The workflow MUST start runs only through a run trigger with a fixed contract: *start* returns a run id or a refusal reason, and *status* returns the run's outcome. The contract MUST NOT depend on where the crawler runs, so a container-based launcher can replace the local one without changing the workflow.
- **FR-001b**: The local run trigger MUST listen on the local machine only, MUST reject calls without a shared token (supplied by environment variable), and MUST do nothing except start the crawler command and report its status.
- **FR-002**: The workflow MUST support a schedule trigger and an on-demand trigger.
- **FR-003**: The workflow MUST allow at most one run at a time and record refused starts as skipped.
- **FR-004**: An unattended run MUST keep spec 003's human budget approval through a standing pre-approved budget cap. A person approves the cap once and records who approved it and when. The standing approval is stored in the pipeline database, not in a local file. It expires 30 days after it is given, and is cancelled earlier if the cap, the models or the model prices in the run config change. After that, a person must approve again. An unattended run proceeds only if its budget fits within the cap, and records the standing approval (approver and time) as its `approved_by`/`approved_at`. A run whose budget exceeds the cap, or that has no valid standing approval, exits as not approved and spends nothing. Interactive runs keep the TTY prompt unchanged.
- **FR-005**: The workflow MUST record each run's run id, start and end time, exit outcome (mapped from the crawler's exit codes) and the location of the run summary.
- **FR-005a**: The location of run artifacts (summary, local trace) MUST be configurable, defaulting to today's `results/crawler/<run_id>/`.
- **FR-006**: The workflow MUST stop a run that exceeds a maximum run time and record it as timed out.
- **FR-007**: Every crawler run MUST produce one trace, keyed by its run id, in the self-hosted observability tool when tracing is configured.
- **FR-008**: The trace MUST include every agent step, LLM call and tool call that the local trace file records, with the same parent/child nesting.
- **FR-009**: LLM calls in the trace MUST carry the model, token counts, cost and latency.
- **FR-010**: Trace data MUST carry no secret values; secrets are redacted before export.
- **FR-010a**: By default, LLM calls in the observability trace MUST include the full prompt and response text. A single configuration setting MUST switch to metadata-only mode, which exports only model, token counts, cost, latency, status and prompt/response hashes, the same content as the local trace. The mode in effect MUST be recorded in the run summary.
- **FR-010b**: Prompt and response text MUST go only to the self-hosted observability tool. It MUST NOT be written to the local trace file, console lines or any tracked repo file, so the repo's corpus guard keeps holding.
- **FR-011**: Tracing export MUST be optional. With no tracing endpoint set, nothing is exported, and runs end with the same outcomes, exit codes and stored results as before this feature. The console line format (FR-013) and the added `summary.json` fields apply whether or not tracing is set.
- **FR-012**: A failed or partial trace export MUST be reported on the console and recorded in the run summary; it MUST NOT fail the run.
- **FR-013**: The crawler MUST print one structured console line per step, with timestamp, level, run id, step, tool (if any), outcome and duration.
- **FR-014**: Console lines MUST be human-readable by default and machine-readable (one JSON object per line) by setting.
- **FR-015**: Errors MUST log at error level with the reason. No failure may be reported as success at any layer (console, trace, workflow).
- **FR-015a**: The workflow MUST send exactly one notification for each run that does not complete: failed, escalated, stopped at a limit, timed out, not approved, or run trigger unreachable. The notification names the run id, the outcome and the reason. Completed runs and skipped starts (FR-003) send no notification. If the notification itself fails to send, that is recorded in the workflow history.
- **FR-016**: All secrets MUST come from environment variables or the workflow tool's own credential store. Secrets MUST NOT be committed, printed or placed in workflow exports.
- **FR-017**: The workflow definition MUST be committed in the repo, with no secrets in it, so it can be reviewed and re-imported.
- **FR-018**: The experiment harness core (`harness/`) MUST NOT depend on the workflow or tracing tools. Like other optional integrations, they live in an optional extra.
- **FR-019**: Tests MUST run without the workflow tool, the tracing service or the network.

### Key Entities

- **Workflow Execution**: one trigger firing; has trigger type, start/end time, linked run id, outcome (completed, failed, escalated, stopped-at-limit, not-approved, timed-out, skipped).
- **Run Trace**: the observability tool's view of one crawler run; keyed by run id; contains spans for steps, LLM calls and tool calls.
- **Console Log Line**: one structured record per step; shares the run id and step name with the matching span.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: One run started by the workflow completes end to end, and the maintainer can find its trace by run id in under 1 minute.
- **SC-002**: 100% of the steps in that run's local trace file appear in the observability trace, and 100% appear as console lines.
- **SC-003**: In a forced-failure test (missing key, failing tool, unreachable tracing service, timeout), each failure shows up as a failure in the console, the workflow history and, where reachable, the trace, and produces exactly one notification. None of them is shown as success, and a completed run produces no notification.
- **SC-004**: With the tracing service unreachable, a run takes no more than 10% longer than with tracing off.
- **SC-005**: A secret scan of the repo, the workflow export, the console output and the trace data finds zero secret values.

## Assumptions

- The workflow tool and the observability tool both run self-hosted on the maintainer's machine (local containers). Hosted deployment is out of scope.
- "Agent #1" is the spec 003 crawler (`python -m crawler run`). Other agents and the experiment harness are not orchestrated in this feature.
- The crawler's existing OpenTelemetry spans are the source of the trace, taken in through a standard trace endpoint. In full-text mode, LLM spans also carry prompt/response text, but only on the export path; the local `trace.jsonl` stays metadata-only.
- Prompt text may contain fragments of copyrighted exam pages. That is acceptable only because the observability tool runs locally and its data is never committed or shared. Its data store is treated like the gitignored `results/` folder.
- The workflow tool runs in a container. The crawler runs on the host machine and is started through the local run trigger (FR-001a/b). Running the crawler in its own container, and later as Kubernetes Jobs next to XERJ, is a follow-up recorded in [todo.md](todo.md).- Default maximum run time is the crawler's own budget limit plus a margin; the exact value is set during planning.
- Missed schedule runs are not caught up; one missed night is acceptable for a research crawler.
- Live end-to-end checks spend money (search and LLM calls), so they are manual maintainer steps, as with spec 003's T074.
