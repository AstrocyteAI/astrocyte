"""Haystack (deepset) integration — Astrocytes as a retriever component.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.haystack import AstrocyteRetriever

    brain = Astrocyte.from_config("astrocyte.yaml")
    retriever = AstrocyteRetriever(brain, bank_id="knowledge-base")

    # Use in a Haystack pipeline
    pipe = Pipeline()
    pipe.add_component("retriever", retriever)
    pipe.add_component("reader", reader)
    pipe.connect("retriever.documents", "reader.documents")

    result = pipe.run({"retriever": {"query": "What is dark mode?"}})

Haystack uses a component pattern where each component has run() or arun()
methods with typed inputs/outputs. Retrievers return documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


@dataclass
class AstrocyteDocument:
    """Haystack-compatible document representation.

    Mirrors haystack.Document with content, meta, score, and id fields.
    """

    content: str
    meta: dict[str, Any]
    score: float
    id: str


class AstrocyteRetriever:
    """Astrocytes-backed retriever for Haystack pipelines.

    Implements Haystack's Retriever component pattern:
    - run(query, top_k) → {"documents": list[Document]}
    - Async via arun()

    Documents returned use AstrocyteDocument (compatible with haystack.Document).
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
        *,
        top_k: int = 10,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id
        self.top_k = top_k

    async def arun(
        self,
        query: str,
        *,
        top_k: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, list[AstrocyteDocument]]:
        """Async retrieval — returns {"documents": [...]}.

        Haystack pipeline connects this output to downstream components.
        """
        result = await self.brain.recall(
            query,
            bank_id=self.bank_id,
            max_results=top_k or self.top_k,
            tags=tags,
        )
        documents = [
            AstrocyteDocument(
                content=h.text,
                meta=dict(h.metadata) if h.metadata else {"source": "astrocyte"},
                score=h.score,
                id=h.memory_id or "",
            )
            for h in result.hits
        ]
        return {"documents": documents}

    def run(self, query: str, **kwargs: Any) -> dict[str, list[AstrocyteDocument]]:
        """Synchronous retrieval for Haystack pipeline compatibility."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, self.arun(query, **kwargs)).result()
        return asyncio.run(self.arun(query, **kwargs))


class AstrocyteWriter:
    """Astrocytes-backed document writer for Haystack pipelines.

    Implements a Writer component: run(documents) → {"written": count}.
    """

    def __init__(
        self,
        brain: Astrocyte,
        bank_id: str,
    ) -> None:
        self.brain = brain
        self.bank_id = bank_id

    async def arun(self, documents: list[AstrocyteDocument | dict[str, Any]]) -> dict[str, int]:
        """Write documents to Astrocytes memory."""
        written = 0
        for doc in documents:
            if isinstance(doc, AstrocyteDocument):
                content = doc.content
                meta = doc.meta
            elif isinstance(doc, dict):
                content = doc.get("content", "")
                meta = doc.get("meta", {})
            else:
                continue

            result = await self.brain.retain(
                content,
                bank_id=self.bank_id,
                metadata=meta,
                tags=["haystack"],
            )
            if result.stored:
                written += 1
        return {"written": written}
