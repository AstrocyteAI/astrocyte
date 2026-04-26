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

Use PostgreSQL as the default backend for the reference stack. The production worker implementation should use [PgQueuer](https://janbjorge.github.io/pgqueuer/) rather than a bespoke polling loop. PgQueuer gives us the same core primitives in this design (`FOR UPDATE SKIP LOCKED`, transactional enqueue, retries) plus `LISTEN/NOTIFY`, scheduling, heartbeat, completion tracking, metrics, and an in-memory test adapter.

- Table: `astrocyte_tasks`.
- Claiming: `SELECT ... FOR UPDATE SKIP LOCKED`.
- Idempotency: unique `(task_type, idempotency_key)` where `status in ('queued', 'running')`.
- Retry: exponential backoff with a capped `run_after`.
- Dead letter: keep failed rows with final status for operators.

This mirrors the Hindsight operating lesson without adding Kafka or another queue to the default deployment. Stream systems can still enqueue through an adapter later.

The framework-facing API remains `MemoryTask` + `MemoryTaskDispatcher`. PgQueuer lives at the worker boundary:

```text
MemoryTask
  -> PgQueuerMemoryTaskQueue.enqueue()
  -> PgQueuer entrypoint(task_type)
  -> MemoryTaskDispatcher.run(task)
```

Install via the worker extra:

```bash
pip install 'astrocyte[worker]'
```

When using psycopg, PgQueuer requires the async connection to be opened with `autocommit=True`.

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

The gateway starts only in-process queues by default. Production deployments should run an `astrocyte-worker-py` process backed by PgQueuer that:

1. Claims tasks from the configured backend.
2. Resolves `AstrocyteConfig` the same way as the gateway.
3. Dispatches by `task_type` to compile, consolidate, entity, audit, or export handlers.
4. Emits OpenTelemetry spans and task metrics.

Benchmark integration tests should exercise the PgQueuer path against Postgres when `ASTROCYTE_PGQUEUER_TEST_DSN` is set. This proves that benchmark preprocessing tasks can run in the same database envelope as pgvector/AGE before LoCoMo or LongMemEval evaluation starts.

Gateway deployments can enable the worker lifecycle through `astrocyte.yaml`:

```yaml
async_tasks:
  enabled: true
  backend: pgqueuer
  dsn: ${ASTROCYTE_TASKS_DSN}
  install_on_start: true
  auto_start_worker: true
  batch_size: 10
```

Tests and local demos may use `backend: pgqueuer_in_memory`; production should use `backend: pgqueuer` with Postgres.

## First Milestone

The first concrete backend should support the benchmark-improvement task set below. These jobs are deliberately off the `retain()` and `recall()` hot paths; workers materialize better memory structures before benchmarks ask questions.

| Task type | Main benchmark target | What it does |
|---|---|---|
| `compile_bank` | LongMemEval multi-session / knowledge-update | Runs `CompileEngine` over a bank or scope to create durable wiki pages from raw memories. |
| `compile_persona_page` | LoCoMo open-domain | Builds `person:{name}` pages from speaker/person metadata and raw dialogue evidence. |
| `index_wiki_page_vector` | LoCoMo + LongMemEval precision | Embeds the current wiki page revision into pgvector with `memory_layer="compiled"` and `fact_type="wiki"`. |
| `project_entity_edges` | LoCoMo multi-hop | Projects person/session/turn co-occurrence into the graph store with memory provenance. |
| `normalize_temporal_facts` | LoCoMo temporal | Resolves relative temporal phrases against `occurred_at` and writes `temporal_phrase`, `resolved_date`, and `date_granularity` metadata. |
| `lint_wiki_page` | LongMemEval knowledge-update | Runs wiki lint to find stale, orphaned, or contradictory compiled pages; stale pages can enqueue recompile tasks. |
| `analyze_benchmark_failures` | LoCoMo + LongMemEval regression loop | Reads serialized per-question benchmark output, groups failures, and emits a stable regression slice. |

Once these are stable, add `consolidate_observations`, `resolve_entities`, `audit_bank`, and `export_memory_event`.

The existing in-process `CompileQueue` remains the library/default development path. The Postgres backend is the production path for multi-process deployments.
