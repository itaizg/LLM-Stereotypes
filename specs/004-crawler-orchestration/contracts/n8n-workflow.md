# Contract: n8n workflow `crawler-run`

File: `orchestration/n8n/crawler-run.workflow.json`. It's exported **inactive** and **without credential values** (FR-017). n8n only maps outcomes; it never decides which exams, tools or retries the crawler uses (FR-001; team constitution §2).

## Credentials (n8n credential store, referenced by name only)

| Name | Type | Holds |
|---|---|---|
| `crawler-trigger-token` | Header Auth | `Authorization: Bearer <CRAWLER_TRIGGER_TOKEN>` |
| `crawler-webhook-auth` | Header Auth | the token that on-demand callers must send to the webhook |
| `crawler-notify-smtp` | SMTP | email account for notifications (R10) |

## Nodes

1. **Schedule** (`0 2 * * *`) and **Webhook** (`POST /webhook/crawler-run`, header auth) both lead to:
2. **Start run**: HTTP Request `POST http://host.docker.internal:8765/runs`, timeout 30 s, with "continue on error" on.
3. **Route start response**: a Switch on the HTTP status.
   - `202` → step 4.
   - `409` → **End (skipped)**: no notification (FR-003, FR-015a).
   - `403`/`422`/`200` with a non-completed outcome → **Notify**.
   - Connection error or `401`/`5xx` → **Notify** with outcome `trigger_unreachable`.
   - For the webhook path, **Respond to Webhook** returns `{run_id, outcome}` right after this step (US1 scenario 2).
4. **Poll**: Wait 60 s, then `GET /runs/{run_id}`. Loop while `status == "running"` and elapsed time < `deadline_at` + 15 min. If that's exceeded, the outcome is `timed_out`.
5. **Route outcome**: `completed` → **End**. Anything else → **Notify**.
6. **Notify**: Send Email with subject `[crawler] <outcome> <run_id>`. The body has the outcome, the reason, `cost_usd`, `summary_path` and the Langfuse session link `http://localhost:3000/project/<id>/sessions/<run_id>`. If sending fails, the node errors, so the execution is marked failed in n8n history (FR-015a).

## Rules checked by `tests/crawler/test_orchestration_files.py`

- The JSON has no `credentials` values, only names (no token-like strings, no `Bearer` with a value).
- Every HTTP Request node targets `http://host.docker.internal:8765/…`.
- `active` is `false`.
- Every outcome except `completed` and `skipped` has a path to Notify.
