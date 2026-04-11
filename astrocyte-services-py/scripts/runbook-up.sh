#!/usr/bin/env bash
# One-shot runbook: Postgres → SQL migrations (psql) → astrocyte-rest with runbook config.
# Run from astrocyte-services-py/:  ./scripts/runbook-up.sh
# Or from repo root:  ./astrocyte-services-py/scripts/runbook-up.sh
#
# Requires: docker, docker compose, psql (PostgreSQL client), optional .env (see .env.example).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  . ./.env
  set +a
fi

POSTGRES_USER="${POSTGRES_USER:-astrocyte}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-astrocyte}"
POSTGRES_DB="${POSTGRES_DB:-astrocyte}"
POSTGRES_PUBLISH_PORT="${POSTGRES_PUBLISH_PORT:-5433}"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker not found" >&2
  exit 1
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "error: psql not found; install PostgreSQL client tools" >&2
  exit 1
fi

echo "Starting Postgres..."
docker compose up -d postgres

echo "Waiting for Postgres to accept connections..."
ready=0
for _ in $(seq 1 90); do
  if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "error: Postgres did not become ready in time" >&2
  exit 1
fi

# Host URL for migrate.sh (not the in-cluster postgres:5432 hostname).
if [ -n "${MIGRATE_DATABASE_URL:-}" ]; then
  MIGRATE_URL="$MIGRATE_DATABASE_URL"
else
  MIGRATE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PUBLISH_PORT}/${POSTGRES_DB}"
fi

echo "Applying SQL migrations..."
( export DATABASE_URL="$MIGRATE_URL" && ../adapters-py/astrocyte-pgvector/scripts/migrate.sh )

echo "Starting full stack (runbook config)..."
docker compose -f docker-compose.yml -f docker-compose.runbook.yml up --build -d

echo "Done. Health: http://127.0.0.1:${ASTROCYTES_HTTP_PUBLISH_PORT:-8080}/health"
