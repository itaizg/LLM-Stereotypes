#!/usr/bin/env bash
# Start Dagster on this machine: `orchestration/dev.sh` from anywhere in the repo.
#
# It sets what a bare `dagster dev` lacks: the code location, and a persistent
# DAGSTER_HOME whose dagster.yaml holds the one-slot `pipeline_writer` pool (without
# it the pool is unlimited and writing steps could overlap). Extra arguments go to
# `dagster dev`, e.g. `orchestration/dev.sh -p 3001`. PIPELINE_DSN comes from your
# environment and is only needed once a step runs.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DAGSTER_HOME="$root/orchestration/dagster_home"

cd "$root"
exec uv run --extra pipeline --extra orchestration \
  dagster dev -m exam_dagster.definitions -d orchestration "$@"
