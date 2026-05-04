---
title: Source documents and chunks
---

# Source documents and chunks (M10)

Pre-M10, retained memories were anonymous flat rows in `astrocyte_vectors`
with no record of where they came from. Source-aware retain adds an
optional three-layer hierarchy that preserves provenance back to the
originating document and chunk:

```
SourceDocument                         (one row per ingested document)
   │
   ├─ SourceChunk (chunk_index = 0)    (ordered chunks of the document)
   ├─ SourceChunk (chunk_index = 1)
   └─ SourceChunk (chunk_index = N)
        │
        └─ VectorItem (chunk_id = ...) (one or many memories per chunk)
```

The hierarchy is **fully optional**. Deployments without a `SourceStore`
keep working exactly as before — vectors stay anonymous flat rows with
`chunk_id = NULL`. The schema's backreference column on `astrocyte_vectors`
is `ADD COLUMN IF NOT EXISTS chunk_id TEXT` (nullable) so existing data
needs no rewrite.

## Quick start

```yaml
# astrocyte.yaml
source_store: postgres
source_store_config:
  dsn: "postgresql://astrocyte:****@db:5432/astrocyte"
  bootstrap_schema: false   # production: trust the 014 migration
```

```bash
# Apply migrations (single tenant — see schema-per-tenant.md for multi).
SCHEMA=public ./adapters-storage-py/astrocyte-postgres/scripts/migrate.sh

# Or via env-var:
ASTROCYTE_SOURCE_STORE=postgres ./scripts/runbook-up.sh
```

## Why a dedicated SPI

The vector store records what was extracted; the source store records
where it came from. Three concerns made a separate SPI worth the cost:

| Concern | Vector store only (pre-M10) | First-class SourceStore (M10) |
|---|---|---|
| Provenance | Reconstructed from `metadata` keys per pipeline | Preserved as a relation |
| Re-extraction | Need to re-fetch + re-chunk the original document | Iterate `SourceChunk` rows |
| Dedup at ingest | Caller compares vector text — slow + lossy | Caller probes `content_hash` against `astrocyte_source_documents` |
| Downstream attribution ("show citations") | No stable id for the source | `chunk_id` → `document_id` → `source_uri` |
| Per-document deletion | Iterate vectors by metadata | Soft-delete the document; chunks/vectors retain by their own lifecycle |

## Architecture

### Layers

```
Ingestion / Retain                     Re-extraction / Cite-back
    │                                       │
    ▼                                       ▼
┌──────────────────────────────┐    ┌──────────────────────────────┐
│  Caller-side ingestion       │    │  Recall pipeline / API       │
│  (chunker + embedder)        │    │  (cite source by chunk_id)   │
└──────────────────────────────┘    └──────────────────────────────┘
              │                                  │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │ SourceStore (SPI)            │
              │  (provider.py)               │
              └──────────────────────────────┘
                             │
            ┌────────────────┴───────────────────────┐
            ▼                                        ▼
┌─────────────────────────┐         ┌────────────────────────────┐
│ InMemorySourceStore     │         │ PostgresSourceStore        │
│  (testing/in_memory.py) │         │  (014 migration)           │
└─────────────────────────┘         └────────────────────────────┘
```

### Schema (Postgres adapter)

`migrations/014_source_documents.sql` creates two tables:

**`astrocyte_source_documents`** — top-level document.

| Column | Notes |
|---|---|
| `id` (TEXT) | Caller-supplied; PRIMARY KEY together with `bank_id` |
| `bank_id` (TEXT) | Tenant boundary for the document |
| `title` (TEXT, nullable) | Free-text title |
| `source_uri` (TEXT, nullable) | URL / path / Slack permalink / etc. |
| `content_hash` (TEXT, nullable) | SHA-256 hex; powers dedup |
| `content_type` (TEXT, nullable) | MIME type |
| `metadata` (JSONB) | Caller-defined keys |
| `created_at` (TIMESTAMPTZ) | Defaults to `NOW()` |
| `deleted_at` (TIMESTAMPTZ, nullable) | Soft-delete marker |

Indexes:
- `(bank_id, created_at DESC) WHERE deleted_at IS NULL` — recency listing.
- `UNIQUE (bank_id, content_hash) WHERE content_hash IS NOT NULL AND deleted_at IS NULL` — dedup guard. Partial so caller-side races can't slip in duplicate hashes.

**`astrocyte_source_chunks`** — ordered chunks of a document.

| Column | Notes |
|---|---|
| `id` (TEXT) | Caller-supplied chunk id |
| `bank_id` (TEXT) | Tenant boundary |
| `document_id` (TEXT) | FK → `astrocyte_source_documents(id)` `ON DELETE CASCADE` |
| `chunk_index` (INTEGER) | UNIQUE within `(bank_id, document_id)` |
| `text` (TEXT) | The chunk body |
| `content_hash` (TEXT, nullable) | SHA-256 hex of the chunk text |
| `metadata` (JSONB) | Caller-defined keys |
| `created_at` (TIMESTAMPTZ) | Defaults to `NOW()` |

Indexes:
- `(bank_id, document_id, chunk_index)` — load all chunks of a document in order.
- `UNIQUE (bank_id, content_hash) WHERE content_hash IS NOT NULL` — dedup.

**Backreference on `astrocyte_vectors`**:

```sql
ALTER TABLE astrocyte_vectors ADD COLUMN IF NOT EXISTS chunk_id TEXT;
CREATE INDEX astrocyte_vectors_chunk_idx ON astrocyte_vectors (chunk_id)
  WHERE chunk_id IS NOT NULL;
```

The column is nullable for backward compatibility — legacy vectors keep
`chunk_id = NULL` and continue to work.

## SPI contract

```python
class SourceStore(Protocol):
    SPI_VERSION: ClassVar[int] = 1

    async def store_document(self, document: SourceDocument) -> str: ...
    async def get_document(self, document_id: str, bank_id: str) -> SourceDocument | None: ...
    async def find_document_by_hash(self, content_hash: str, bank_id: str) -> SourceDocument | None: ...
    async def list_documents(self, bank_id: str, *, limit: int = 100) -> list[SourceDocument]: ...
    async def delete_document(self, document_id: str, bank_id: str) -> bool: ...

    async def store_chunks(self, chunks: list[SourceChunk]) -> list[str]: ...
    async def get_chunk(self, chunk_id: str, bank_id: str) -> SourceChunk | None: ...
    async def list_chunks(self, document_id: str, bank_id: str) -> list[SourceChunk]: ...
    async def find_chunk_by_hash(self, content_hash: str, bank_id: str) -> SourceChunk | None: ...

    async def health(self) -> HealthStatus: ...
```

Behaviour pinned by the spec:

- **`store_document` is upsert-with-dedup.** When `content_hash` is set
  and matches an existing live (not soft-deleted) row in the same bank,
  the existing id is returned. Caller's requested id is ignored. When
  `content_hash` is `None`, no dedup happens — the requested id wins.
- **`store_chunks` is bulk-insert-with-dedup.** Same rule per chunk.
  The Postgres adapter performs a single batched probe (`WHERE
  content_hash = ANY(%s::text[])`) per call to keep the round-trip cost
  flat regardless of batch size.
- **`delete_document` is soft-delete.** It sets `deleted_at` and returns
  `True` only if a live row was found. Soft-deleted documents disappear
  from `get_document` / `list_documents` / `find_document_by_hash` /
  `find_chunk_by_hash` (all read paths filter `deleted_at IS NULL`),
  but their chunks are NOT auto-deleted — re-extraction flows can still
  resolve provenance until you hard-delete the parent row, at which
  point the FK CASCADE removes the chunks.
- **Dedup is bank-scoped.** Same hash in two different banks creates
  two rows. (Mirrors how vector / mental-model dedup also scopes by bank.)

## Per-tenant aware

The Postgres adapter is **schema-per-tenant aware** via the
`astrocyte.tenancy._current_schema` ContextVar (see
[schema-per-tenant.md](schema-per-tenant.md)). Every SQL statement routes
through `fq_table()`, so a single `PostgresSourceStore` instance serves
multiple tenants when the gateway sets the active schema per request:

```python
from astrocyte.tenancy import use_schema

with use_schema("tenant_acme"):
    await source_store.store_document(...)   # writes to tenant_acme.astrocyte_source_documents
with use_schema("tenant_globex"):
    await source_store.store_document(...)   # writes to tenant_globex.astrocyte_source_documents
```

Bootstrap (`bootstrap_schema=True`, the default) is per-tenant tracked —
each new schema is materialised on first use. In production, run the
migration via `migrate.sh` per tenant and set `bootstrap_schema: false`.

## Ingestion patterns

### Pattern 1: Probe-then-store (caller-side dedup)

```python
import hashlib

content_hash = hashlib.sha256(body.encode()).hexdigest()
existing = await source_store.find_document_by_hash(content_hash, bank_id)
if existing:
    document_id = existing.id
else:
    document_id = await source_store.store_document(SourceDocument(
        id=f"doc:{content_hash[:12]}",
        bank_id=bank_id,
        title=title,
        source_uri=uri,
        content_hash=content_hash,
        content_type="text/plain",
    ))
```

### Pattern 2: Optimistic store (let the server dedup)

```python
# store_document is idempotent when content_hash is set, so this is safe.
document_id = await source_store.store_document(SourceDocument(
    id=requested_id,                 # may be ignored if a dup exists
    bank_id=bank_id,
    content_hash=content_hash,
    ...
))
```

### Linking a memory back to its chunk

```python
chunk_id = (await source_store.store_chunks([chunk]))[0]
# Then in your retain payload:
await brain.retain(
    bank_id=bank_id,
    items=[VectorItem(..., chunk_id=chunk_id)],   # threaded through pipeline
)
```

## Decision record

### Why upsert-with-dedup instead of error-on-conflict?

Two ingestion paths matter: streaming (Slack, GitHub PRs, …) and
batch reprocessing. Both want "store this document; if it's already
here under any id, give me the existing id and don't fail." A
strict error-on-conflict would force every caller to wrap
`store_document` in try/except + `find_document_by_hash`. Pushing the
probe into the SPI keeps callers terse and makes the dedup guarantee
testable in one place.

### Why soft-delete the document but cascade-on-hard-delete the chunks?

The schema declares the FK as `ON DELETE CASCADE` so a hard delete
(operator running `DELETE FROM astrocyte_source_documents WHERE …`)
removes chunks atomically. The SPI's `delete_document` is a
soft-delete because re-extraction flows often want to invalidate a
document while still being able to walk its chunks for one more
passes (e.g. "the source URL changed, but I still need to re-emit the
old citations"). Operators can hard-delete out of band when they
truly want to reclaim space.

### Why `chunk_id` is nullable on `astrocyte_vectors`

Adding a NOT-NULL column to a hot table on a live database is a
migration risk and a backward-compat break. Nullable + a partial
index (`WHERE chunk_id IS NOT NULL`) gives source-aware deployments
the index they need without rewriting existing rows. Vectors retain
today's anonymous flat-row contract by default.

### Why dedup is bank-scoped

Two distinct customers might legitimately ingest the same Wikipedia
article. Cross-bank dedup would either leak data (one tenant's
document_id surfacing in another) or force a separate "global"
namespace that nobody asked for. Bank-scoped dedup keeps the boundary
where every other store puts it.

## See also

- [`schema-per-tenant.md`](schema-per-tenant.md) — multi-tenant routing
- [`provider-spi.md`](provider-spi.md) — SPI versioning rules
- `astrocyte_postgres.PostgresSourceStore` — adapter source
- `astrocyte.testing.in_memory.InMemorySourceStore` — reference implementation
- `migrations/014_source_documents.sql` — schema source of truth
