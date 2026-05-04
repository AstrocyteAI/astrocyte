#!/usr/bin/env bash
# One-shot data migration: copy wiki-piggyback mental-model rows into the
# first-class astrocyte_mental_models table.
#
# Background:
#   Before M9, mental models lived in astrocyte_wiki_pages with
#   ``kind='concept'`` and ``metadata->>'_mental_model' = 'true'``.
#   M9 introduces a dedicated astrocyte_mental_models table with proper
#   revision history. This script copies any existing piggyback rows
#   into the new table so deployments that have been using the prior
#   /v1/mental-models endpoints don't lose data on upgrade.
#
#   Idempotent: re-running is safe — the INSERT uses ON CONFLICT DO NOTHING
#   so already-migrated rows are skipped.
#
#   Single-tenant: pass SCHEMA=public (the default).
#   Multi-tenant: loop over your tenant directory, e.g.
#     for s in tenant_acme tenant_globex; do
#         SCHEMA=$s ./migrate-wiki-piggyback-to-mental-models.sh
#     done
#
#   Source rows are NOT deleted — operators can verify the migration,
#   then drop the old wiki rows manually with:
#     DELETE FROM "<schema>".astrocyte_wiki_pages
#      WHERE kind = 'concept' AND metadata->>'_mental_model' = 'true';
#
# Usage:
#   export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
#   SCHEMA=public ./scripts/migrate-wiki-piggyback-to-mental-models.sh

set -euo pipefail

CONN="${DATABASE_URL:-${ASTROCYTE_PG_DSN:-}}"
if [ -z "${CONN}" ]; then
  echo "error: set DATABASE_URL or ASTROCYTE_PG_DSN" >&2
  exit 1
fi

SCHEMA="${SCHEMA:-public}"
if ! [[ "${SCHEMA}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  echo "error: SCHEMA must match ^[a-zA-Z_][a-zA-Z0-9_]*$ (got: ${SCHEMA})" >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "error: psql not found; install PostgreSQL client tools" >&2
  exit 1
fi

# Verify the target table exists (i.e., migration 012 has run).
EXISTS=$(psql "${CONN}" -Atc "
  SELECT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = '${SCHEMA}' AND table_name = 'astrocyte_mental_models'
  )
")
if [ "${EXISTS}" != "t" ]; then
  echo "error: ${SCHEMA}.astrocyte_mental_models does not exist." >&2
  echo "       Run SCHEMA=${SCHEMA} ./migrate.sh first to apply migration 012." >&2
  exit 1
fi

# Count source rows for visibility.
SOURCE_COUNT=$(psql "${CONN}" -Atc "
  SELECT COUNT(*) FROM \"${SCHEMA}\".astrocyte_wiki_pages
  WHERE kind = 'concept'
    AND metadata->>'_mental_model' = 'true'
    AND deleted_at IS NULL
")

if [ "${SOURCE_COUNT}" -eq 0 ]; then
  echo "No wiki-piggyback mental-model rows found in schema \"${SCHEMA}\". Nothing to migrate."
  exit 0
fi

echo "Found ${SOURCE_COUNT} wiki-piggyback mental-model row(s) in \"${SCHEMA}\"."
echo "Copying into \"${SCHEMA}\".astrocyte_mental_models (ON CONFLICT DO NOTHING)..."

# Copy: page_id → model_id, take latest revision per page from
# astrocyte_wiki_revisions, default scope to 'bank' if not set.
psql "${CONN}" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO "${SCHEMA}".astrocyte_mental_models
    (bank_id, model_id, title, content, scope, source_ids, revision,
     metadata, refreshed_at, created_at)
SELECT
    p.bank_id,
    p.page_id AS model_id,
    p.title,
    COALESCE(r.markdown, '') AS content,
    COALESCE(NULLIF(p.scope, ''), 'bank') AS scope,
    COALESCE(
        ARRAY(
            SELECT memory_id FROM "${SCHEMA}".astrocyte_wiki_revision_sources s
            WHERE s.revision_id = p.current_revision_id
        ),
        '{}'::text[]
    ) AS source_ids,
    COALESCE(
        (SELECT MAX(revision_number) FROM "${SCHEMA}".astrocyte_wiki_revisions r2
         WHERE r2.page_uuid = p.id),
        1
    ) AS revision,
    -- Strip the legacy _mental_model / _freshness discriminators —
    -- they're encoded by table identity now.
    (p.metadata - '_mental_model' - '_freshness') AS metadata,
    p.updated_at AS refreshed_at,
    p.created_at
FROM "${SCHEMA}".astrocyte_wiki_pages p
LEFT JOIN "${SCHEMA}".astrocyte_wiki_revisions r
    ON r.id = p.current_revision_id
WHERE p.kind = 'concept'
  AND p.metadata->>'_mental_model' = 'true'
  AND p.deleted_at IS NULL
ON CONFLICT (bank_id, model_id) DO NOTHING;
SQL

NEW_COUNT=$(psql "${CONN}" -Atc "
  SELECT COUNT(*) FROM \"${SCHEMA}\".astrocyte_mental_models
")

echo "Done. \"${SCHEMA}\".astrocyte_mental_models now contains ${NEW_COUNT} row(s)."
echo
echo "To drop the old wiki-piggyback rows after verification:"
echo "  DELETE FROM \"${SCHEMA}\".astrocyte_wiki_pages"
echo "   WHERE kind = 'concept' AND metadata->>'_mental_model' = 'true';"
