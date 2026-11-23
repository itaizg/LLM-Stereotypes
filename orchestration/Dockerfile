# The one image every Dagster service runs in (specs/002-airflow-orchestration,
# contracts/stack.md). Build context is the repository root.
#
# One environment: the locked project with the `pipeline` and `orchestration` extras.
# Dagster runs the pipeline as `python -m pipeline` in that same interpreter.

FROM python:3.13-slim

# uv is pinned here, not taken from the base image.
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

# curl: the compose healthcheck. tesseract: the pipeline's OCR fallback needs the binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# The venv lives outside the mounted repository, so the Mac's checkout never holds a
# Linux environment. uv must use the image's Python, never download one.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/opt/uv-cache \
    PATH=/opt/venv/bin:$PATH \
    PIPELINE_PROJECT=/opt/project \
    DAGSTER_HOME=/opt/dagster_home

# Only the dependency manifests are copied: the project itself is mounted at
# /opt/project at run time, so editing code needs no rebuild. It is never installed:
# the pipeline runs with /opt/project as its working directory, and Dagster finds
# exam_dagster through the workspace's working_directory.
WORKDIR /opt/build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra pipeline --extra orchestration --no-install-project

# Compose runs the containers as the Mac user's id, which is not the user that built the
# image. Group 0 is shared with it, so the venv, the cache and DAGSTER_HOME are writable
# by the group: Dagster writes its local files there.
RUN mkdir -p /opt/dagster_home \
    && chmod -R g+rwX /opt/venv /opt/uv-cache /opt/dagster_home

WORKDIR /opt/project
