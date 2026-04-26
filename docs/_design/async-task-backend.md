# Async Task Backend

Astrocyte keeps `retain()` fast. Expensive work that improves memory quality should run after a policy-cleared commit:

- M8 wiki compile and lint.
- Observation consolidation.
- M11 entity resolution or second-pass gleaning.
- M10 scheduled gap scans.
- Memory export sink retries.

## Backend Contract

The worker contract should be narrow and storage-backed:

```python
class TaskBackend(Protocol):
    async def enqueue(self, task: MemoryTask) -> str: ...
    async def claim(self, worker_id: str, limit: int = 10) -> list[MemoryTask]: ...
    async def complete(self, task_id: str, result: dict) -> None: ...
    async def fail(self, task_id: str, error: str, retry_at: datetime | None) -> None: ...
```

`MemoryTask` should include `task_type`, `bank_id`, `payload`, `idempotency_key`, `attempts`, `max_attempts`, `run_after`, and timestamps.

## Postgres Default

Use PostgreSQL as the default backend for the reference stack:

- Table: `astrocyte_tasks`.
- Claiming: `SELECT ... FOR UPDATE SKIP LOCKED`.
- Idempotency: unique `(task_type, idempotency_key)` where `status in ('queued', 'running')`.
- Retry: exponential backoff with a capped `run_after`.
- Dead letter: keep failed rows with final status for operators.

This mirrors the Hindsight operating lesson without adding Kafka or another queue to the default deployment. Stream systems can still enqueue through an adapter later.

### Schema Sketch

```sql
CREATE TABLE astrocyte_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_type text NOT NULL,
  bank_id text NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead')),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  result jsonb,
  error text,
  idempotency_key text,
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),
  claimed_by text,
  claimed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX astrocyte_tasks_idempotency_active_idx
  ON astrocyte_tasks (task_type, idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND status IN ('queued', 'running');

CREATE INDEX astrocyte_tasks_claim_idx
  ON astrocyte_tasks (status, run_after, created_at)
  WHERE status = 'queued';
```

Claiming should happen in a short transaction:

```sql
WITH next AS (
  SELECT id
  FROM astrocyte_tasks
  WHERE status = 'queued'
    AND run_after <= now()
  ORDER BY run_after, created_at
  LIMIT $1
  FOR UPDATE SKIP LOCKED
)
UPDATE astrocyte_tasks t
SET status = 'running',
    claimed_by = $2,
    claimed_at = now(),
    updated_at = now(),
    attempts = attempts + 1
FROM next
WHERE t.id = next.id
RETURNING t.*;
```

### Outbox Role

The same table can act as a projection-repair outbox. For example, a successful wiki compile first commits SQL page/revision/provenance rows, then enqueues tasks such as `index_wiki_page_vector` and `project_wiki_page_graph`. If pgvector or AGE is temporarily unavailable, workers retry those projection tasks without corrupting the SQL source of truth.

## Worker Process

The gateway starts only in-process queues by default. Production deployments should run an `astrocyte-worker-py` process that:

1. Claims tasks from the configured backend.
2. Resolves `AstrocyteConfig` the same way as the gateway.
3. Dispatches by `task_type` to compile, consolidate, entity, audit, or export handlers.
4. Emits OpenTelemetry spans and task metrics.

## First Milestone

The first concrete backend should support `compile_bank` only. Once that path is stable, add `consolidate_observations`, `resolve_entities`, `audit_bank`, and `export_memory_event`.

The existing in-process `CompileQueue` remains the library/default development path. The Postgres backend is the production path for multi-process deployments.
