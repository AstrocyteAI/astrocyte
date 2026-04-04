#!/usr/bin/env bash
# Apply SQL migrations in astrocytes-pgvector/migrations/ using psql (no Python).
#
# Usage:
#   export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
#   ./scripts/migrate.sh
#
# Or pass a connection URI as the first argument:
#   ./scripts/migrate.sh 'postgresql://...'
#
# Requires: psql (PostgreSQL client), on PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS_DIR="$(cd "${SCRIPT_DIR}/../migrations" && pwd)"

if [ -n "${1:-}" ]; then
  export DATABASE_URL="$1"
fi

CONN="${DATABASE_URL:-${ASTROCYTES_PG_DSN:-}}"
if [ -z "${CONN}" ]; then
  echo "error: set DATABASE_URL or ASTROCYTES_PG_DSN, or pass a URI as the first argument" >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "error: psql not found; install PostgreSQL client tools" >&2
  exit 1
fi

found=0
while IFS= read -r f; do
  [ -n "${f}" ] || continue
  found=1
  echo "Applying $(basename "$f")..."
  psql "${CONN}" -v ON_ERROR_STOP=1 -f "$f"
done < <(find "${MIGRATIONS_DIR}" -maxdepth 1 -type f -name '[0-9][0-9][0-9]_*.sql' | sort)

if [ "${found}" -eq 0 ]; then
  echo "error: no migrations found in ${MIGRATIONS_DIR}" >&2
  exit 1
fi

echo "Migrations finished."
