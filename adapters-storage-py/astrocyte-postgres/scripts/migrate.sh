#!/usr/bin/env bash
# Apply SQL migrations in astrocyte-postgres/migrations/ using psql (no Python).
#
# Usage (single-tenant — schema = 'public'):
#   export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
#   ./scripts/migrate.sh
#
# Usage (multi-tenant — one schema per tenant):
#   export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
#   SCHEMA=tenant_acme   ./scripts/migrate.sh
#   SCHEMA=tenant_globex ./scripts/migrate.sh
#
# Or pass a connection URI as the first argument:
#   ./scripts/migrate.sh 'postgresql://...'
#
# Schema requirements:
#   - SCHEMA must match ^[a-zA-Z_][a-zA-Z0-9_]*$ (validated locally; no SQL escape).
#   - The schema is created if it doesn't exist (CREATE SCHEMA IF NOT EXISTS).
#   - The migration runner sets ``search_path`` to "<schema>", public so that
#     unqualified table references in the SQL files resolve to the tenant's
#     schema while extensions in `public` (pgvector, pg_trgm, etc.) stay
#     reachable.
#
# Requires: psql (PostgreSQL client), on PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS_DIR="$(cd "${SCRIPT_DIR}/../migrations" && pwd)"

if [ -n "${1:-}" ]; then
  export DATABASE_URL="$1"
fi

CONN="${DATABASE_URL:-${ASTROCYTE_PG_DSN:-}}"
if [ -z "${CONN}" ]; then
  echo "error: set DATABASE_URL or ASTROCYTE_PG_DSN, or pass a URI as the first argument" >&2
  exit 1
fi

EMBEDDING_DIMENSIONS="${ASTROCYTE_EMBEDDING_DIMENSIONS:-${EMBEDDING_DIMENSIONS:-128}}"
if ! [[ "${EMBEDDING_DIMENSIONS}" =~ ^[0-9]+$ ]] || [ "${EMBEDDING_DIMENSIONS}" -lt 1 ]; then
  echo "error: ASTROCYTE_EMBEDDING_DIMENSIONS must be a positive integer" >&2
  exit 1
fi

# Schema selection (defaults to ``public`` for backward compatibility).
SCHEMA="${SCHEMA:-public}"
if ! [[ "${SCHEMA}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  echo "error: SCHEMA must match ^[a-zA-Z_][a-zA-Z0-9_]*$ (got: ${SCHEMA})" >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "error: psql not found; install PostgreSQL client tools" >&2
  exit 1
fi

# Ensure the target schema exists, then bind the session search_path so
# every migration's unqualified DDL lands in the right schema.
if [ "${SCHEMA}" != "public" ]; then
  echo "Creating schema \"${SCHEMA}\" if it does not exist..."
  psql "${CONN}" -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS \"${SCHEMA}\""
fi

found=0
while IFS= read -r f; do
  [ -n "${f}" ] || continue
  found=1
  echo "Applying $(basename "$f")  [schema=${SCHEMA}]..."
  # ``-c "SET search_path = ..."`` runs before the file's SQL so any bare
  # identifiers in the migration resolve to the tenant's schema. ``public``
  # stays on the path so pgvector / pg_trgm / pgcrypto types remain visible.
  psql "${CONN}" \
    -v ON_ERROR_STOP=1 \
    -v "embedding_dimensions=${EMBEDDING_DIMENSIONS}" \
    -c "SET search_path = \"${SCHEMA}\", public" \
    -f "$f"
done < <(find "${MIGRATIONS_DIR}" -maxdepth 1 -type f -name '[0-9][0-9][0-9]_*.sql' | sort)

if [ "${found}" -eq 0 ]; then
  echo "error: no migrations found in ${MIGRATIONS_DIR}" >&2
  exit 1
fi

echo "Migrations finished for schema \"${SCHEMA}\"."
