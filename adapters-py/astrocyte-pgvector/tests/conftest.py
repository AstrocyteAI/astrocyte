"""Fixtures for astrocyte-pgvector integration tests."""

from __future__ import annotations

import os
import uuid
from datetime import datetime

import psycopg
import pytest
from astrocyte.types import VectorItem

from astrocyte_pgvector.store import PgVectorStore

DIM = 3  # small vectors for tests


@pytest.fixture
def dsn() -> str:
    """Read DATABASE_URL; skip the entire test if not set."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — skipping pgvector integration tests")
    return url


@pytest.fixture
async def store(dsn: str):
    """PgVectorStore with a unique table per test; drops it on teardown."""
    table = f"test_{uuid.uuid4().hex[:12]}"
    s = PgVectorStore(dsn=dsn, table_name=table, embedding_dimensions=DIM)
    yield s
    # teardown: drop the table
    conn = await psycopg.AsyncConnection.connect(dsn)
    async with conn:
        await conn.execute(f"DROP TABLE IF EXISTS {table}")
        await conn.commit()


def make_item(
    id: str,
    bank_id: str = "bank-1",
    vector: list[float] | None = None,
    text: str = "test text",
    metadata: dict | None = None,
    tags: list[str] | None = None,
    fact_type: str | None = None,
    occurred_at: datetime | None = None,
    memory_layer: str | None = None,
) -> VectorItem:
    """Create a VectorItem with sensible defaults."""
    if vector is None:
        # deterministic vector from id hash
        h = hash(id) % 1000
        vector = [float(h % 10) / 10, float((h // 10) % 10) / 10, float((h // 100) % 10) / 10]
    return VectorItem(
        id=id,
        bank_id=bank_id,
        vector=vector,
        text=text,
        metadata=metadata,
        tags=tags,
        fact_type=fact_type,
        occurred_at=occurred_at,
        memory_layer=memory_layer,
    )
