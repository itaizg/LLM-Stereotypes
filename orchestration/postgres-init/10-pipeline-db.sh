#!/usr/bin/env bash
# Runs once, on the first start of an empty data volume (the official postgres image
# executes everything in /docker-entrypoint-initdb.d, as the superuser).
#
# One server, two databases, and neither role can open the other's database, so
# Dagster cannot write pipeline tables even by mistake (spec 002 FR-016).
# Passwords reach SQL as psql variables, never by string-building (CDR-2026-021), and
# are read from the environment by psql itself (\getenv), so they are never in the
# command line of a process.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
\getenv dagster_pw DAGSTER_DB_PASSWORD
\getenv pipeline_pw PIPELINE_DB_PASSWORD
CREATE ROLE dagster  LOGIN PASSWORD :'dagster_pw';
CREATE ROLE pipeline LOGIN PASSWORD :'pipeline_pw';
CREATE DATABASE dagster  OWNER dagster;
CREATE DATABASE pipeline OWNER pipeline;
REVOKE CONNECT ON DATABASE dagster  FROM PUBLIC;
REVOKE CONNECT ON DATABASE pipeline FROM PUBLIC;
SQL
