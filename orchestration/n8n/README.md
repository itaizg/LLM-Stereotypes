# Crawler orchestration (spec 004)

n8n starts the crawler on a schedule or on demand. It calls a small local run trigger
and maps outcomes; it never decides what the crawler does. Full steps and expected
results: [specs/004-crawler-orchestration/quickstart.md](../../specs/004-crawler-orchestration/quickstart.md).

1. **Give the standing approval** (once per 30 days, or after a model or price change):
   `uv run python -m crawler approve`. Revoke it with `--revoke`.
2. **Start the trigger** on the host, with `CRAWLER_TRIGGER_TOKEN` (32+ characters) and
   the keys `crawler run` needs in the environment:
   `uv run python -m crawler trigger`. It listens on `127.0.0.1:8765` only.
3. **Start n8n**: copy `.env.example` to `.env`, set `N8N_ENCRYPTION_KEY`, then
   `docker compose -f orchestration/n8n/docker-compose.yaml up -d`.
4. **Import** `crawler-run.workflow.json` in n8n and create the three credentials it
   names: `crawler-trigger-token` (Header Auth, `Authorization: Bearer <token>`),
   `crawler-webhook-auth` (Header Auth) and `crawler-notify-smtp` (SMTP).
5. **Activate** the workflow. It runs at 02:00 and on `POST /webhook/crawler-run`.
   Every outcome except *completed* and *skipped* sends one email.

The workflow is exported inactive and without credential values.
