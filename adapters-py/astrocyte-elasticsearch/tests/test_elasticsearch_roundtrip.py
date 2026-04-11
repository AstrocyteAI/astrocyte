"""Integration tests for Elasticsearch DocumentStore."""

from __future__ import annotations

import os
import uuid

import pytest
from astrocyte.types import Document

from astrocyte_elasticsearch import ElasticsearchDocumentStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ES_URL = os.environ.get("ASTROCYTE_ELASTICSEARCH_URL", "http://127.0.0.1:9200")


@pytest.mark.asyncio
async def test_health(require_elasticsearch: None) -> None:
    store = ElasticsearchDocumentStore(url=ES_URL, index_prefix=f"ast_test_{uuid.uuid4().hex[:8]}")
    h = await store.health()
    assert h.healthy is True


@pytest.mark.asyncio
async def test_store_search_get(require_elasticsearch: None) -> None:
    prefix = f"ast_test_{uuid.uuid4().hex[:8]}"
    store = ElasticsearchDocumentStore(url=ES_URL, index_prefix=prefix)
    bank = "docs-b1"
    doc = Document(id="d1", text="Elasticsearch adapter full text search", tags=["t1"])
    did = await store.store_document(doc, bank_id=bank)
    assert did == "d1"

    hits = await store.search_fulltext("adapter", bank_id=bank, limit=5)
    assert len(hits) >= 1
    assert "Elasticsearch" in hits[0].text
    assert 0.0 <= hits[0].score <= 1.0

    got = await store.get_document("d1", bank_id=bank)
    assert got is not None
    assert got.text == doc.text
