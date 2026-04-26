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
