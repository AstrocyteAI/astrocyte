"""FinanceBenchClient — Astrocyte Document Engine adapter for FinanceBench.

Handles two retrieval strategies, selectable at construction time:

  strategy="vector"
    PDF → markdown (pymupdf fallback; markitdown when Phase A ships) →
    build_markdown_tree → AdaptiveSummarizer → DocumentIngestor →
    memory.retain() per node → memory.recall() at query time.

  strategy="tree_search"
    Same ingest path, PLUS saves tree to DocumentStore →
    DocumentNavigator.search() at query time (requires Phase C/D).

PDF parsing fallback order:
  1. MarkitdownParser  (Phase A — not yet built; import-guarded)
  2. pymupdf/fitz      (already in bench-runner-deps; produces ## Page N sections)

Cross-engine isolation is preserved: DocumentIngestor uses opaque
metadata strings; the Memory Engine never sees a DocumentTree.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from astrocyte.documents.builders.md_builder import build_markdown_tree
from astrocyte.documents.builders.summarizer import AdaptiveSummarizer
from astrocyte.documents.ingestor import DocumentIngestor
from astrocyte.documents.types import Document
from astrocyte.types import Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Phase A import — MarkitdownParser (not yet built)
# ---------------------------------------------------------------------------
try:
    from astrocyte.documents.parsers.markitdown import MarkitdownParser as _MarkitdownParser  # type: ignore[import-not-found]
    _MARKITDOWN_AVAILABLE = True
except ImportError:
    _MARKITDOWN_AVAILABLE = False

# ---------------------------------------------------------------------------
# Optional Phase C/D import — DocumentRetriever + DocumentNavigator
# ---------------------------------------------------------------------------
try:
    from astrocyte.documents.retrieval.retriever import DocumentRetriever  # type: ignore[import-not-found]
    from astrocyte.documents.retrieval.navigator import DocumentNavigator  # type: ignore[import-not-found]
    _TREE_SEARCH_AVAILABLE = True
except ImportError:
    _TREE_SEARCH_AVAILABLE = False


# ---------------------------------------------------------------------------
# PDF → markdown (pymupdf fallback)
# ---------------------------------------------------------------------------

def _extract_markdown_pymupdf(pdf_path: Path) -> str:
    """Extract text from PDF via pymupdf, formatted as ## Page N sections.

    Produces one heading per page so build_markdown_tree creates a
    flat tree with one leaf per page. This is the fallback until
    MarkitdownParser (Phase A) ships — section boundaries at page
    boundaries rather than heading boundaries.
    """
    import pymupdf  # noqa: PLC0415 — bench-runner-deps

    doc = pymupdf.open(str(pdf_path))
    pages: list[str] = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if text:
            pages.append(f"## Page {i}\n\n{text}")
    doc.close()
    return "\n\n".join(pages)


async def _extract_markdown(pdf_path: Path) -> str:
    """Parse PDF to markdown — MarkitdownParser if available, pymupdf fallback."""
    if _MARKITDOWN_AVAILABLE:
        parser = _MarkitdownParser()
        return await parser.convert(pdf_path.read_bytes(), pdf_path.name)
    return _extract_markdown_pymupdf(pdf_path)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_summarizer_llm_call(provider: Any, model: str):
    """Adapt OpenAIProvider.complete → AdaptiveSummarizer's LlmCall signature."""

    async def call(prompt: str) -> str:
        completion = await provider.complete(
            messages=[Message(role="user", content=prompt)],
            model=model,
            max_tokens=512,
            temperature=0.0,
        )
        return completion.text or ""

    return call


def _make_chat_llm_call(provider: Any, model: str):
    """Return an async (system, user) -> str callable for answerer/judge."""

    async def call(system: str, user: str) -> str:
        completion = await provider.complete(
            messages=[
                Message(role="system", content=system),
                Message(role="user", content=user),
            ],
            model=model,
            max_tokens=256,
            temperature=0.0,
        )
        return completion.text or ""

    return call


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class FinanceBenchClient:
    """Astrocyte Document Engine adapter for FinanceBench.

    Call flow:
        client = FinanceBenchClient(...)
        async with client:
            doc_id = await client.ingest_pdf(pdf_path, doc_name)
            context = await client.retrieve(question, doc_name)
            answer  = await client.answer(question, context)
    """

    def __init__(
        self,
        *,
        strategy: str = "vector",
        bank_id: str = "financebench",
        tree_build_model: str = "gpt-4o-mini",
        answerer_model: str = "gpt-4o-mini",
        judge_model: str = "gpt-4o-mini",
    ) -> None:
        if strategy not in ("vector", "tree_search"):
            raise ValueError(f"strategy must be 'vector' or 'tree_search', got {strategy!r}")
        if strategy == "tree_search" and not _TREE_SEARCH_AVAILABLE:
            raise RuntimeError(
                "strategy='tree_search' requires Phase C/D "
                "(astrocyte.documents.retrieval is not yet implemented)."
            )
        self.strategy = strategy
        self.bank_id = bank_id
        self.tree_build_model = tree_build_model
        self.answerer_model = answerer_model
        self.judge_model = judge_model

        self._doc_store: Any | None = None
        self._provider: Any | None = None
        self._astrocyte: Any | None = None
        self._doc_ids: dict[str, str] = {}  # doc_name → document.id

    # ── lifecycle ────────────────────────────────────────────────────────

    async def _ensure_resources(self) -> None:
        if self._provider is not None:
            return

        from astrocyte_postgres.document_store import PostgresDocumentStore  # noqa: PLC0415
        from astrocyte.providers.openai import OpenAIProvider  # noqa: PLC0415

        dsn = os.environ.get("DATABASE_URL") or os.environ.get("ASTROCYTE_PG_DSN")
        if not dsn:
            raise RuntimeError(
                "FinanceBenchClient requires DATABASE_URL or ASTROCYTE_PG_DSN in env."
            )
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("FinanceBenchClient requires OPENAI_API_KEY in env.")

        self._doc_store = PostgresDocumentStore()
        self._provider = OpenAIProvider(api_key=os.environ["OPENAI_API_KEY"])

        # Astrocyte memory engine — for vector retain/recall path.
        # Reads DATABASE_URL + OPENAI_API_KEY from environment.
        from astrocyte import Astrocyte  # noqa: PLC0415
        from astrocyte.config import AstrocyteConfig  # noqa: PLC0415

        cfg = AstrocyteConfig()
        self._astrocyte = Astrocyte(config=cfg)

    async def close(self) -> None:
        for attr in ("_provider", "_doc_store", "_astrocyte"):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, "close"):
                try:
                    await obj.close()
                except Exception:  # noqa: BLE001
                    pass

    async def __aenter__(self) -> FinanceBenchClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── ingest ───────────────────────────────────────────────────────────

    async def ingest_pdf(self, pdf_path: Path, doc_name: str) -> str:
        """Parse PDF → tree → DocumentStore + Memory Engine.

        Returns the document.id assigned to this PDF.
        Idempotent: if doc_name was already ingested this session, returns
        the cached document.id without re-ingesting.
        """
        if doc_name in self._doc_ids:
            return self._doc_ids[doc_name]

        await self._ensure_resources()
        start = time.monotonic()

        # 1. Parse PDF → markdown
        md_text = await _extract_markdown(pdf_path)
        parser_name = "markitdown" if _MARKITDOWN_AVAILABLE else "pymupdf"

        # 2. Build Document + tree
        document = Document.new(
            source_uri=str(pdf_path),
            content=md_text,
            mime_type="application/pdf",
            title=doc_name,
        )
        tree = build_markdown_tree(md_text, document.id)

        # 3. Adaptive summarization — same 200-token gate as M17
        summarizer = AdaptiveSummarizer(
            _make_summarizer_llm_call(self._provider, self.tree_build_model),
            threshold_tokens=200,
        )
        await summarizer.summarize_tree(tree)

        # 4. Save tree to DocumentStore (enables tree-search)
        await self._doc_store.save_document(document, tree)

        # 5. Ingest into Memory Engine via DocumentIngestor (enables vector recall)
        ingestor = DocumentIngestor(retain=self._astrocyte.retain)
        result = await ingestor.ingest(tree, document, bank_id=self.bank_id)

        elapsed = time.monotonic() - start
        logger.info(
            "ingested %s via %s: nodes=%d emitted=%d %.1fs",
            doc_name,
            parser_name,
            tree.node_count(),
            result.segments_emitted,
            elapsed,
        )

        self._doc_ids[doc_name] = document.id
        return document.id

    # ── retrieval ────────────────────────────────────────────────────────

    async def retrieve(self, question: str, doc_name: str, *, top_k: int = 5) -> str:
        """Retrieve relevant context for a question from a specific document."""
        await self._ensure_resources()

        if self.strategy == "vector":
            return await self._retrieve_vector(question, top_k=top_k)
        else:
            doc_id = self._doc_ids.get(doc_name)
            if doc_id is None:
                raise RuntimeError(f"Document {doc_name!r} not ingested yet.")
            return await self._retrieve_tree_search(question, doc_id)

    async def _retrieve_vector(self, question: str, *, top_k: int = 5) -> str:
        hits = await self._astrocyte.recall(
            query=question,
            bank_id=self.bank_id,
            limit=top_k,
        )
        if not hits:
            return ""
        parts: list[str] = []
        for h in hits:
            title = (h.metadata or {}).get("tree_node_title", "")
            prefix = f"[{title}]\n" if title else ""
            parts.append(f"{prefix}{h.text}")
        return "\n\n---\n\n".join(parts)

    async def _retrieve_tree_search(self, question: str, doc_id: str) -> str:
        retriever = DocumentRetriever(self._doc_store)
        navigator = DocumentNavigator(
            retriever,
            _make_summarizer_llm_call(self._provider, self.tree_build_model),
        )
        result = await navigator.search(question, [doc_id])
        if not result.sections:
            return ""
        parts: list[str] = []
        for hit in result.sections:
            breadcrumb = " > ".join(hit.breadcrumb) if hit.breadcrumb else ""
            header = f"[{hit.node_title}]" + (f" ({breadcrumb})" if breadcrumb else "")
            parts.append(f"{header}\n{hit.text}")
        return "\n\n---\n\n".join(parts)

    # ── answer ───────────────────────────────────────────────────────────

    async def answer(self, question: str, context: str) -> str:
        """Generate an answer from retrieved context."""
        await self._ensure_resources()
        from scripts.financebench._prompts import ANSWERER_SYSTEM, ANSWERER_USER  # noqa: PLC0415

        if not context.strip():
            return "Not found in context."

        user_msg = ANSWERER_USER.format(context=context, question=question)
        llm_call = _make_chat_llm_call(self._provider, self.answerer_model)
        return await llm_call(ANSWERER_SYSTEM, user_msg)

    def judge_llm_call(self):
        """Return the judge LlmCall for _scoring.judge()."""
        return _make_chat_llm_call(self._provider, self.judge_model)
