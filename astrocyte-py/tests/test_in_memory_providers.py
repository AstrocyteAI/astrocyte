"""Conformance tests for in-memory test providers.

Validates that InMemoryVectorStore, InMemoryGraphStore, InMemoryDocumentStore,
InMemoryEngineProvider, and MockLLMProvider honour their SPI contracts.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from astrocyte.testing.in_memory import (
    InMemoryDocumentStore,
    InMemoryEngineProvider,
    InMemoryGraphStore,
    InMemoryVectorStore,
    MockLLMProvider,
    _cosine_sim,
    _extractive_synthesize,
    _normalize_terms,
)
from astrocyte.types import (
    Document,
    Entity,
    EntityLink,
    ForgetRequest,
    MemoryEntityAssociation,
    Message,
    RecallRequest,
    ReflectRequest,
    RetainRequest,
    VectorFilters,
    VectorItem,
)

# ---------------------------------------------------------------------------
# _cosine_sim helper
# ---------------------------------------------------------------------------


class TestCosineSim:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert _cosine_sim(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_sim([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert _cosine_sim([0, 0], [1, 2]) == 0.0

    def test_opposite_direction(self):
        assert _cosine_sim([1, 0], [-1, 0]) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# InMemoryVectorStore
# ---------------------------------------------------------------------------


class TestInMemoryVectorStore:
    @pytest.mark.asyncio
    async def test_store_and_search(self):
        vs = InMemoryVectorStore()
        items = [
            VectorItem(id="v1", text="hello world", vector=[1.0, 0.0], bank_id="b1"),
            VectorItem(id="v2", text="goodbye world", vector=[0.0, 1.0], bank_id="b1"),
        ]
        ids = await vs.store_vectors(items)
        assert ids == ["v1", "v2"]

        results = await vs.search_similar([1.0, 0.0], "b1", limit=10)
        assert len(results) == 2
        assert results[0].id == "v1"
        assert results[0].score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_search_filters_by_bank(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0], bank_id="b1"),
            VectorItem(id="v2", text="b", vector=[1.0], bank_id="b2"),
        ])
        results = await vs.search_similar([1.0], "b1")
        assert len(results) == 1
        assert results[0].id == "v1"

    @pytest.mark.asyncio
    async def test_search_with_tag_filter(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0], bank_id="b1", tags=["important"]),
            VectorItem(id="v2", text="b", vector=[1.0], bank_id="b1", tags=["trivial"]),
        ])
        results = await vs.search_similar(
            [1.0], "b1", filters=VectorFilters(tags=["important"])
        )
        assert len(results) == 1
        assert results[0].id == "v1"

    @pytest.mark.asyncio
    async def test_search_with_fact_type_filter(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0], bank_id="b1", fact_type="world"),
            VectorItem(id="v2", text="b", vector=[1.0], bank_id="b1", fact_type="experience"),
        ])
        results = await vs.search_similar(
            [1.0], "b1", filters=VectorFilters(fact_types=["experience"])
        )
        assert len(results) == 1
        assert results[0].id == "v2"

    @pytest.mark.asyncio
    async def test_search_limit(self):
        vs = InMemoryVectorStore()
        items = [
            VectorItem(id=f"v{i}", text=f"item {i}", vector=[float(i)], bank_id="b1")
            for i in range(5)
        ]
        await vs.store_vectors(items)
        results = await vs.search_similar([5.0], "b1", limit=2)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_preserves_metadata(self):
        vs = InMemoryVectorStore()
        now = datetime.now(timezone.utc)
        await vs.store_vectors([
            VectorItem(
                id="v1", text="a", vector=[1.0], bank_id="b1",
                metadata={"key": "val"}, tags=["t1"], fact_type="world",
                occurred_at=now, memory_layer="semantic",
            ),
        ])
        results = await vs.search_similar([1.0], "b1")
        hit = results[0]
        assert hit.metadata == {"key": "val"}
        assert hit.tags == ["t1"]
        assert hit.fact_type == "world"
        assert hit.occurred_at == now
        assert hit.memory_layer == "semantic"

    @pytest.mark.asyncio
    async def test_delete(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0], bank_id="b1"),
            VectorItem(id="v2", text="b", vector=[1.0], bank_id="b1"),
        ])
        count = await vs.delete(["v1"], "b1")
        assert count == 1
        results = await vs.search_similar([1.0], "b1")
        assert len(results) == 1
        assert results[0].id == "v2"

    @pytest.mark.asyncio
    async def test_delete_wrong_bank(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0], bank_id="b1"),
        ])
        count = await vs.delete(["v1"], "b2")
        assert count == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        vs = InMemoryVectorStore()
        count = await vs.delete(["nope"], "b1")
        assert count == 0

    @pytest.mark.asyncio
    async def test_list_vectors(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="a", vector=[1.0], bank_id="b1"),
            VectorItem(id="v2", text="b", vector=[1.0], bank_id="b1"),
            VectorItem(id="v3", text="c", vector=[1.0], bank_id="b2"),
        ])
        items = await vs.list_vectors("b1")
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_list_vectors_pagination(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id=f"v{i}", text=f"{i}", vector=[1.0], bank_id="b1")
            for i in range(5)
        ])
        page = await vs.list_vectors("b1", offset=2, limit=2)
        assert len(page) == 2

    @pytest.mark.asyncio
    async def test_health(self):
        vs = InMemoryVectorStore()
        status = await vs.health()
        assert status.healthy is True

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self):
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="v1", text="original", vector=[1.0], bank_id="b1"),
        ])
        await vs.store_vectors([
            VectorItem(id="v1", text="updated", vector=[1.0], bank_id="b1"),
        ])
        items = await vs.list_vectors("b1")
        assert len(items) == 1
        assert items[0].text == "updated"

    @pytest.mark.asyncio
    async def test_empty_search(self):
        vs = InMemoryVectorStore()
        results = await vs.search_similar([1.0], "nonexistent")
        assert results == []


# ---------------------------------------------------------------------------
# InMemoryGraphStore
# ---------------------------------------------------------------------------


class TestInMemoryGraphStore:
    @pytest.mark.asyncio
    async def test_store_and_query_entities(self):
        gs = InMemoryGraphStore()
        entities = [
            Entity(id="e1", name="Alice", entity_type="PERSON"),
            Entity(id="e2", name="Bob", entity_type="PERSON"),
        ]
        ids = await gs.store_entities(entities, "b1")
        assert ids == ["e1", "e2"]

        results = await gs.query_entities("alice", "b1")
        assert len(results) == 1
        assert results[0].name == "Alice"

    @pytest.mark.asyncio
    async def test_query_entities_case_insensitive(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([Entity(id="e1", name="Alice", entity_type="PERSON")], "b1")
        results = await gs.query_entities("ALICE", "b1")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_query_entities_bank_isolation(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([Entity(id="e1", name="Alice", entity_type="PERSON")], "b1")
        results = await gs.query_entities("Alice", "b2")
        assert results == []

    @pytest.mark.asyncio
    async def test_store_links(self):
        gs = InMemoryGraphStore()
        links = [EntityLink(source_entity_id="e1", target_entity_id="e2", link_type="knows")]
        ids = await gs.store_links(links, "b1")
        assert len(ids) == 1

    @pytest.mark.asyncio
    async def test_link_memories_and_query_neighbors(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([Entity(id="e1", name="Alice", entity_type="PERSON")], "b1")
        await gs.link_memories_to_entities(
            [MemoryEntityAssociation(memory_id="m1", entity_id="e1")], "b1"
        )
        hits = await gs.query_neighbors(["e1"], "b1")
        assert len(hits) == 1
        assert hits[0].memory_id == "m1"

    @pytest.mark.asyncio
    async def test_query_neighbors_bank_isolation(self):
        gs = InMemoryGraphStore()
        await gs.link_memories_to_entities(
            [MemoryEntityAssociation(memory_id="m1", entity_id="e1")], "b1"
        )
        hits = await gs.query_neighbors(["e1"], "b2")
        assert hits == []

    @pytest.mark.asyncio
    async def test_query_neighbors_limit(self):
        gs = InMemoryGraphStore()
        assocs = [
            MemoryEntityAssociation(memory_id=f"m{i}", entity_id="e1")
            for i in range(10)
        ]
        await gs.link_memories_to_entities(assocs, "b1")
        hits = await gs.query_neighbors(["e1"], "b1", limit=3)
        assert len(hits) == 3

    @pytest.mark.asyncio
    async def test_health(self):
        gs = InMemoryGraphStore()
        status = await gs.health()
        assert status.healthy is True


# ---------------------------------------------------------------------------
# InMemoryDocumentStore
# ---------------------------------------------------------------------------


class TestInMemoryDocumentStore:
    @pytest.mark.asyncio
    async def test_store_and_search(self):
        ds = InMemoryDocumentStore()
        doc = Document(id="d1", text="dark mode preferences for the app")
        await ds.store_document(doc, "b1")

        results = await ds.search_fulltext("dark mode", "b1")
        assert len(results) == 1
        assert results[0].document_id == "d1"
        assert results[0].score > 0

    @pytest.mark.asyncio
    async def test_search_no_match(self):
        ds = InMemoryDocumentStore()
        doc = Document(id="d1", text="completely unrelated content")
        await ds.store_document(doc, "b1")

        results = await ds.search_fulltext("quantum physics", "b1")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_bank_isolation(self):
        ds = InMemoryDocumentStore()
        await ds.store_document(Document(id="d1", text="hello world"), "b1")
        results = await ds.search_fulltext("hello", "b2")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_limit(self):
        ds = InMemoryDocumentStore()
        for i in range(5):
            await ds.store_document(Document(id=f"d{i}", text=f"common word item {i}"), "b1")
        results = await ds.search_fulltext("common", "b1", limit=2)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_ranked_by_overlap(self):
        ds = InMemoryDocumentStore()
        await ds.store_document(Document(id="d1", text="dark"), "b1")
        await ds.store_document(Document(id="d2", text="dark mode settings"), "b1")
        results = await ds.search_fulltext("dark mode", "b1")
        assert results[0].document_id == "d2"  # More overlap → higher score

    @pytest.mark.asyncio
    async def test_get_document(self):
        ds = InMemoryDocumentStore()
        doc = Document(id="d1", text="hello", metadata={"key": "val"})
        await ds.store_document(doc, "b1")

        result = await ds.get_document("d1", "b1")
        assert result is not None
        assert result.text == "hello"
        assert result.metadata == {"key": "val"}

    @pytest.mark.asyncio
    async def test_get_document_wrong_bank(self):
        ds = InMemoryDocumentStore()
        await ds.store_document(Document(id="d1", text="hello"), "b1")
        assert await ds.get_document("d1", "b2") is None

    @pytest.mark.asyncio
    async def test_get_document_nonexistent(self):
        ds = InMemoryDocumentStore()
        assert await ds.get_document("nope", "b1") is None

    @pytest.mark.asyncio
    async def test_health(self):
        ds = InMemoryDocumentStore()
        status = await ds.health()
        assert status.healthy is True


# ---------------------------------------------------------------------------
# InMemoryEngineProvider
# ---------------------------------------------------------------------------


class TestInMemoryEngineProvider:
    @pytest.mark.asyncio
    async def test_retain_and_recall(self):
        ep = InMemoryEngineProvider()
        result = await ep.retain(RetainRequest(content="dark mode preference", bank_id="b1"))
        assert result.stored is True
        assert result.memory_id is not None

        recall = await ep.recall(RecallRequest(query="dark mode", bank_id="b1"))
        assert len(recall.hits) == 1
        assert "dark mode" in recall.hits[0].text

    @pytest.mark.asyncio
    async def test_recall_keyword_scoring(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="dark mode preference", bank_id="b1"))
        await ep.retain(RetainRequest(content="light mode preference", bank_id="b1"))

        recall = await ep.recall(RecallRequest(query="dark mode", bank_id="b1"))
        assert recall.hits[0].text == "dark mode preference"
        assert recall.hits[0].score > recall.hits[1].score

    @pytest.mark.asyncio
    async def test_recall_no_match(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="hello world", bank_id="b1"))
        recall = await ep.recall(RecallRequest(query="quantum physics", bank_id="b1"))
        assert recall.hits == []

    @pytest.mark.asyncio
    async def test_recall_wildcard(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="mem1", bank_id="b1"))
        await ep.retain(RetainRequest(content="mem2", bank_id="b1"))
        recall = await ep.recall(RecallRequest(query="*", bank_id="b1"))
        assert len(recall.hits) == 2

    @pytest.mark.asyncio
    async def test_recall_tag_filter(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="tagged", bank_id="b1", tags=["important"]))
        await ep.retain(RetainRequest(content="untagged", bank_id="b1"))
        recall = await ep.recall(RecallRequest(query="*", bank_id="b1", tags=["important"]))
        assert len(recall.hits) == 1
        assert recall.hits[0].text == "tagged"

    @pytest.mark.asyncio
    async def test_recall_fact_type_filter(self):
        ep = InMemoryEngineProvider()
        # InMemoryEngineProvider always sets fact_type="world"
        await ep.retain(RetainRequest(content="fact", bank_id="b1"))
        recall = await ep.recall(RecallRequest(query="*", bank_id="b1", fact_types=["world"]))
        assert len(recall.hits) == 1
        recall2 = await ep.recall(RecallRequest(query="*", bank_id="b1", fact_types=["experience"]))
        assert len(recall2.hits) == 0

    @pytest.mark.asyncio
    async def test_recall_time_range_filter(self):
        ep = InMemoryEngineProvider()
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=30)
        await ep.retain(RetainRequest(content="recent", bank_id="b1", occurred_at=now))
        await ep.retain(RetainRequest(content="old", bank_id="b1", occurred_at=old))
        recall = await ep.recall(RecallRequest(
            query="*", bank_id="b1",
            time_range=(now - timedelta(hours=1), now + timedelta(hours=1)),
        ))
        assert len(recall.hits) == 1
        assert recall.hits[0].text == "recent"

    @pytest.mark.asyncio
    async def test_recall_max_results(self):
        ep = InMemoryEngineProvider()
        for i in range(10):
            await ep.retain(RetainRequest(content=f"common word {i}", bank_id="b1"))
        recall = await ep.recall(RecallRequest(query="common word", bank_id="b1", max_results=3))
        assert len(recall.hits) == 3
        assert recall.truncated is True

    @pytest.mark.asyncio
    async def test_recall_bank_isolation(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="hello", bank_id="b1"))
        recall = await ep.recall(RecallRequest(query="hello", bank_id="b2"))
        assert recall.hits == []

    @pytest.mark.asyncio
    async def test_reflect(self):
        ep = InMemoryEngineProvider(supports_reflect=True)
        await ep.retain(RetainRequest(content="dark mode preference", bank_id="b1"))
        result = await ep.reflect(ReflectRequest(query="dark mode", bank_id="b1"))
        assert "dark mode" in result.answer
        assert len(result.sources) > 0

    @pytest.mark.asyncio
    async def test_reflect_not_supported(self):
        ep = InMemoryEngineProvider(supports_reflect=False)
        with pytest.raises(NotImplementedError):
            await ep.reflect(ReflectRequest(query="test", bank_id="b1"))

    @pytest.mark.asyncio
    async def test_forget_all(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="mem1", bank_id="b1"))
        await ep.retain(RetainRequest(content="mem2", bank_id="b1"))
        result = await ep.forget(ForgetRequest(bank_id="b1", scope="all"))
        assert result.deleted_count == 2

        recall = await ep.recall(RecallRequest(query="*", bank_id="b1"))
        assert recall.hits == []

    @pytest.mark.asyncio
    async def test_forget_by_ids(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="keep", bank_id="b1"))
        r2 = await ep.retain(RetainRequest(content="delete", bank_id="b1"))
        result = await ep.forget(ForgetRequest(bank_id="b1", memory_ids=[r2.memory_id]))
        assert result.deleted_count == 1
        recall = await ep.recall(RecallRequest(query="*", bank_id="b1"))
        assert len(recall.hits) == 1
        assert recall.hits[0].text == "keep"

    @pytest.mark.asyncio
    async def test_forget_by_tags(self):
        ep = InMemoryEngineProvider()
        await ep.retain(RetainRequest(content="tagged", bank_id="b1", tags=["temp"]))
        await ep.retain(RetainRequest(content="safe", bank_id="b1", tags=["permanent"]))
        result = await ep.forget(ForgetRequest(bank_id="b1", tags=["temp"]))
        assert result.deleted_count == 1

    @pytest.mark.asyncio
    async def test_forget_by_date(self):
        ep = InMemoryEngineProvider()
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=30)
        await ep.retain(RetainRequest(content="old", bank_id="b1", occurred_at=old))
        await ep.retain(RetainRequest(content="new", bank_id="b1", occurred_at=now))
        result = await ep.forget(ForgetRequest(bank_id="b1", before_date=now - timedelta(days=1)))
        assert result.deleted_count == 1

    @pytest.mark.asyncio
    async def test_forget_not_supported(self):
        ep = InMemoryEngineProvider(supports_forget=False)
        with pytest.raises(NotImplementedError):
            await ep.forget(ForgetRequest(bank_id="b1", scope="all"))

    def test_capabilities(self):
        ep = InMemoryEngineProvider()
        caps = ep.capabilities()
        assert caps.supports_reflect is True
        assert caps.supports_forget is True
        assert caps.supports_semantic_search is True

    def test_capabilities_custom(self):
        ep = InMemoryEngineProvider(supports_reflect=False, supports_forget=False)
        caps = ep.capabilities()
        assert caps.supports_reflect is False
        assert caps.supports_forget is False

    @pytest.mark.asyncio
    async def test_health(self):
        ep = InMemoryEngineProvider()
        status = await ep.health()
        assert status.healthy is True

    @pytest.mark.asyncio
    async def test_retain_preserves_metadata(self):
        ep = InMemoryEngineProvider()
        now = datetime.now(timezone.utc)
        await ep.retain(RetainRequest(
            content="test", bank_id="b1",
            metadata={"k": "v"}, tags=["t1"],
            occurred_at=now, source="test-source",
        ))
        recall = await ep.recall(RecallRequest(query="test", bank_id="b1"))
        hit = recall.hits[0]
        # InMemoryEngineProvider auto-stamps `_created_at` so MIP forget
        # min_age_days can be enforced. Caller-provided keys are preserved.
        assert hit.metadata is not None
        assert hit.metadata["k"] == "v"
        assert "_created_at" in hit.metadata
        assert hit.tags == ["t1"]
        assert hit.occurred_at == now
        assert hit.source == "test-source"
        assert hit.bank_id == "b1"


# ---------------------------------------------------------------------------
# MockLLMProvider
# ---------------------------------------------------------------------------


class TestMockLLMProvider:
    @pytest.mark.asyncio
    async def test_embed_dimension(self):
        llm = MockLLMProvider()
        vecs = await llm.embed(["hello world"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 128

    @pytest.mark.asyncio
    async def test_embed_normalized(self):
        llm = MockLLMProvider()
        vecs = await llm.embed(["some text with words"])
        norm = math.sqrt(sum(x * x for x in vecs[0]))
        assert norm == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_embed_empty_text(self):
        llm = MockLLMProvider()
        vecs = await llm.embed([""])
        # All zeros, norm=0, stays as zeros
        assert all(x == 0.0 for x in vecs[0])

    @pytest.mark.asyncio
    async def test_embed_non_negative(self):
        llm = MockLLMProvider()
        vecs = await llm.embed(["arbitrary text with many varied words"])
        assert all(x >= 0.0 for x in vecs[0])

    @pytest.mark.asyncio
    async def test_embed_shared_words_similar(self):
        llm = MockLLMProvider()
        vecs = await llm.embed(["dark mode preference", "dark mode settings"])
        sim = _cosine_sim(vecs[0], vecs[1])
        assert sim > 0.5  # Shared "dark" and "mode"

    @pytest.mark.asyncio
    async def test_embed_unrelated_texts_low_similarity(self):
        llm = MockLLMProvider()
        vecs = await llm.embed(["quantum physics lecture", "banana smoothie recipe"])
        sim = _cosine_sim(vecs[0], vecs[1])
        assert sim < 0.3

    @pytest.mark.asyncio
    async def test_embed_multiple_texts(self):
        llm = MockLLMProvider()
        vecs = await llm.embed(["one", "two", "three"])
        assert len(vecs) == 3

    @pytest.mark.asyncio
    async def test_complete_default_response(self):
        llm = MockLLMProvider(default_response="test reply")
        result = await llm.complete([Message(role="user", content="hello")])
        assert result.text == "test reply"

    @pytest.mark.asyncio
    async def test_complete_entity_extraction(self):
        llm = MockLLMProvider()
        result = await llm.complete([
            Message(role="system", content="Extract named entities from text"),
            Message(role="user", content="Alice works at Acme Corp"),
        ])
        assert "Test Entity" in result.text

    @pytest.mark.asyncio
    async def test_complete_synthesis(self):
        llm = MockLLMProvider()
        prompt = "<memories>\n[Memory 1]: User likes dark mode\n</memories>\n\n<query>\nWhat does the user prefer?\n</query>"
        result = await llm.complete([Message(role="user", content=prompt)])
        assert "dark mode" in result.text.lower()

    @pytest.mark.asyncio
    async def test_complete_tracks_call_count(self):
        llm = MockLLMProvider()
        assert llm._call_count == 0
        await llm.complete([Message(role="user", content="hi")])
        assert llm._call_count == 1
        await llm.complete([Message(role="user", content="bye")])
        assert llm._call_count == 2

    @pytest.mark.asyncio
    async def test_complete_records_last_user_message(self):
        llm = MockLLMProvider()
        await llm.complete([
            Message(role="system", content="system"),
            Message(role="user", content="the query"),
        ])
        assert llm.last_user_message == "the query"

    @pytest.mark.asyncio
    async def test_complete_returns_usage(self):
        llm = MockLLMProvider()
        result = await llm.complete([Message(role="user", content="hi")])
        assert result.usage is not None
        assert result.usage.input_tokens > 0
        assert result.usage.output_tokens > 0

    def test_capabilities(self):
        llm = MockLLMProvider()
        caps = llm.capabilities()
        assert caps is not None


# ---------------------------------------------------------------------------
# _extractive_synthesize
# ---------------------------------------------------------------------------


class TestExtractiveSynthesize:
    def test_basic_extraction(self):
        prompt = "<memories>\n[Memory 1]: The sky is blue\n</memories>\n\n<query>\nsky color\n</query>"
        result = _extractive_synthesize(prompt)
        assert "sky" in result.lower()

    def test_multiline_memory_blocks(self):
        prompt = """<memories>
[Memory 1]: Speaker A said hello
Speaker B replied goodbye
[Memory 2]: Unrelated content here
</memories>

<query>
What did Speaker A say?
</query>"""
        result = _extractive_synthesize(prompt)
        assert "Speaker A" in result

    def test_no_query(self):
        result = _extractive_synthesize("<memories>stuff</memories>")
        assert "No query" in result

    def test_no_memories(self):
        result = _extractive_synthesize("<query>test</query>")
        assert "No memories" in result

    def test_no_overlap_returns_fallback(self):
        prompt = "<memories>\n[Memory 1]: completely different content\n</memories>\n\n<query>\nxyzzy\n</query>"
        result = _extractive_synthesize(prompt)
        assert "don't have relevant" in result.lower()

    def test_returns_top_3(self):
        memories = "\n".join(f"[Memory {i}]: word{i} shared" for i in range(5))
        prompt = f"<memories>\n{memories}\n</memories>\n\n<query>\nshared\n</query>"
        result = _extractive_synthesize(prompt)
        # Should contain content from memories (up to 3)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _normalize_terms
# ---------------------------------------------------------------------------


class TestNormalizeTerms:
    def test_strips_punctuation(self):
        terms = _normalize_terms("hello, world!")
        assert "hello" in terms
        assert "world" in terms

    def test_drops_short_words(self):
        terms = _normalize_terms("I am a big dog")
        assert "big" in terms
        assert "dog" in terms
        assert "am" not in terms
        assert "a" not in terms

    def test_lowercases(self):
        terms = _normalize_terms("Hello World")
        assert "hello" in terms
        assert "world" in terms
