"""PgQueuer integration tests for Astrocyte memory tasks."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.pgqueuer_tasks import (
    PgQueuerMemoryTaskQueue,
    task_from_pgqueuer_payload,
    task_to_pgqueuer_payload,
)
from astrocyte.pipeline.tasks import (
    NORMALIZE_TEMPORAL_FACTS,
    MemoryTask,
    MemoryTaskDispatcher,
    TaskHandlerContext,
)
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import VectorItem


def _dispatcher(vector_store: InMemoryVectorStore) -> MemoryTaskDispatcher:
    return MemoryTaskDispatcher(
        TaskHandlerContext(
            vector_store=vector_store,
            llm_provider=MockLLMProvider(),
        )
    )


async def test_pgqueuer_in_memory_runs_memory_task() -> None:
    vector_store = InMemoryVectorStore()
    await vector_store.store_vectors([
        VectorItem(
            id="m1",
            bank_id="bench-locomo",
            vector=[1.0] + [0.0] * 127,
            text="Alice went hiking yesterday.",
            occurred_at=datetime(2026, 2, 10, tzinfo=UTC),
        )
    ])
    queue = PgQueuerMemoryTaskQueue.in_memory()
    queue.register_dispatcher(_dispatcher(vector_store))

    await queue.enqueue(MemoryTask(
        task_type=NORMALIZE_TEMPORAL_FACTS,
        bank_id="bench-locomo",
        idempotency_key="normalize:bench-locomo",
    ))
    await queue.run_drain()

    stored = (await vector_store.list_vectors("bench-locomo"))[0]
    assert stored.metadata is not None
    assert stored.metadata["resolved_date"] == "2026-02-09"


async def test_pgqueuer_payload_mapping_roundtrips_memory_task() -> None:
    task = MemoryTask(
        task_type=NORMALIZE_TEMPORAL_FACTS,
        bank_id="bench-locomo",
        payload={"memory_ids": ["m1"]},
        idempotency_key="normalize:bench-locomo",
        run_after=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 4, 26, 11, 0, tzinfo=UTC),
    )

    payload = task_to_pgqueuer_payload(task)
    decoded = task_from_pgqueuer_payload(
        json.dumps(payload).encode("utf-8"),
        fallback_task_type="fallback",
    )

    assert payload == {
        "id": task.id,
        "task_type": NORMALIZE_TEMPORAL_FACTS,
        "bank_id": "bench-locomo",
        "payload": {"memory_ids": ["m1"]},
        "idempotency_key": "normalize:bench-locomo",
        "attempts": 0,
        "max_attempts": 5,
        "run_after": "2026-04-26T12:00:00+00:00",
        "created_at": "2026-04-26T11:00:00+00:00",
    }
    assert decoded.task_type == task.task_type
    assert decoded.bank_id == task.bank_id
    assert decoded.payload == task.payload
    assert decoded.run_after == task.run_after


@pytest.mark.skipif(
    not os.environ.get("ASTROCYTE_PGQUEUER_TEST_DSN"),
    reason="Set ASTROCYTE_PGQUEUER_TEST_DSN to run PgQueuer/Postgres benchmark task test",
)
async def test_pgqueuer_postgres_runs_benchmark_preprocessing_task() -> None:
    import psycopg

    vector_store = InMemoryVectorStore()
    await vector_store.store_vectors([
        VectorItem(
            id="m1",
            bank_id="bench-locomo-pg",
            vector=[1.0] + [0.0] * 127,
            text="Alice went hiking yesterday.",
            occurred_at=datetime(2026, 2, 10, tzinfo=UTC),
        )
    ])

    async with await psycopg.AsyncConnection.connect(
        os.environ["ASTROCYTE_PGQUEUER_TEST_DSN"],
        autocommit=True,
    ) as conn:
        queue = PgQueuerMemoryTaskQueue.from_psycopg_connection(conn)
        await queue.install()
        await queue.clear()
        queue.register_dispatcher(_dispatcher(vector_store))

        await queue.enqueue(MemoryTask(
            task_type=NORMALIZE_TEMPORAL_FACTS,
            bank_id="bench-locomo-pg",
            idempotency_key="normalize:bench-locomo-pg",
        ))
        await queue.run_drain()

    stored = (await vector_store.list_vectors("bench-locomo-pg"))[0]
    assert stored.metadata is not None
    assert stored.metadata["resolved_date"] == "2026-02-09"
