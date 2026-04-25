"""M8: LLM wiki compile — CompileEngine.

CompileEngine synthesises WikiPage entries from raw memories. Supported triggers:

- **Explicit**: ``brain.compile(scope, bank_id)`` — caller provides scope string.
  All memories tagged with that scope are compiled into one wiki page.

- **Automatic**: ``brain.compile(bank_id)`` — scope discovered from the memory
  bank itself via two passes:
    1. Tagged memories → group by tag (each tag = one compile scope).
    2. Untagged memories → DBSCAN clustering on stored embedding vectors;
       each cluster is labelled with a lightweight LLM call.

The engine is async and non-blocking. Raw memories are never modified —
WikiPages are additive artefacts with provenance back to ``source_ids``.

Vector embeddings of compiled pages are stored in the VectorStore with
``memory_layer="compiled"`` and ``fact_type="wiki"``, so the standard
recall pipeline can search them via ``search_similar`` without changes.

Design reference: docs/_design/llm-wiki-compile.md §3, §3.1, §3.2.
"""

from __future__ import annotations

import math
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from astrocyte.types import (
    CompileResult,
    CompileScope,
    Message,
    VectorItem,
    WikiPage,
    WikiPageKind,
)

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, VectorStore, WikiStore

# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_COMPILE_SYSTEM_PROMPT = """\
You are a knowledge compiler. Given a collection of memories about a topic, \
synthesise a clear, structured wiki page in markdown.

Rules:
- Start with a ## Title (the topic name)
- Organise content into logical ### sections
- Preserve factual accuracy — do not invent or extrapolate beyond the memories given
- Note any contradictions or uncertainties explicitly (e.g. "Conflicting information: …")
- Keep cross-references minimal but useful (e.g. "See also: deployment-pipeline")
- Output only the markdown content, no preamble or meta-commentary
"""

_CLUSTER_LABEL_SYSTEM_PROMPT = """\
You are a topic classifier. Given a set of short text fragments, identify the \
single most specific topic they share in common.

Output only a short slug: 2–4 words, lowercase, hyphenated. No explanation.
Examples: "incident-response", "alice-background", "deployment-pipeline"
"""

# ---------------------------------------------------------------------------
# Pure-Python DBSCAN (cosine distance, no external deps)
# ---------------------------------------------------------------------------


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _dbscan(
    items: list[tuple[str, list[float]]],
    eps: float = 0.25,
    min_samples: int = 3,
) -> dict[str, int]:
    """DBSCAN over cosine distance. No external dependencies.

    Args:
        items: List of (id, embedding_vector) pairs.
        eps: Maximum cosine distance for two points to be neighbours.
             cosine_distance = 1 - cosine_similarity.
        min_samples: Minimum neighbourhood size for a core point.

    Returns:
        Mapping of item_id → cluster_id. cluster_id == -1 means noise.
    """
    n = len(items)
    if n == 0:
        return {}

    ids = [item[0] for item in items]
    vectors = [item[1] for item in items]

    # Pre-compute neighbourhoods: points within eps cosine distance
    neighbours: list[list[int]] = []
    for i in range(n):
        nbrs = [
            j
            for j in range(n)
            if i != j and (1.0 - _cosine_sim(vectors[i], vectors[j])) <= eps
        ]
        neighbours.append(nbrs)

    labels = [-1] * n
    visited = [False] * n
    cluster_id = 0

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True

        if len(neighbours[i]) < min_samples:
            labels[i] = -1  # noise — may be absorbed later by a cluster
            continue

        # Expand cluster from core point i
        labels[i] = cluster_id
        seeds: set[int] = set(neighbours[i])

        while seeds:
            j = seeds.pop()
            if not visited[j]:
                visited[j] = True
                if len(neighbours[j]) >= min_samples:
                    seeds.update(neighbours[j])
            if labels[j] == -1:
                labels[j] = cluster_id  # border point absorbed into cluster

        cluster_id += 1

    return {ids[i]: labels[i] for i in range(n)}


# ---------------------------------------------------------------------------
# CompileEngine
# ---------------------------------------------------------------------------


class CompileEngine:
    """Compiles raw memories into WikiPage entries (M8 LLM wiki compile).

    Usage::

        engine = CompileEngine(vector_store, llm_provider, wiki_store)

        # Explicit compile — caller provides scope
        result = await engine.run("eng-team", scope="incident-response")

        # Automatic compile — scope discovered from tags + DBSCAN
        result = await engine.run("eng-team")
    """

    def __init__(
        self,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        wiki_store: WikiStore,
        *,
        dbscan_eps: float = 0.25,
        dbscan_min_samples: int = 3,
        compile_model: str | None = None,
        max_memories_per_scope: int = 100,
    ) -> None:
        self._vs = vector_store
        self._llm = llm_provider
        self._ws = wiki_store
        self._dbscan_eps = dbscan_eps
        self._dbscan_min_samples = dbscan_min_samples
        self._compile_model = compile_model
        self._max_per_scope = max_memories_per_scope

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        bank_id: str,
        scope: str | None = None,
    ) -> CompileResult:
        """Compile memories into WikiPages for ``bank_id``.

        Args:
            bank_id: Bank to compile.
            scope: If provided, compile only memories tagged with this scope.
                   If ``None``, trigger full scope discovery (tags + DBSCAN).

        Returns:
            :class:`~astrocyte.types.CompileResult` with pages created/updated,
            noise memory count, and token usage. On error, ``error`` is set and
            counts reflect partial progress.
        """
        start = time.monotonic()
        tokens_used = 0
        pages_created = 0
        pages_updated = 0
        noise_memories = 0
        scopes_compiled: list[str] = []

        try:
            # Fetch all raw memories once — reused across scope resolution and synthesis
            all_items = await self._fetch_raw_memories(bank_id)

            if scope is not None:
                compile_scopes = self._resolve_explicit(all_items, scope)
            else:
                compile_scopes, noise_memories = await self._discover_scopes(all_items)

            item_map = {item.id: item for item in all_items}

            for cs in compile_scopes:
                relevant = [
                    item_map[mid]
                    for mid in cs.memory_ids[: self._max_per_scope]
                    if mid in item_map
                ]
                page, tokens = await self._synthesise(cs, relevant, bank_id)
                tokens_used += tokens

                existing = await self._ws.get_page(page.page_id, bank_id)
                await self._ws.upsert_page(page, bank_id)

                # Embed the compiled page and store in VectorStore for recall tiering.
                # Stored with memory_layer="compiled" and fact_type="wiki" so the
                # standard search_similar path can filter for wiki pages.
                try:
                    vecs = await self._llm.embed([f"{page.title}\n\n{page.content[:1000]}"])
                    if vecs:
                        await self._vs.store_vectors(
                            [
                                VectorItem(
                                    id=page.page_id,
                                    bank_id=bank_id,
                                    vector=vecs[0],
                                    text=f"[WIKI:{page.kind}] {page.title}\n\n{page.content[:500]}",
                                    tags=page.tags,
                                    memory_layer="compiled",
                                    fact_type="wiki",
                                )
                            ]
                        )
                except Exception:
                    pass  # embedding failure is non-fatal; page is still stored

                if existing is None:
                    pages_created += 1
                else:
                    pages_updated += 1
                scopes_compiled.append(cs.scope)

        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return CompileResult(
                bank_id=bank_id,
                scopes_compiled=scopes_compiled,
                pages_created=pages_created,
                pages_updated=pages_updated,
                noise_memories=noise_memories,
                tokens_used=tokens_used,
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return CompileResult(
            bank_id=bank_id,
            scopes_compiled=scopes_compiled,
            pages_created=pages_created,
            pages_updated=pages_updated,
            noise_memories=noise_memories,
            tokens_used=tokens_used,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Scope resolution
    # ------------------------------------------------------------------

    def _resolve_explicit(
        self,
        all_items: list[VectorItem],
        scope: str,
    ) -> list[CompileScope]:
        """Resolve an explicit caller-provided scope.

        Selects memories whose tags include ``scope``. If no tagged memories
        match, falls back to all memories in the bank (caller is compiling
        an untagged bank with a named scope).
        """
        matching_ids = [
            item.id for item in all_items if item.tags and scope in item.tags
        ]
        if not matching_ids:
            matching_ids = [item.id for item in all_items]
        return [CompileScope(scope=scope, source="explicit", memory_ids=matching_ids)]

    async def _discover_scopes(
        self,
        all_items: list[VectorItem],
    ) -> tuple[list[CompileScope], int]:
        """Discover compile scopes from memory content.

        Pass 1 — Tagged memories: group by tag. Each unique tag with at least
        ``dbscan_min_samples`` memories becomes a compile scope.

        Pass 2 — Untagged memories: DBSCAN on stored embedding vectors.
        Each cluster is labelled with a lightweight LLM call. Noise points
        (memories DBSCAN could not assign to any cluster) are counted and
        held for the next compile cycle.

        Returns:
            (list of CompileScope, noise_count)
        """
        if not all_items:
            return [], 0

        # Pass 1: tag grouping
        tag_groups: dict[str, list[str]] = {}
        untagged: list[VectorItem] = []

        for item in all_items:
            if item.tags:
                for tag in item.tags:
                    tag_groups.setdefault(tag, []).append(item.id)
            else:
                untagged.append(item)

        scopes: list[CompileScope] = [
            CompileScope(scope=tag, source="tag", memory_ids=ids)
            for tag, ids in tag_groups.items()
            if len(ids) >= self._dbscan_min_samples
        ]

        # Pass 2: DBSCAN on untagged memories
        noise_count = 0
        if untagged:
            vector_pairs = [
                (item.id, item.vector)
                for item in untagged
                if item.vector  # guard against items with no vector
            ]
            if vector_pairs:
                labels = _dbscan(
                    vector_pairs,
                    eps=self._dbscan_eps,
                    min_samples=self._dbscan_min_samples,
                )

                cluster_groups: dict[int, list[str]] = {}
                for item_id, cluster_label in labels.items():
                    if cluster_label == -1:
                        noise_count += 1
                    else:
                        cluster_groups.setdefault(cluster_label, []).append(item_id)

                untagged_map = {item.id: item for item in untagged}
                for cluster_label, memory_ids in cluster_groups.items():
                    # Label via LLM using up to 5 representative memories
                    samples = [
                        untagged_map[mid]
                        for mid in memory_ids[:5]
                        if mid in untagged_map
                    ]
                    label = await self._label_cluster(samples)
                    scopes.append(
                        CompileScope(
                            scope=label,
                            source="cluster",
                            memory_ids=memory_ids,
                        )
                    )

        return scopes, noise_count

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    async def _synthesise(
        self,
        cs: CompileScope,
        relevant: list[VectorItem],
        bank_id: str,
    ) -> tuple[WikiPage, int]:
        """Synthesise a WikiPage from the memories in a CompileScope."""
        tokens_used = 0

        if not relevant:
            page = _empty_page(cs, bank_id)
            return page, 0

        memory_texts = "\n".join(
            f"[Memory {i + 1}]: {item.text[:400]}"
            for i, item in enumerate(relevant)
        )
        user_prompt = f"Topic: {cs.scope}\n\nMemories:\n{memory_texts}"

        messages = [
            Message(role="system", content=_COMPILE_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ]

        completion = await self._llm.complete(
            messages,
            model=self._compile_model,
            max_tokens=2048,
        )

        if completion.usage:
            tokens_used = completion.usage.input_tokens + completion.usage.output_tokens

        # Collect tags from contributing memories (deduplicated, ordered)
        seen_tags: set[str] = set()
        page_tags: list[str] = []
        for item in relevant:
            for tag in item.tags or []:
                if tag not in seen_tags:
                    seen_tags.add(tag)
                    page_tags.append(tag)

        kind = _infer_kind(cs.scope)
        page_id = f"{kind}:{cs.scope.replace(' ', '-')}"

        page = WikiPage(
            page_id=page_id,
            bank_id=bank_id,
            kind=kind,
            title=cs.scope.replace("-", " ").title(),
            content=completion.text,
            scope=cs.scope,
            source_ids=[item.id for item in relevant],
            cross_links=[],  # extracted in a future lint pass
            revision=1,
            revised_at=datetime.now(UTC),
            tags=page_tags or None,
        )
        return page, tokens_used

    async def _label_cluster(self, items: list[VectorItem]) -> str:
        """Generate a topic slug for a DBSCAN cluster via a lightweight LLM call."""
        snippets = "\n".join(f"- {item.text[:150]}" for item in items)
        messages = [
            Message(role="system", content=_CLUSTER_LABEL_SYSTEM_PROMPT),
            Message(role="user", content=f"Texts:\n{snippets}"),
        ]
        try:
            completion = await self._llm.complete(messages, max_tokens=20)
            label = completion.text.strip().lower().replace(" ", "-")[:50]
            return label or "general"
        except Exception:
            return f"cluster-{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    async def _fetch_raw_memories(self, bank_id: str) -> list[VectorItem]:
        """Fetch all raw (non-compiled) memories from the VectorStore."""
        all_items: list[VectorItem] = []
        offset = 0
        batch = 100
        while True:
            chunk = await self._vs.list_vectors(bank_id, offset=offset, limit=batch)
            if not chunk:
                break
            # Exclude previously compiled pages (stored with memory_layer="compiled")
            raw = [
                item
                for item in chunk
                if item.memory_layer != "compiled" and item.fact_type != "wiki"
            ]
            all_items.extend(raw)
            if len(chunk) < batch:
                break
            offset += batch
        return all_items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_kind(scope: str) -> WikiPageKind:
    """Infer WikiPage kind from scope string prefix conventions."""
    for prefix in ("entity:", "person:", "org:", "location:"):
        if scope.startswith(prefix):
            return "entity"
    if scope.startswith("concept:"):
        return "concept"
    return "topic"


def _empty_page(cs: CompileScope, bank_id: str) -> WikiPage:
    kind = _infer_kind(cs.scope)
    title = cs.scope.replace("-", " ").title()
    return WikiPage(
        page_id=f"{kind}:{cs.scope.replace(' ', '-')}",
        bank_id=bank_id,
        kind=kind,
        title=title,
        content=f"## {title}\n\nNo memories found for this scope.",
        scope=cs.scope,
        source_ids=[],
        cross_links=[],
        revision=1,
        revised_at=datetime.now(UTC),
    )
