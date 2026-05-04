---
title: Mental models (first-class)
---

# Mental models — first-class storage

Mental models are **curated, refreshable saved-reflect summaries** — the
durable artifacts that outlive any single recall and serve as authoritative
summaries when the recall pipeline elects to use the compiled layer.
Examples: "Caroline prefers async updates", "Project X status: blocked on
review", "User's stated tone preference: concise + technical".

This page documents the M9 architecture (dedicated SPI + Postgres table)
and the migration path from the prior wiki-piggyback design.

## Quick start

```yaml
# astrocyte.yaml
mental_model_store: postgres
mental_model_store_config:
  bootstrap_schema: false   # production: trust 012_mental_models.sql migration
```

```bash
# Apply migrations (single tenant — see schema-per-tenant.md for multi).
SCHEMA=public ./adapters-storage-py/astrocyte-postgres/scripts/migrate.sh

# Then the gateway endpoints work end-to-end:
curl -X POST http://localhost:8080/v1/mental-models \
  -H 'Content-Type: application/json' \
  -d '{"bank_id":"bank-1","model_id":"model:alice","title":"Alice","content":"Prefers async updates"}'
```

## Why a first-class SPI

Pre-M9, mental models piggybacked on `WikiStore` via `kind="concept"` and a
`metadata["_mental_model"] = True` discriminator. That pattern overloaded
the wiki layer's lifecycle (revisions, lint issues, cross_links) for a
fundamentally different concept and required undocumented metadata-key
conventions. M9 cuts to a dedicated table and SPI:

| Concern | Wiki-piggyback (pre-M9) | First-class (M9) |
|---|---|---|
| Storage | `astrocyte_wiki_pages` rows | Dedicated `astrocyte_mental_models` table |
| Discriminator | `metadata["_mental_model"]` | Table identity |
| Revision history | Wiki revisions table (shared with topics/entities) | Dedicated `astrocyte_mental_model_versions` table |
| Lifecycle ops | Mixed with wiki ops (lint, cross-links) | Independent |
| Future extension (e.g. `max_tokens`, `last_refreshed_source_query` à la Hindsight) | More metadata sprawl | Add a column |
| Underscore-key namespace | Polluted | Clean |

## Architecture

### Layers

```
HTTP request                          Python API
    │                                       │
    ▼                                       ▼
┌──────────────────────────────┐    ┌──────────────────────────────┐
│ /v1/mental-models endpoints  │    │ MentalModelService(store)    │
│  (gateway app.py)            │    │   in-process consumer        │
└──────────────────────────────┘    └──────────────────────────────┘
              │                                  │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │ MentalModelService           │
              │  (pipeline/mental_model.py)  │
              └──────────────────────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │ MentalModelStore (SPI)       │
              │  (provider.py)               │
              └──────────────────────────────┘
                             │
            ┌────────────────┴───────────────────────┐
            ▼                                        ▼
┌─────────────────────────┐         ┌────────────────────────────┐
│ InMemoryMentalModelStore│         │ PostgresMentalModelStore   │
│  (testing/in_memory.py) │         │  (astrocyte_postgres pkg)  │
└─────────────────────────┘         └────────────────────────────┘
```

### Schema (Postgres)

```sql
CREATE TABLE astrocyte_mental_models (
    bank_id      TEXT       NOT NULL,
    model_id     TEXT       NOT NULL,
    title        TEXT       NOT NULL,
    content      TEXT       NOT NULL,
    scope        TEXT       NOT NULL DEFAULT 'bank',
    source_ids   TEXT[]     NOT NULL DEFAULT '{}'::text[],
    revision     INTEGER    NOT NULL DEFAULT 1,
    metadata     JSONB      NOT NULL DEFAULT '{}'::jsonb,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at   TIMESTAMPTZ,
    PRIMARY KEY (bank_id, model_id)
);

-- Per-revision history for changelog / diff (Hindsight has the equivalent).
CREATE TABLE astrocyte_mental_model_versions (
    id          BIGSERIAL    PRIMARY KEY,
    bank_id     TEXT         NOT NULL,
    model_id    TEXT         NOT NULL,
    revision    INTEGER      NOT NULL,
    title       TEXT         NOT NULL,
    content     TEXT         NOT NULL,
    source_ids  TEXT[]       NOT NULL DEFAULT '{}'::text[],
    metadata    JSONB        NOT NULL DEFAULT '{}'::jsonb,
    archived_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, model_id, revision),
    FOREIGN KEY (bank_id, model_id)
        REFERENCES astrocyte_mental_models(bank_id, model_id) ON DELETE CASCADE
);
```

Hot-path indexes (defined in migration 012):
- `(bank_id, scope) WHERE deleted_at IS NULL` — list by scope
- `(bank_id, refreshed_at DESC) WHERE deleted_at IS NULL` — recency listing
- `(bank_id, model_id, revision DESC)` on versions — history lookup

### Lifecycle invariants

- **`upsert(model, bank_id) -> int`** — creates if missing, refreshes if
  present. Returns the new revision number. On refresh, the prior
  current-revision row is archived into `astrocyte_mental_model_versions`
  before overwrite. Atomic in a single transaction.
- **`get(model_id, bank_id)`** — returns current revision; filters out
  soft-deleted rows.
- **`list(bank_id, scope=None)`** — orders by `refreshed_at DESC`.
- **`delete(model_id, bank_id)`** — soft-delete (`SET deleted_at = NOW()`).
  `get` and `list` filter it out. Revision history rows in the versions
  table remain for audit (NOT removed by soft-delete).
- **Hard delete** (CASCADE removal of versions) requires manual SQL —
  intentional, so audit trail isn't accidentally destroyed.

## Migration from wiki-piggyback (existing deployments)

Deployments running against gateway versions before M9 may have mental
models stored in `astrocyte_wiki_pages` with the legacy discriminator.
A one-shot data migration script copies them into the new table:

```bash
# Single tenant
DATABASE_URL=postgresql://... \
  SCHEMA=public \
  ./adapters-storage-py/astrocyte-postgres/scripts/migrate-wiki-piggyback-to-mental-models.sh

# Multi-tenant — loop over your tenants
for s in tenant_acme tenant_globex; do
  SCHEMA=$s ./adapters-storage-py/astrocyte-postgres/scripts/migrate-wiki-piggyback-to-mental-models.sh
done
```

The script:

- Validates SCHEMA against the safe-identifier regex
- Confirms the target table exists (errors if migration 012 hasn't run)
- Copies rows where `kind = 'concept'` AND `metadata->>'_mental_model' = 'true'`
  AND `deleted_at IS NULL` from `astrocyte_wiki_pages` into `astrocyte_mental_models`
- Preserves: `page_id → model_id`, `title`, `content` (latest revision),
  `scope`, `source_ids`, `revision`, `created_at`
- Strips: legacy `_mental_model` and `_freshness` discriminators from metadata
- Source rows are NOT deleted — verify the migration first, then drop
  manually with the SQL the script prints

Idempotent: re-running uses `ON CONFLICT (bank_id, model_id) DO NOTHING`,
so already-migrated rows are skipped.

After verification, drop the legacy rows:

```sql
DELETE FROM "<schema>".astrocyte_wiki_pages
 WHERE kind = 'concept' AND metadata->>'_mental_model' = 'true';
```

## Building a custom store

The SPI is small. To plug in a non-Postgres backend (Redis, DynamoDB,
SQLite, etc.):

```python
from astrocyte.provider import MentalModelStore
from astrocyte.types import MentalModel, HealthStatus


class MyMentalModelStore(MentalModelStore):
    SPI_VERSION = 1

    async def upsert(self, model: MentalModel, bank_id: str) -> int:
        ...  # Return the new revision number

    async def get(self, model_id: str, bank_id: str) -> MentalModel | None:
        ...

    async def list(self, bank_id: str, *, scope: str | None = None) -> list[MentalModel]:
        ...

    async def delete(self, model_id: str, bank_id: str) -> bool:
        ...

    async def health(self) -> HealthStatus:
        ...
```

Register via Python entry points in your package's `pyproject.toml`:

```toml
[project.entry-points."astrocyte.mental_model_stores"]
my_backend = "my_package.store:MyMentalModelStore"
```

Then in YAML:

```yaml
mental_model_store: my_backend
mental_model_store_config:
  # whatever your __init__ takes
```

## Schema-per-tenant interactions

`PostgresMentalModelStore` is fully tenant-aware via `astrocyte.tenancy.fq_table`:
all SQL routes through `self._fq()` so the active tenant's schema
determines which table the operation hits. See
[`schema-per-tenant.md`](./schema-per-tenant.md) for the broader
architecture.

Per-tenant migration:

```bash
SCHEMA=tenant_acme ./adapters-storage-py/astrocyte-postgres/scripts/migrate.sh
# OR for batch onboarding:
./adapters-storage-py/astrocyte-postgres/scripts/migrate-all-tenants.sh \
    --tenants tenant_acme,tenant_globex
```

The 4 isolation tests in
`adapters-storage-py/astrocyte-postgres/tests/test_postgres_mental_model_tenant_isolation.py`
prove writes / reads / lists / deletes / version history all stay
within the tenant's schema.

## API endpoints

The HTTP surface is unchanged from the pre-M9 design — only the storage
backend changes. All endpoints route through `brain._policy.check_access`
for the same RBAC as `/v1/recall`, `/v1/retain`, `/v1/forget`:

| Endpoint | Permission | Notes |
|---|---|---|
| `POST /v1/mental-models` | `write` | Create or refresh |
| `GET /v1/mental-models?bank_id=...` | `read` | List, optional `scope=` filter |
| `GET /v1/mental-models/{id}?bank_id=...` | `read` | Get current revision |
| `POST /v1/mental-models/{id}/refresh` | `write` | Update content; preserves source_ids if omitted |
| `DELETE /v1/mental-models/{id}?bank_id=...` | `forget` | Soft-delete |

All return `501 Not Implemented` if `mental_model_store` is unset in
config — operators must explicitly opt in.

## What's deferred (not in M9)

- **`structured_content` JSONB column** for non-textual mental models
  (Hindsight has this; we don't yet)
- **`max_tokens` / `last_refreshed_source_query` columns** for
  refresh-budgeting workflows
- **Auto-refresh hook** triggered by retain — currently mental models
  are explicitly created/refreshed by callers
- **Vector-indexed retrieval over mental models** — they're searchable
  by `bank_id + model_id` only today; semantic recall over them would
  need an embedding column + HNSW index (similar to wiki page embeddings)

These are future-work items, none blocking M9 ship.
