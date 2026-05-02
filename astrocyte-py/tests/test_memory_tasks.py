"""Tests for async memory task handlers."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from astrocyte.pipeline.lint import LintEngine
from astrocyte.pipeline.tasks import (
    ANALYZE_BENCHMARK_FAILURES,
    COMPILE_PERSONA_PAGE,
    LINT_WIKI_PAGE,
    NORMALIZE_TEMPORAL_FACTS,
    PROJECT_ENTITY_EDGES,
    InMemoryTaskBackend,
    MemoryTask,
    MemoryTaskDispatcher,
    MemoryTaskWorker,
    TaskHandlerContext,
)
from astrocyte.testing.in_memory import (
    InMemoryGraphStore,
    InMemoryVectorStore,
    InMemoryWikiStore,
    MockLLMProvider,
)
from astrocyte.types import VectorItem, WikiPage


def _item(
    item_id: str,
    text: str,
    *,
    metadata: dict | None = None,
    occurred_at: datetime | None = None,
) -> VectorItem:
    return VectorItem(
        id=item_id,
        bank_id="b1",
        vector=[1.0] + [0.0] * 127,
        text=text,
        metadata=metadata,
        occurred_at=occurred_at,
    )


def _dispatcher(
    vector_store: InMemoryVectorStore,
    *,
    wiki_store: InMemoryWikiStore | None = None,
    graph_store: InMemoryGraphStore | None = None,
    llm: MockLLMProvider | None = None,
    lint_engine: LintEngine | None = None,
) -> MemoryTaskDispatcher:
    return MemoryTaskDispatcher(
        TaskHandlerContext(
            vector_store=vector_store,
            llm_provider=llm or MockLLMProvider(default_response="## Alice\n\nAlice likes hiking."),
            wiki_store=wiki_store,
            graph_store=graph_store,
            lint_engine=lint_engine,
        )
    )


async def test_in_memory_backend_idempotency_and_worker() -> None:
    backend = InMemoryTaskBackend()
    vector_store = InMemoryVectorStore()
    await vector_store.store_vectors([
        _item("m1", "Alice went hiking yesterday.", occurred_at=datetime(2026, 2, 10, tzinfo=UTC)),
    ])
    dispatcher = _dispatcher(vector_store)
    worker = MemoryTaskWorker(backend, dispatcher, worker_id="w1")

    first = await backend.enqueue(MemoryTask(
        task_type=NORMALIZE_TEMPORAL_FACTS,
        bank_id="b1",
        idempotency_key="normalize:b1",
    ))
    second = await backend.enqueue(MemoryTask(
        task_type=NORMALIZE_TEMPORAL_FACTS,
        bank_id="b1",
        idempotency_key="normalize:b1",
    ))

    assert first == second
    assert await worker.run_once(limit=1) == 1
    assert backend.get(first).status == "succeeded"


async def test_normalize_temporal_facts_updates_vector_metadata() -> None:
    vector_store = InMemoryVectorStore()
    await vector_store.store_vectors([
        _item("m1", "Alice went hiking yesterday.", occurred_at=datetime(2026, 2, 10, tzinfo=UTC)),
    ])

    result = await _dispatcher(vector_store).run(MemoryTask(
        task_type=NORMALIZE_TEMPORAL_FACTS,
        bank_id="b1",
    ))

    stored = (await vector_store.list_vectors("b1"))[0]
    assert result["memories_updated"] == 1
    assert stored.metadata["resolved_date"] == "2026-02-09"


async def test_compile_persona_page_and_index_vector() -> None:
    vector_store = InMemoryVectorStore()
    wiki_store = InMemoryWikiStore()
    await vector_store.store_vectors([
        _item("m1", "Alice likes hiking.", metadata={"locomo_persons": "Alice"}),
        _item("m2", "Bob likes chess.", metadata={"locomo_persons": "Bob"}),
    ])
    dispatcher = _dispatcher(vector_store, wiki_store=wiki_store)

    compile_result = await dispatcher.run(MemoryTask(
        task_type=COMPILE_PERSONA_PAGE,
        bank_id="b1",
        payload={"person": "Alice", "index_vector": True},
    ))

    page = await wiki_store.get_page("person:alice", "b1")
    indexed = await vector_store.list_vectors("b1")
    assert compile_result["source_count"] == 1
    assert page is not None
    assert page.source_ids == ["m1"]
    assert compile_result["indexed_page_id"] == "person:alice"
    assert any(item.id == "person:alice" and item.fact_type == "wiki" for item in indexed)


async def test_compile_persona_page_with_scope_uses_scoped_page_id_and_tag() -> None:
    """Persona pages built with a scope must store at the scoped page_id
    AND carry the scope tag, so that:
      (a) the same person across distinct scopes does not collapse to a
          single diluted page (the upsert key matches the lookup key), and
      (b) scoped recall queries — which filter by ``convo:<id>`` — can
          still surface the persona page.

    This locks in the LoCoMo regression fix where
    ``_build_persona_page`` previously hardcoded an unscoped page_id and
    omitted the scope tag.
    """
    vector_store = InMemoryVectorStore()
    wiki_store = InMemoryWikiStore()
    # Stamp the scope tag the same way the LoCoMo retain path does so
    # ``_item_in_scope`` can match.
    await vector_store.store_vectors([
        VectorItem(
            id="m1",
            bank_id="b1",
            vector=[1.0] + [0.0] * 127,
            text="Alice in convo-A likes hiking.",
            metadata={"locomo_persons": "Alice", "conversation_id": "convo-A"},
            tags=["convo:convo-A"],
        ),
        VectorItem(
            id="m2",
            bank_id="b1",
            vector=[1.0] + [0.0] * 127,
            text="Alice in convo-B likes chess.",
            metadata={"locomo_persons": "Alice", "conversation_id": "convo-B"},
            tags=["convo:convo-B"],
        ),
    ])

    dispatcher = _dispatcher(vector_store, wiki_store=wiki_store)

    # Compile persona for Alice scoped to convo-A.
    await dispatcher.run(MemoryTask(
        task_type=COMPILE_PERSONA_PAGE,
        bank_id="b1",
        payload={
            "person": "Alice",
            "scope": "convo:convo-A",
            "index_vector": True,
        },
    ))
    # And again for convo-B — must NOT collapse onto convo-A's page.
    await dispatcher.run(MemoryTask(
        task_type=COMPILE_PERSONA_PAGE,
        bank_id="b1",
        payload={
            "person": "Alice",
            "scope": "convo:convo-B",
            "index_vector": True,
        },
    ))

    page_a = await wiki_store.get_page("person:convo-convo-a:alice", "b1")
    page_b = await wiki_store.get_page("person:convo-convo-b:alice", "b1")
    legacy = await wiki_store.get_page("person:alice", "b1")

    # Each scope produced its own page, keyed at the scoped id.
    assert page_a is not None, "scoped page A should be retrievable at the scoped page_id"
    assert page_b is not None, "scoped page B should be retrievable at the scoped page_id"
    # And the unscoped page_id was NOT used as a backdoor write target.
    assert legacy is None, "scoped compile must not also write to the unscoped page_id"

    # Source isolation: the scope filter prevents cross-conversation bleed.
    assert page_a.source_ids == ["m1"]
    assert page_b.source_ids == ["m2"]

    # The page carries the scope tag so scoped recall (which filters by
    # ``convo:convo-A``) can surface it.
    assert "convo:convo-A" in page_a.tags
    assert "convo:convo-B" in page_b.tags

    # And the indexed VectorItem inherits that tag (so vector recall
    # filtered by tag finds the persona page).
    indexed = await vector_store.list_vectors("b1")
    indexed_a = next(v for v in indexed if v.id == "person:convo-convo-a:alice")
    indexed_b = next(v for v in indexed if v.id == "person:convo-convo-b:alice")
    assert "convo:convo-A" in (indexed_a.tags or [])
    assert "convo:convo-B" in (indexed_b.tags or [])


async def test_project_entity_edges_uses_person_metadata() -> None:
    vector_store = InMemoryVectorStore()
    graph_store = InMemoryGraphStore()
    await vector_store.store_vectors([
        _item(
            "m1",
            "Alice talked with Bob about hiking.",
            metadata={"locomo_persons": "Alice,Bob", "session_id": "s1", "locomo_turn_ids": "dia_1"},
        ),
    ])

    result = await _dispatcher(vector_store, graph_store=graph_store).run(MemoryTask(
        task_type=PROJECT_ENTITY_EDGES,
        bank_id="b1",
    ))

    assert result["entities_projected"] == 2
    assert result["associations_projected"] == 2
    assert result["links_projected"] == 1


async def test_lint_wiki_page_returns_filtered_issues() -> None:
    vector_store = InMemoryVectorStore()
    wiki_store = InMemoryWikiStore()
    page = WikiPage(
        page_id="topic:stale",
        bank_id="b1",
        kind="topic",
        title="Stale",
        content="## Stale",
        scope="stale",
        source_ids=["missing"],
        cross_links=[],
        revision=1,
        revised_at=datetime.now(UTC),
    )
    await wiki_store.upsert_page(page, "b1")
    lint_engine = LintEngine(vector_store, wiki_store)

    result = await _dispatcher(
        vector_store,
        wiki_store=wiki_store,
        lint_engine=lint_engine,
    ).run(MemoryTask(
        task_type=LINT_WIKI_PAGE,
        bank_id="b1",
        payload={"page_id": "topic:stale"},
    ))

    assert result["orphan_count"] == 1
    assert result["issues"][0]["action"] == "archive"


async def test_analyze_benchmark_failures_task(tmp_path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps({
            "per_question": [
                {
                    "question": "When did Alice hike?",
                    "expected_answer": "yesterday",
                    "category": "temporal",
                    "correct": False,
                    "_evidence_id_hit": True,
                    "_relevant_found": 1,
                    "_reciprocal_rank": 0.1,
                }
            ]
        }),
        encoding="utf-8",
    )

    result = await _dispatcher(InMemoryVectorStore()).run(MemoryTask(
        task_type=ANALYZE_BENCHMARK_FAILURES,
        bank_id="bench",
        payload={"result_path": str(result_path), "slice_size": 1},
    ))

    assert result["total_failed"] == 1
    assert result["buckets"]["temporal_normalization_miss"]["count"] == 1
    assert result["stable_question_slice"] == [0]
