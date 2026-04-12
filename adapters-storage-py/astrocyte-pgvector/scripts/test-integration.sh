#!/usr/bin/env bash
# Run astrocyte-pgvector integration tests against a disposable PostgreSQL + pgvector container.
#
# Usage:
#   ./scripts/test-integration.sh            # start container, run tests, stop container
#   ./scripts/test-integration.sh --keep     # keep container running after tests (for debugging)
#
# Requires: docker, uv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONTAINER_NAME="astrocyte-pgvector-test"
PG_PORT="${PG_TEST_PORT:-5434}"
PG_USER="test"
PG_PASS="test"
PG_DB="test"
KEEP=false
PYTEST_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=true ;;
    *) PYTEST_ARGS+=("$arg") ;;
  esac
done

cleanup() {
  if [ "$KEEP" = false ]; then
    echo "Stopping test container..."
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  else
    echo "Container '$CONTAINER_NAME' kept running on port $PG_PORT"
  fi
}
trap cleanup EXIT

# Start container (remove any stale one first)
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
echo "Starting pgvector container on port $PG_PORT..."
docker run -d --name "$CONTAINER_NAME" \
  -e POSTGRES_USER="$PG_USER" \
  -e POSTGRES_PASSWORD="$PG_PASS" \
  -e POSTGRES_DB="$PG_DB" \
  -p "$PG_PORT:5432" \
  pgvector/pgvector:pg16 >/dev/null

# Wait for postgres to be ready
echo -n "Waiting for PostgreSQL"
for i in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" pg_isready -U "$PG_USER" >/dev/null 2>&1; then
    echo " ready."
    break
  fi
  echo -n "."
  sleep 1
done

if ! docker exec "$CONTAINER_NAME" pg_isready -U "$PG_USER" >/dev/null 2>&1; then
  echo " timed out."
  exit 1
fi

# Run tests
export DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@localhost:${PG_PORT}/${PG_DB}"
cd "$PKG_DIR"
uv sync --extra dev
uv run python -m pytest -v ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
