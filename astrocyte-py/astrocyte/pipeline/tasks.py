"""Async memory task contract and handlers.

This module is the framework-facing half of the Postgres task design. The
production backend can persist these ``MemoryTask`` rows in ``astrocyte_tasks``;
tests and single-process deployments can use ``InMemoryTaskBackend``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from astrocyte.eval.failure_analysis import (
    analyze_failures,
    load_benchmark_result,
    stable_question_slice,
)
from astrocyte.pipeline.temporal import temporal_metadata
from astrocyte.types import (
    Entity,
    EntityLink,
    MemoryEntityAssociation,
    Message,
    VectorItem,
    WikiPage,
)

if TYPE_CHECKING:
    from astrocyte.pipeline.compile import CompileEngine
    from astrocyte.pipeline.lint import LintEngine
    from astrocyte.provider import GraphStore, LLMProvider, VectorStore, WikiStore

TaskStatus = Literal["queued", "running", "succeeded", "failed", "dead"]

COMPILE_BANK = "compile_bank"
COMPILE_PERSONA_PAGE = "compile_persona_page"
INDEX_WIKI_PAGE_VECTOR = "index_wiki_page_vector"
PROJECT_ENTITY_EDGES = "project_entity_edges"
NORMALIZE_TEMPORAL_FACTS = "normalize_temporal_facts"
LINT_WIKI_PAGE = "lint_wiki_page"
ANALYZE_BENCHMARK_FAILURES = "analyze_benchmark_failures"


@dataclass
class MemoryTask:
    """A durable background task for memory quality work."""

    task_type: str
    bank_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = "queued"
    idempotency_key: str | None = None
    attempts: int = 0
    max_attempts: int = 5
    run_after: datetime = field(default_factory=lambda: datetime.now(UTC))
    result: dict[str, Any] | None = None
    error: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TaskBackend(Protocol):
    """Storage contract for task queues, implemented by Postgres in production."""

    async def enqueue(self, task: MemoryTask) -> str: ...
    async def claim(self, worker_id: str, limit: int = 10) -> list[MemoryTask]: ...
    async def complete(self, task_id: str, result: dict[str, Any]) -> None: ...
    async def fail(self, task_id: str, error: str, retry_at: datetime | None) -> None: ...


class InMemoryTaskBackend:
    """Deterministic task backend for unit tests and local development."""

    def __init__(self) -> None:
        self._tasks: dict[str, MemoryTask] = {}

    async def enqueue(self, task: MemoryTask) -> str:
        if task.idempotency_key is not None:
            for existing in self._tasks.values():
                if (
                    existing.task_type == task.task_type
                    and existing.idempotency_key == task.idempotency_key
                    and existing.status in {"queued", "running"}
                ):
                    return existing.id
        self._tasks[task.id] = task
        return task.id

    async def claim(self, worker_id: str, limit: int = 10) -> list[MemoryTask]:
        now = datetime.now(UTC)
        queued = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status == "queued" and task.run_after <= now
            ),
            key=lambda task: (task.run_after, task.created_at),
        )
        claimed: list[MemoryTask] = []
        for task in queued[:limit]:
            task.status = "running"
            task.claimed_by = worker_id
            task.claimed_at = now
            task.updated_at = now
            task.attempts += 1
            claimed.append(task)
        return claimed

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        task = self._tasks[task_id]
        task.status = "succeeded"
        task.result = result
        task.error = None
        task.updated_at = datetime.now(UTC)

    async def fail(self, task_id: str, error: str, retry_at: datetime | None) -> None:
        task = self._tasks[task_id]
        task.error = error
        task.updated_at = datetime.now(UTC)
        if retry_at is None or task.attempts >= task.max_attempts:
            task.status = "dead"
            return
        task.status = "queued"
        task.run_after = retry_at

    def get(self, task_id: str) -> MemoryTask | None:
        return self._tasks.get(task_id)


@dataclass
class TaskHandlerContext:
    """Dependencies available to task handlers."""

    vector_store: VectorStore
    llm_provider: LLMProvider
    wiki_store: WikiStore | None = None
    graph_store: GraphStore | None = None
    compile_engine: CompileEngine | None = None
    lint_engine: LintEngine | None = None


class MemoryTaskDispatcher:
    """Dispatches task rows to memory-quality handlers."""

    def __init__(self, context: TaskHandlerContext) -> None:
        self._ctx = context

    async def run(self, task: MemoryTask) -> dict[str, Any]:
        if task.task_type == COMPILE_BANK:
            return await self._compile_bank(task)
        if task.task_type == COMPILE_PERSONA_PAGE:
            return await self._compile_persona_page(task)
        if task.task_type == INDEX_WIKI_PAGE_VECTOR:
            return await self._index_wiki_page_vector(task)
        if task.task_type == PROJECT_ENTITY_EDGES:
            return await self._project_entity_edges(task)
        if task.task_type == NORMALIZE_TEMPORAL_FACTS:
            return await self._normalize_temporal_facts(task)
        if task.task_type == LINT_WIKI_PAGE:
            return await self._lint_wiki_page(task)
        if task.task_type == ANALYZE_BENCHMARK_FAILURES:
            return await self._analyze_benchmark_failures(task)
        raise ValueError(f"Unknown memory task type: {task.task_type}")

    async def _compile_bank(self, task: MemoryTask) -> dict[str, Any]:
        if self._ctx.compile_engine is None:
            raise ValueError("compile_bank requires CompileEngine")
        result = await self._ctx.compile_engine.run(
            task.bank_id,
            scope=_optional_str(task.payload.get("scope")),
        )
        return {
            "bank_id": result.bank_id,
            "scopes_compiled": result.scopes_compiled,
            "pages_created": result.pages_created,
            "pages_updated": result.pages_updated,
            "noise_memories": result.noise_memories,
            "tokens_used": result.tokens_used,
            "elapsed_ms": result.elapsed_ms,
            "error": result.error,
        }

    async def _compile_persona_page(self, task: MemoryTask) -> dict[str, Any]:
        if self._ctx.wiki_store is None:
            raise ValueError("compile_persona_page requires WikiStore")
        person = _optional_str(task.payload.get("person"))
        source_ids = [str(value) for value in task.payload.get("source_ids", []) if value]
        items = await _list_bank_vectors(self._ctx.vector_store, task.bank_id)
        relevant = [
            item for item in items
            if _item_matches_person(item, person) and (not source_ids or item.id in source_ids)
        ]
        if person is None:
            names = sorted({name for item in items for name in _item_person_names(item)})
            if not names:
                return {"pages_created": 0, "pages_updated": 0, "reason": "no_person_metadata"}
            created = 0
            updated = 0
            page_ids: list[str] = []
            for name in names:
                subtask = MemoryTask(
                    task_type=COMPILE_PERSONA_PAGE,
                    bank_id=task.bank_id,
                    payload={"person": name},
                )
                result = await self._compile_persona_page(subtask)
                created += int(result.get("pages_created", 0))
                updated += int(result.get("pages_updated", 0))
                page_ids.extend(result.get("page_ids", []))
            return {"pages_created": created, "pages_updated": updated, "page_ids": page_ids}

        page_id = f"person:{_slug(person)}"
        existing = await self._ctx.wiki_store.get_page(page_id, task.bank_id)
        page = await self._build_persona_page(task.bank_id, person, relevant, existing)
        await self._ctx.wiki_store.upsert_page(page, task.bank_id)
        result = {
            "pages_created": 1 if existing is None else 0,
            "pages_updated": 0 if existing is None else 1,
            "page_ids": [page_id],
            "source_count": len(relevant),
        }
        if task.payload.get("index_vector"):
            index_result = await self._index_wiki_page_vector(
                MemoryTask(
                    task_type=INDEX_WIKI_PAGE_VECTOR,
                    bank_id=task.bank_id,
                    payload={"page_id": page_id},
                )
            )
            result.update(index_result)
        return result

    async def _index_wiki_page_vector(self, task: MemoryTask) -> dict[str, Any]:
        if self._ctx.wiki_store is None:
            raise ValueError("index_wiki_page_vector requires WikiStore")
        page_id = _required_str(task.payload, "page_id")
        page = await self._ctx.wiki_store.get_page(page_id, task.bank_id)
        if page is None:
            raise ValueError(f"Wiki page not found: {page_id}")
        vectors = await self._ctx.llm_provider.embed([f"{page.title}\n\n{page.content[:1000]}"])
        await self._ctx.vector_store.store_vectors(
            [
                VectorItem(
                    id=page.page_id,
                    bank_id=task.bank_id,
                    vector=vectors[0],
                    text=f"[WIKI:{page.kind}] {page.title}\n\n{page.content[:500]}",
                    metadata={"_wiki_source_ids": json.dumps(page.source_ids)},
                    tags=page.tags,
                    fact_type="wiki",
                    memory_layer="compiled",
                    retained_at=datetime.now(UTC),
                )
            ]
        )
        return {"indexed_page_id": page_id, "source_count": len(page.source_ids)}

    async def _project_entity_edges(self, task: MemoryTask) -> dict[str, Any]:
        if self._ctx.graph_store is None:
            raise ValueError("project_entity_edges requires GraphStore")
        items = await _list_bank_vectors(self._ctx.vector_store, task.bank_id)
        entity_by_name: dict[str, Entity] = {}
        memory_names: dict[str, set[str]] = {}
        for item in items:
            names = _item_person_names(item)
            if not names:
                continue
            memory_names[item.id] = names
            for name in names:
                entity_by_name.setdefault(
                    name,
                    Entity(
                        id=f"person:{_slug(name)}",
                        name=name,
                        entity_type="PERSON",
                        aliases=[name],
                        metadata={"source": "task:project_entity_edges"},
                    ),
                )

        entities = list(entity_by_name.values())
        entity_ids = await self._ctx.graph_store.store_entities(entities, task.bank_id)
        id_by_name = dict(zip(entity_by_name.keys(), entity_ids, strict=False))

        associations = [
            MemoryEntityAssociation(memory_id=memory_id, entity_id=id_by_name[name])
            for memory_id, names in memory_names.items()
            for name in names
            if name in id_by_name
        ]
        if associations:
            await self._ctx.graph_store.link_memories_to_entities(associations, task.bank_id)

        links: list[EntityLink] = []
        for item in items:
            names = sorted(memory_names.get(item.id, set()))
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    links.append(
                        EntityLink(
                            entity_a=id_by_name[names[i]],
                            entity_b=id_by_name[names[j]],
                            link_type="co_occurs",
                            evidence=item.text[:500],
                            confidence=1.0,
                            created_at=datetime.now(UTC),
                            metadata={
                                "memory_id": item.id,
                                "session_id": str((item.metadata or {}).get("session_id") or ""),
                                "turn_ids": str((item.metadata or {}).get("locomo_turn_ids") or ""),
                            },
                        )
                    )
        if links:
            await self._ctx.graph_store.store_links(links, task.bank_id)
        return {
            "entities_projected": len(entities),
            "associations_projected": len(associations),
            "links_projected": len(links),
        }

    async def _normalize_temporal_facts(self, task: MemoryTask) -> dict[str, Any]:
        items = await _list_bank_vectors(self._ctx.vector_store, task.bank_id)
        updated: list[VectorItem] = []
        for item in items:
            metadata = dict(item.metadata or {})
            normalized = temporal_metadata(item.text, item.occurred_at)
            if not normalized:
                continue
            metadata.update(normalized)
            updated.append(
                VectorItem(
                    id=item.id,
                    bank_id=item.bank_id,
                    vector=item.vector,
                    text=item.text,
                    metadata=metadata,
                    tags=item.tags,
                    fact_type=item.fact_type,
                    occurred_at=item.occurred_at,
                    memory_layer=item.memory_layer,
                    retained_at=item.retained_at,
                )
            )
        if updated:
            await self._ctx.vector_store.store_vectors(updated)
        return {"memories_scanned": len(items), "memories_updated": len(updated)}

    async def _lint_wiki_page(self, task: MemoryTask) -> dict[str, Any]:
        if self._ctx.lint_engine is None:
            raise ValueError("lint_wiki_page requires LintEngine")
        result = await self._ctx.lint_engine.run(task.bank_id)
        page_id = _optional_str(task.payload.get("page_id"))
        issues = result.issues
        if page_id is not None:
            issues = [issue for issue in issues if issue.page_id == page_id]
        return {
            "bank_id": result.bank_id,
            "pages_checked": result.pages_checked,
            "stale_count": sum(1 for issue in issues if issue.kind == "stale"),
            "orphan_count": sum(1 for issue in issues if issue.kind == "orphan"),
            "contradiction_count": sum(1 for issue in issues if issue.kind == "contradiction"),
            "issues": [
                {
                    "kind": issue.kind,
                    "page_id": issue.page_id,
                    "action": issue.action,
                    "detail": issue.detail,
                    "peer_page_id": issue.peer_page_id,
                }
                for issue in issues
            ],
            "elapsed_ms": result.elapsed_ms,
            "error": result.error,
        }

    async def _analyze_benchmark_failures(self, task: MemoryTask) -> dict[str, Any]:
        result_path = Path(_required_str(task.payload, "result_path"))
        result = load_benchmark_result(result_path)
        analysis = analyze_failures(result)
        analysis["stable_question_slice"] = stable_question_slice(
            result,
            size=int(task.payload.get("slice_size", 200)),
            seed=str(task.payload.get("seed", "locomo-v1")),
        )
        output_path = task.payload.get("output_path")
        if output_path:
            Path(str(output_path)).write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")
        return analysis

    async def _build_persona_page(
        self,
        bank_id: str,
        person: str,
        items: list[VectorItem],
        existing: WikiPage | None,
    ) -> WikiPage:
        source_ids = [item.id for item in items]
        evidence = "\n".join(f"- {item.text[:500]}" for item in items[:50])
        prompt = (
            f"Compile a concise persona/preference wiki page for {person}.\n"
            "Preserve stable facts: goals, likes, dislikes, relationships, plans, repeated activities, constraints.\n"
            "Use only the evidence.\n\n"
            f"Evidence:\n{evidence}"
        )
        completion = await self._ctx.llm_provider.complete(
            [
                Message(role="system", content="You maintain source-grounded person memory pages."),
                Message(role="user", content=prompt),
            ],
            max_tokens=1200,
            temperature=0.0,
        )
        content = completion.text.strip() or f"## {person}\n\nNo stable persona facts compiled."
        page_id = f"person:{_slug(person)}"
        return WikiPage(
            page_id=page_id,
            bank_id=bank_id,
            kind="entity",
            title=person,
            content=content,
            scope=page_id,
            source_ids=source_ids,
            cross_links=[],
            revision=(existing.revision + 1) if existing else 1,
            revised_at=datetime.now(UTC),
            tags=["persona", f"person:{_slug(person)}"],
            metadata={"task_type": COMPILE_PERSONA_PAGE},
        )


class MemoryTaskWorker:
    """Small worker loop for backends that implement the task contract."""

    def __init__(
        self,
        backend: TaskBackend,
        dispatcher: MemoryTaskDispatcher,
        *,
        worker_id: str,
        retry_delay_seconds: int = 60,
    ) -> None:
        self._backend = backend
        self._dispatcher = dispatcher
        self._worker_id = worker_id
        self._retry_delay = retry_delay_seconds

    async def run_once(self, *, limit: int = 10) -> int:
        tasks = await self._backend.claim(self._worker_id, limit=limit)
        for task in tasks:
            try:
                result = await self._dispatcher.run(task)
            except Exception as exc:
                retry_at = datetime.now(UTC) + timedelta(seconds=self._retry_delay)
                await self._backend.fail(task.id, str(exc), retry_at)
            else:
                await self._backend.complete(task.id, result)
        return len(tasks)


async def _list_bank_vectors(vector_store: VectorStore, bank_id: str) -> list[VectorItem]:
    items: list[VectorItem] = []
    offset = 0
    while True:
        batch = await vector_store.list_vectors(bank_id, offset=offset, limit=500)
        if not batch:
            return items
        items.extend(batch)
        offset += len(batch)


def _item_person_names(item: VectorItem) -> set[str]:
    names: set[str] = set()
    metadata = item.metadata or {}
    for key in ("person", "locomo_persons", "locomo_speakers", "speaker"):
        raw = metadata.get(key)
        if raw:
            names.update(part.strip() for part in str(raw).replace("|", ",").split(",") if part.strip())
    names.update(match.group(0) for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b", item.text))
    return {name for name in names if name.lower() not in {"the", "this", "that"}}


def _item_matches_person(item: VectorItem, person: str | None) -> bool:
    if person is None:
        return True
    return person.lower() in {name.lower() for name in _item_person_names(item)}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = _optional_str(payload.get(key))
    if value is None:
        raise ValueError(f"Task payload requires {key!r}")
    return value
