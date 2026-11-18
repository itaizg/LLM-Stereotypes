# Contract: Local Dagster stack

*(Replaces the Airflow stack contract; see [research.md](../research.md) D1–D5.)*

Two ways to run, with the same code location. The pool keeps one writer at a time in both.

## A. `dagster dev` on the Mac

```bash
uv sync --extra pipeline --extra orchestration
export PIPELINE_DSN="postgresql://pipeline:<password>@localhost:5432/pipeline"
DAGSTER_HOME=$PWD/orchestration/dagster_home \
  uv run dagster dev -m exam_dagster.definitions -d orchestration
```

UI at <http://127.0.0.1:3000>. `dagster dev` starts the web server and the daemon in one
process. Run history lives in SQLite under `orchestration/dagster_home/` (gitignored; only
`dagster.yaml` is tracked). The database for `PIPELINE_DSN` can be the compose `postgres`
service alone (`docker compose … up -d postgres`) or any Postgres you already run.

## B. Docker compose

```bash
cp orchestration/.env.example orchestration/.env   # once; then fill in the passwords
docker compose -f orchestration/docker-compose.yaml up -d --build
docker compose -f orchestration/docker-compose.yaml down        # keeps data volumes
```

### Image (`orchestration/Dockerfile`, build context = repository root)

`python:3.13-slim`; `uv` 0.12.1; `curl` (healthcheck) and `tesseract-ocr`; `/opt/venv`, built by
`uv sync --locked --extra pipeline --extra orchestration --no-install-project` from
`pyproject.toml` and `uv.lock`. One environment: Dagster and the pipeline share it. The
project is mounted at `/opt/project` and never installed: the pipeline runs with that
folder as its working directory, and the workspace puts `orchestration/` on the path.
`UV_PROJECT_ENVIRONMENT=/opt/venv`, `PIPELINE_PROJECT=/opt/project`, `DAGSTER_HOME=/opt/dagster_home`.
The venv and `DAGSTER_HOME` are group-writable, because compose runs the containers as
`${DAGSTER_UID}:0`, which is not the user that built the image.

### Services

| Service | Ports (host) | Notes |
|---------|--------------|-------|
| `postgres` (`postgres:17.6`) | `127.0.0.1:5432` | Creates the role and database `dagster` and the role and database `pipeline`. Each role owns only its database; `CONNECT` is revoked from `PUBLIC`. |
| `dagster-init` | — | `dagster instance migrate`; exits 0. |
| `dagster-webserver` | `127.0.0.1:3000` | UI and GraphQL API. Workspace `docker/workspace.yaml`. |
| `dagster-daemon` | — | Executes backfills and the run queue. Holds `PIPELINE_DSN` and `ANTHROPIC_API_KEY`: the only service that does, because the runs it launches run the pipeline. |

`docker/dagster.yaml` sets Postgres run storage, `QueuedRunCoordinator`, and
`concurrency.pools.default_limit: 1`. There is no separate user-code server, because the code
location is a module in the same image.

### Environment file (`orchestration/.env`, gitignored; `.env.example` committed)

| Variable | Purpose |
|----------|---------|
| `DAGSTER_UID` | Mac user id (`id -u`), so files written to the mounted repository belong to the maintainer |
| `DAGSTER_DB_PASSWORD` | password for the `dagster` role |
| `PIPELINE_DB_PASSWORD` | password for the `pipeline` role |
| `POSTGRES_ADMIN_PASSWORD` | superuser, used only by the init script. Without a separate superuser the `dagster` role would be one, and a superuser ignores `CONNECT` revokes. |
| `ANTHROPIC_API_KEY` | optional; passed to the daemon for `translate` once that stage exists |

## Ports and other stacks

The stack publishes `127.0.0.1:5432` (Postgres) and `127.0.0.1:3000` (the UI). Another local
stack on the same ports must be stopped first. The UI has no login: there is one user and the
port is loopback-only.

## Guarantees

- Only loopback (`127.0.0.1`) ports are published.
- No secret is baked into the image or written to a committed file.
- Dagster's run storage holds counts and ids, never corpus text (FR-014).
- `docker compose down` keeps the `pgdata` volume. `down -v` deletes both databases; the
  pipeline database can be rebuilt with `pipeline rebuild` (spec 001 FR-020).
- The repository's `.venv` is mounted but never used inside containers.
