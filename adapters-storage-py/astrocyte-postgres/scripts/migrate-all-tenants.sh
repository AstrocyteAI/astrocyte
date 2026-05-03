#!/usr/bin/env bash
# Apply migrations to MULTIPLE Postgres schemas — one per tenant.
#
# Use cases:
# - Bringing up new tenants (one schema each)
# - Rolling out a new migration to every existing tenant
# - Running migrations against a shared dev DB with multiple test schemas
#
# Tenant discovery (in order of precedence):
# 1. ``--tenants`` argument: comma-separated schema names
#      ./migrate-all-tenants.sh --tenants tenant_acme,tenant_globex,tenant_initech
# 2. ``--tenants-file``: file with one schema per line (# comments allowed)
#      ./migrate-all-tenants.sh --tenants-file ./tenants.txt
# 3. ``ASTROCYTE_TENANTS`` env var: same comma-separated format as --tenants
# 4. Default: discover from the database itself by listing every schema
#    that already contains ``astrocyte_vectors`` (i.e. previously migrated
#    tenants) — useful for upgrade flows where you want to roll forward
#    every tenant that's been onboarded.
#
# Per-tenant migration is delegated to ``./migrate.sh SCHEMA=…`` so the
# behavior stays consistent with the single-tenant runner.
#
# Failure handling: by default, abort on the FIRST tenant that fails. Pass
# ``--continue-on-error`` to keep going (e.g. for upgrade rollouts where you
# don't want one bad tenant to block the rest); the exit code is the count
# of failed tenants in that mode.
#
# Usage examples:
#   ./scripts/migrate-all-tenants.sh --tenants tenant_acme,tenant_globex
#   ./scripts/migrate-all-tenants.sh --tenants-file infra/tenants.txt
#   ASTROCYTE_TENANTS=tenant_acme,tenant_globex ./scripts/migrate-all-tenants.sh
#   ./scripts/migrate-all-tenants.sh   # auto-discover from DB
#   ./scripts/migrate-all-tenants.sh --continue-on-error
#
# Requires the same env vars as ``migrate.sh``:
#   - DATABASE_URL (or pass --dsn)
#   - ASTROCYTE_EMBEDDING_DIMENSIONS (default 128)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATE_SCRIPT="${SCRIPT_DIR}/migrate.sh"

if [ ! -x "${MIGRATE_SCRIPT}" ]; then
  echo "error: ${MIGRATE_SCRIPT} not executable" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

TENANTS_ARG=""
TENANTS_FILE=""
DSN_ARG=""
CONTINUE_ON_ERROR=0

while [ $# -gt 0 ]; do
  case "$1" in
    --tenants)
      TENANTS_ARG="${2:-}"
      shift 2
      ;;
    --tenants-file)
      TENANTS_FILE="${2:-}"
      shift 2
      ;;
    --dsn)
      DSN_ARG="${2:-}"
      shift 2
      ;;
    --continue-on-error)
      CONTINUE_ON_ERROR=1
      shift
      ;;
    --help|-h)
      sed -n '/^# Apply/,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      echo "run with --help for usage" >&2
      exit 2
      ;;
  esac
done

# DSN comes from --dsn, env, or migrate.sh's own discovery.
CONN="${DSN_ARG:-${DATABASE_URL:-${ASTROCYTE_PG_DSN:-}}}"
if [ -z "${CONN}" ]; then
  echo "error: set DATABASE_URL or ASTROCYTE_PG_DSN, or pass --dsn" >&2
  exit 1
fi
export DATABASE_URL="${CONN}"

# ---------------------------------------------------------------------------
# Tenant discovery
# ---------------------------------------------------------------------------

discover_tenants_from_db() {
  # Lists every schema (excluding pg_/information_schema) that already has
  # an ``astrocyte_vectors`` table. This catches every schema that was
  # previously bootstrapped, even if the tenant directory file is stale.
  if ! command -v psql >/dev/null 2>&1; then
    echo "error: psql not found; install PostgreSQL client tools" >&2
    exit 1
  fi
  psql "${CONN}" -At -c "
    SELECT table_schema
      FROM information_schema.tables
     WHERE table_name = 'astrocyte_vectors'
       AND table_schema NOT IN ('pg_catalog','information_schema')
     ORDER BY table_schema
  "
}

split_csv() {
  # Split comma-separated string into one entry per line, trim whitespace.
  echo "$1" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' || true
}

read_tenants_file() {
  # One schema per line; `#` comments and blank lines ignored.
  grep -v '^[[:space:]]*#' "$1" | sed 's/^ *//;s/ *$//' | grep -v '^$' || true
}

if [ -n "${TENANTS_ARG}" ]; then
  TENANTS="$(split_csv "${TENANTS_ARG}")"
  source="--tenants"
elif [ -n "${TENANTS_FILE}" ]; then
  if [ ! -r "${TENANTS_FILE}" ]; then
    echo "error: --tenants-file ${TENANTS_FILE} not readable" >&2
    exit 1
  fi
  TENANTS="$(read_tenants_file "${TENANTS_FILE}")"
  source="--tenants-file ${TENANTS_FILE}"
elif [ -n "${ASTROCYTE_TENANTS:-}" ]; then
  TENANTS="$(split_csv "${ASTROCYTE_TENANTS}")"
  source="ASTROCYTE_TENANTS env"
else
  echo "Discovering tenants from database (schemas containing astrocyte_vectors)..."
  TENANTS="$(discover_tenants_from_db)"
  source="DB discovery"
fi

if [ -z "${TENANTS}" ]; then
  echo "error: no tenants found via ${source}" >&2
  exit 1
fi

# Validate every schema name BEFORE running anything.
INVALID=()
while IFS= read -r schema; do
  [ -n "${schema}" ] || continue
  if ! [[ "${schema}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    INVALID+=("${schema}")
  fi
done <<< "${TENANTS}"
if [ ${#INVALID[@]} -gt 0 ]; then
  echo "error: invalid schema name(s) — must match ^[a-zA-Z_][a-zA-Z0-9_]*$:" >&2
  for bad in "${INVALID[@]}"; do
    echo "  ${bad}" >&2
  done
  exit 1
fi

TENANT_COUNT=$(echo "${TENANTS}" | wc -l | tr -d ' ')
echo "Found ${TENANT_COUNT} tenant(s) via ${source}:"
echo "${TENANTS}" | sed 's/^/  - /'
echo

# ---------------------------------------------------------------------------
# Migration loop
# ---------------------------------------------------------------------------

SUCCEEDED=()
FAILED=()
INDEX=0

while IFS= read -r schema; do
  [ -n "${schema}" ] || continue
  INDEX=$((INDEX + 1))
  echo "===================================================================="
  echo "[${INDEX}/${TENANT_COUNT}] Migrating schema: ${schema}"
  echo "===================================================================="
  if SCHEMA="${schema}" "${MIGRATE_SCRIPT}"; then
    SUCCEEDED+=("${schema}")
  else
    FAILED+=("${schema}")
    if [ "${CONTINUE_ON_ERROR}" -eq 0 ]; then
      echo
      echo "error: migration failed for schema ${schema} — aborting." >&2
      echo "(Pass --continue-on-error to keep going past failures.)" >&2
      exit 1
    fi
    echo "WARNING: migration failed for ${schema}; continuing." >&2
  fi
  echo
done <<< "${TENANTS}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo "===================================================================="
echo "Multi-tenant migration summary"
echo "===================================================================="
echo "Succeeded: ${#SUCCEEDED[@]} / ${TENANT_COUNT}"
for s in "${SUCCEEDED[@]}"; do
  echo "  ✓ ${s}"
done
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "Failed: ${#FAILED[@]} / ${TENANT_COUNT}"
  for s in "${FAILED[@]}"; do
    echo "  ✗ ${s}"
  done
  exit ${#FAILED[@]}
fi
echo "All tenants migrated successfully."
