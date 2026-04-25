"""M8: LLM wiki compile — unit and integration tests.

Tests cover:
- WikiPage type and WikiStore SPI (W1)
- CompileEngine: DBSCAN clustering, scope discovery, synthesis (W2)
- brain.compile() integration (W2)
- InMemoryWikiStore: upsert, revision tracking, list, delete (W1)
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.errors import ConfigError
from astrocyte.pipeline.compile import CompileEngine, _dbscan, _infer_kind
from astrocyte.testing.in_memory import (
    InMemoryVectorStore,
    InMemoryWikiStore,
    MockLLMProvider,
)
from astrocyte.types import (
    CompileResult,
    VectorItem,
    WikiPage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CyclingLLMProvider(MockLLMProvider):
    """MockLLMProvider that cycles through a list of responses.

    Used to give distinct cluster labels when two clusters are discovered
    in the same compile run.
    """

    def __init__(self, responses: list[str]) -> None:
        super().__init__(default_response=responses[0])
        self._responses = responses
        self._idx = 0

    async def complete(self, messages, model=None, max_tokens=1024, temperature=0.0):  # type: ignore[override]
        from astrocyte.types import Completion, TokenUsage

        resp = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return Completion(text=resp, model="mock", usage=TokenUsage(input_tokens=5, output_tokens=5))


def _make_vector(seed: float, dim: int = 128) -> list[float]:
    """Generate a normalised vector with a single large component at position seed%dim."""
    vec = [0.0] * dim
    vec[int(seed) % dim] = 1.0
    return vec


def _make_item(
    item_id: str,
    bank_id: str,
    text: str,
    vector_seed: float,
    tags: list[str] | None = None,
) -> VectorItem:
    return VectorItem(
        id=item_id,
        bank_id=bank_id,
        vector=_make_vector(vector_seed),
        text=text,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# W1: WikiPage type
# ---------------------------------------------------------------------------


class TestWikiPageType:
    def test_fields_present(self) -> None:
        now = datetime.now(UTC)
        page = WikiPage(
            page_id="topic:incident-response",
            bank_id="eng",
            kind="topic",
            title="Incident Response",
            content="## Incident Response\n\nContent here.",
            scope="incident-response",
            source_ids=["mem-1", "mem-2"],
            cross_links=["topic:deployment-pipeline"],
            revision=1,
            revised_at=now,
        )
        assert page.page_id == "topic:incident-response"
        assert page.revision == 1
        assert page.tags is None
        assert page.metadata is None

    def test_kind_literals(self) -> None:
        for kind in ("entity", "topic", "concept"):
            page = WikiPage(
                page_id=f"{kind}:test",
                bank_id="b",
                kind=kind,  # type: ignore[arg-type]
                title="Test",
                content="content",
                scope="test",
                source_ids=[],
                cross_links=[],
                revision=1,
                revised_at=datetime.now(UTC),
            )
            assert page.kind == kind


# ---------------------------------------------------------------------------
# W1: InMemoryWikiStore
# ---------------------------------------------------------------------------


class TestInMemoryWikiStore:
    def _page(self, page_id: str = "topic:test", bank_id: str = "bank1") -> WikiPage:
        return WikiPage(
            page_id=page_id,
            bank_id=bank_id,
            kind="topic",
            title="Test",
            content="## Test\n\nContent.",
            scope="test",
            source_ids=["m1"],
            cross_links=[],
            revision=1,
            revised_at=datetime.now(UTC),
        )

    async def test_upsert_and_get(self) -> None:
        store = InMemoryWikiStore()
        page = self._page()
        pid = await store.upsert_page(page, "bank1")
        assert pid == "topic:test"
        got = await store.get_page("topic:test", "bank1")
        assert got is not None
        assert got.revision == 1

    async def test_revision_increments_on_upsert(self) -> None:
        store = InMemoryWikiStore()
        page = self._page()
        await store.upsert_page(page, "bank1")
        # Upsert again with updated content
        updated = WikiPage(
            page_id=page.page_id,
            bank_id="bank1",
            kind="topic",
            title="Test Updated",
            content="## Test\n\nNew content.",
            scope="test",
            source_ids=["m1", "m2"],
            cross_links=[],
            revision=1,  # engine always passes 1; store increments
            revised_at=datetime.now(UTC),
        )
        await store.upsert_page(updated, "bank1")
        got = await store.get_page(page.page_id, "bank1")
        assert got is not None
        assert got.revision == 2
        assert got.title == "Test Updated"

    async def test_revision_history_preserved(self) -> None:
        store = InMemoryWikiStore()
        page = self._page()
        await store.upsert_page(page, "bank1")
        updated = WikiPage(
            page_id=page.page_id,
            bank_id="bank1",
            kind="topic",
            title="v2",
            content="v2 content",
            scope="test",
            source_ids=[],
            cross_links=[],
            revision=1,
            revised_at=datetime.now(UTC),
        )
        await store.upsert_page(updated, "bank1")
        history = store.revision_history(page.page_id, "bank1")
        assert len(history) == 1
        assert history[0].title == "Test"  # original

    async def test_bank_isolation(self) -> None:
        store = InMemoryWikiStore()
        await store.upsert_page(self._page("topic:x", "bank1"), "bank1")
        await store.upsert_page(self._page("topic:x", "bank2"), "bank2")
        got1 = await store.get_page("topic:x", "bank1")
        got2 = await store.get_page("topic:x", "bank2")
        assert got1 is not None
        assert got2 is not None

    async def test_get_missing_returns_none(self) -> None:
        store = InMemoryWikiStore()
        got = await store.get_page("topic:missing", "bank1")
        assert got is None

    async def test_list_pages_no_filter(self) -> None:
        store = InMemoryWikiStore()
        await store.upsert_page(self._page("topic:a", "b1"), "b1")
        await store.upsert_page(self._page("topic:b", "b1"), "b1")
        await store.upsert_page(self._page("topic:c", "b2"), "b2")
        pages = await store.list_pages("b1")
        assert len(pages) == 2

    async def test_list_pages_scope_filter(self) -> None:
        store = InMemoryWikiStore()
        p1 = WikiPage(
            page_id="topic:alpha",
            bank_id="b",
            kind="topic",
            title="Alpha",
            content="",
            scope="alpha",
            source_ids=[],
            cross_links=[],
            revision=1,
            revised_at=datetime.now(UTC),
        )
        p2 = WikiPage(
            page_id="topic:beta",
            bank_id="b",
            kind="topic",
            title="Beta",
            content="",
            scope="beta",
            source_ids=[],
            cross_links=[],
            revision=1,
            revised_at=datetime.now(UTC),
        )
        await store.upsert_page(p1, "b")
        await store.upsert_page(p2, "b")
        pages = await store.list_pages("b", scope="alpha")
        assert len(pages) == 1
        assert pages[0].scope == "alpha"

    async def test_delete_page(self) -> None:
        store = InMemoryWikiStore()
        page = self._page()
        await store.upsert_page(page, "bank1")
        deleted = await store.delete_page("topic:test", "bank1")
        assert deleted is True
        got = await store.get_page("topic:test", "bank1")
        assert got is None

    async def test_delete_missing_returns_false(self) -> None:
        store = InMemoryWikiStore()
        result = await store.delete_page("topic:nope", "bank1")
        assert result is False

    async def test_health(self) -> None:
        store = InMemoryWikiStore()
        status = await store.health()
        assert status.healthy is True


# ---------------------------------------------------------------------------
# W2: DBSCAN
# ---------------------------------------------------------------------------


class TestDbscan:
    def test_empty_input(self) -> None:
        assert _dbscan([]) == {}

    def test_single_cluster(self) -> None:
        # Three identical vectors → one cluster
        v = [1.0] + [0.0] * 127
        items = [(f"id{i}", v) for i in range(3)]
        labels = _dbscan(items, eps=0.01, min_samples=2)
        cluster_ids = set(labels.values())
        assert -1 not in cluster_ids
        assert len(cluster_ids) == 1

    def test_two_clusters(self) -> None:
        # Cluster A: vectors pointing in dimension 0
        v_a = [1.0] + [0.0] * 127
        # Cluster B: vectors pointing in dimension 64
        v_b = [0.0] * 64 + [1.0] + [0.0] * 63
        items = [(f"a{i}", v_a) for i in range(3)] + [(f"b{i}", v_b) for i in range(3)]
        labels = _dbscan(items, eps=0.1, min_samples=2)
        a_cluster = labels["a0"]
        b_cluster = labels["b0"]
        assert a_cluster != b_cluster
        assert a_cluster != -1
        assert b_cluster != -1

    def test_noise_points(self) -> None:
        # Two points that are far from each other and min_samples=3 → both noise
        v_a = [1.0] + [0.0] * 127
        v_b = [0.0] * 64 + [1.0] + [0.0] * 63
        items = [("a", v_a), ("b", v_b)]
        labels = _dbscan(items, eps=0.1, min_samples=3)
        assert labels["a"] == -1
        assert labels["b"] == -1

    def test_border_point_absorbed(self) -> None:
        # Core: 3 identical vectors. Border: 1 vector close to core but itself
        # has only 1 neighbour (below min_samples). Should be absorbed.
        core_v = [1.0] + [0.0] * 127
        # Slightly off-axis — cosine distance just within eps
        border_v = [0.99, 0.141] + [0.0] * 126
        norm = math.sqrt(sum(x * x for x in border_v))
        border_v = [x / norm for x in border_v]

        items = [(f"core{i}", list(core_v)) for i in range(3)] + [("border", border_v)]
        labels = _dbscan(items, eps=0.05, min_samples=2)
        # Border should be absorbed into the core cluster
        assert labels["border"] != -1
        assert labels["border"] == labels["core0"]


# ---------------------------------------------------------------------------
# W2: _infer_kind
# ---------------------------------------------------------------------------


class TestInferKind:
    def test_entity_prefixes(self) -> None:
        assert _infer_kind("entity:alice") == "entity"
        assert _infer_kind("person:bob") == "entity"
        assert _infer_kind("org:acme") == "entity"
        assert _infer_kind("location:singapore") == "entity"

    def test_concept_prefix(self) -> None:
        assert _infer_kind("concept:machine-learning") == "concept"

    def test_topic_default(self) -> None:
        assert _infer_kind("incident-response") == "topic"
        assert _infer_kind("deployment-pipeline") == "topic"


# ---------------------------------------------------------------------------
# W2: CompileEngine
# ---------------------------------------------------------------------------


class TestCompileEngine:
    def _engine(
        self,
        vs: InMemoryVectorStore | None = None,
        ws: InMemoryWikiStore | None = None,
        llm: MockLLMProvider | None = None,
    ) -> CompileEngine:
        return CompileEngine(
            vector_store=vs or InMemoryVectorStore(),
            llm_provider=llm or MockLLMProvider(default_response="## Test\n\nSynthesised content."),
            wiki_store=ws or InMemoryWikiStore(),
            dbscan_min_samples=2,
        )

    async def test_run_empty_bank_returns_zero_pages(self) -> None:
        engine = self._engine()
        result = await engine.run("empty-bank")
        assert result.pages_created == 0
        assert result.pages_updated == 0
        assert result.error is None

    async def test_explicit_scope_creates_page(self) -> None:
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        llm = MockLLMProvider(default_response="## Incident Response\n\nContent.")

        items = [
            _make_item("m1", "eng", "Server went down at 2am", 0.0, tags=["incident-response"]),
            _make_item("m2", "eng", "Alert fired on high latency", 1.0, tags=["incident-response"]),
        ]
        await vs.store_vectors(items)

        engine = self._engine(vs=vs, ws=ws, llm=llm)
        result = await engine.run("eng", scope="incident-response")

        assert result.pages_created == 1
        assert result.pages_updated == 0
        assert result.error is None
        assert "incident-response" in result.scopes_compiled

        page = await ws.get_page("topic:incident-response", "eng")
        assert page is not None
        assert page.scope == "incident-response"
        assert len(page.source_ids) == 2

    async def test_explicit_scope_updates_existing_page(self) -> None:
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        llm = MockLLMProvider(default_response="## Updated\n\nNew synthesis.")

        items = [_make_item("m1", "b", "Some text", 0.0, tags=["my-scope"])]
        await vs.store_vectors(items)

        engine = self._engine(vs=vs, ws=ws, llm=llm)
        await engine.run("b", scope="my-scope")  # creates
        result = await engine.run("b", scope="my-scope")  # updates

        assert result.pages_created == 0
        assert result.pages_updated == 1

        page = await ws.get_page("topic:my-scope", "b")
        assert page is not None
        assert page.revision == 2

    async def test_auto_discover_tagged_memories(self) -> None:
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        llm = MockLLMProvider(default_response="## Topic\n\nContent.")

        # Two tags, 2 memories each
        items = [
            _make_item("m1", "b", "Deploy pipeline step 1", 0.0, tags=["deployment"]),
            _make_item("m2", "b", "Deploy pipeline step 2", 1.0, tags=["deployment"]),
            _make_item("m3", "b", "On-call rotation setup", 10.0, tags=["oncall"]),
            _make_item("m4", "b", "On-call escalation policy", 11.0, tags=["oncall"]),
        ]
        await vs.store_vectors(items)

        engine = self._engine(vs=vs, ws=ws, llm=llm)
        result = await engine.run("b")

        assert result.error is None
        assert result.pages_created == 2
        assert set(result.scopes_compiled) == {"deployment", "oncall"}

    async def test_auto_discover_untagged_clusters_via_dbscan(self) -> None:
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        # Use a cycling mock so the two clusters get distinct labels
        llm = _CyclingLLMProvider(["alerts", "deployments"])

        # Two tight clusters of untagged memories
        v_cluster_a = [1.0] + [0.0] * 127
        v_cluster_b = [0.0] * 64 + [1.0] + [0.0] * 63

        items = [
            VectorItem(id=f"a{i}", bank_id="b", vector=list(v_cluster_a), text=f"Alert {i}")
            for i in range(3)
        ] + [
            VectorItem(id=f"b{i}", bank_id="b", vector=list(v_cluster_b), text=f"Deploy {i}")
            for i in range(3)
        ]
        await vs.store_vectors(items)

        engine = CompileEngine(
            vector_store=vs,
            llm_provider=llm,
            wiki_store=ws,
            dbscan_eps=0.1,
            dbscan_min_samples=2,
        )
        result = await engine.run("b")

        assert result.error is None
        assert result.noise_memories == 0
        # Two clusters → two scopes compiled; both should be creates
        assert len(result.scopes_compiled) == 2
        assert result.pages_created + result.pages_updated == 2

    async def test_noise_memories_counted(self) -> None:
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        llm = MockLLMProvider(default_response="some-topic")

        # 3 cluster members + 1 isolated noise point
        v_cluster = [1.0] + [0.0] * 127
        v_noise = [0.0] * 63 + [1.0] + [0.0] * 64  # orthogonal

        items = [
            VectorItem(id=f"c{i}", bank_id="b", vector=list(v_cluster), text=f"Cluster {i}")
            for i in range(3)
        ] + [
            VectorItem(id="noise", bank_id="b", vector=list(v_noise), text="Isolated memory")
        ]
        await vs.store_vectors(items)

        engine = CompileEngine(
            vector_store=vs,
            llm_provider=llm,
            wiki_store=ws,
            dbscan_eps=0.1,
            dbscan_min_samples=2,
        )
        result = await engine.run("b")

        assert result.noise_memories == 1
        assert result.pages_created == 1

    async def test_compiled_items_excluded_from_scope_discovery(self) -> None:
        """Previously compiled VectorItems (memory_layer='compiled') must not
        feed back into the next compile cycle."""
        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        llm = MockLLMProvider(default_response="## Topic\n\nContent.")

        raw = [_make_item("m1", "b", "Raw memory", 0.0, tags=["t1"])]
        compiled = [
            VectorItem(
                id="wiki:topic:t1",
                bank_id="b",
                vector=[1.0] + [0.0] * 127,
                text="[WIKI:topic] T1\n\nPrevious synthesis.",
                memory_layer="compiled",
                fact_type="wiki",
                tags=["t1"],
            )
        ]
        await vs.store_vectors(raw + compiled)

        engine = self._engine(vs=vs, ws=ws, llm=llm)
        await engine.run("b", scope="t1")

        # Only 1 source_id (the raw memory), not 2
        page = await ws.get_page("topic:t1", "b")
        assert page is not None
        assert len(page.source_ids) == 1
        assert page.source_ids[0] == "m1"


# ---------------------------------------------------------------------------
# W2: brain.compile() integration
# ---------------------------------------------------------------------------


class TestBrainCompile:
    def _brain_with_stores(self) -> tuple[Astrocyte, InMemoryVectorStore, InMemoryWikiStore]:
        from astrocyte.pipeline.orchestrator import PipelineOrchestrator

        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()
        llm = MockLLMProvider(default_response="## Page\n\nSynthesised.")

        brain = Astrocyte.from_config_dict({"banks": {"eng": {}}})
        pipeline = PipelineOrchestrator(vector_store=vs, llm_provider=llm)
        brain.set_pipeline(pipeline)
        brain.set_wiki_store(ws)
        return brain, vs, ws

    async def test_compile_no_wiki_store_raises(self) -> None:
        brain = Astrocyte.from_config_dict({})
        with pytest.raises(ConfigError, match="WikiStore"):
            await brain.compile("b")

    async def test_compile_no_pipeline_raises(self) -> None:
        brain = Astrocyte.from_config_dict({})
        brain.set_wiki_store(InMemoryWikiStore())
        with pytest.raises(Exception):  # ProviderUnavailable
            await brain.compile("b")

    async def test_compile_returns_result(self) -> None:
        brain, vs, ws = self._brain_with_stores()

        items = [
            _make_item("m1", "eng", "Server down at 2am", 0.0, tags=["incident"]),
            _make_item("m2", "eng", "High latency alert", 1.0, tags=["incident"]),
        ]
        await vs.store_vectors(items)

        result = await brain.compile("eng", scope="incident")

        assert isinstance(result, CompileResult)
        assert result.error is None
        assert result.pages_created == 1
        assert result.bank_id == "eng"

    async def test_compile_page_stored_in_wiki_store(self) -> None:
        brain, vs, ws = self._brain_with_stores()

        items = [_make_item("m1", "eng", "Deploy step", 0.0, tags=["deploy"])]
        await vs.store_vectors(items)

        await brain.compile("eng", scope="deploy")

        page = await ws.get_page("topic:deploy", "eng")
        assert page is not None
        assert page.bank_id == "eng"

    async def test_compile_page_embedded_in_vector_store(self) -> None:
        """Compiled pages should be stored as VectorItems with memory_layer='compiled'."""
        brain, vs, ws = self._brain_with_stores()

        items = [_make_item("m1", "eng", "Some memory", 0.0, tags=["scope1"])]
        await vs.store_vectors(items)

        await brain.compile("eng", scope="scope1")

        all_vectors = await vs.list_vectors("eng", limit=100)
        compiled = [v for v in all_vectors if v.memory_layer == "compiled"]
        assert len(compiled) == 1
        assert compiled[0].fact_type == "wiki"
