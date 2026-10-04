"""Documents-only baseline: Operator Memory's model behind the AML contract.

Astrocyte has committed in public to comparing itself against a documents-only
memory system and publishing the result whichever way it goes
(``docs/_design/anchored-documents.md`` §0 decision 8, §9). This module is that
system. It is deliberately **not** Astrocyte: no embeddings, no vector store, no
fusion, no reranking. It follows Operator Memory
(github.com/aerovato/operator-memory, ``docs/architecture.md``):

- The brain is ordinary Markdown: documents plus a **catalog** in which each
  entry says what a document covers and when to open it.
- Every session starts from a deterministic preamble, which here is the
  catalog. Everything else is opened deliberately, following catalog guidance.
- The agent updates current knowledge **at its source**: it opens the documents
  a conversation affects and rewrites them, rather than appending fragments.

Mapped onto Add/Search, so it runs through the same harness, items, and judge
as every Astrocyte arm:

- ``Add``: the agent reads the catalog, decides which documents to open and
  which to create, reads them, and writes back their new contents and catalog
  entries (two model calls).
- ``Search``: returns the catalog, always loaded like the preamble, plus the
  documents the agent decides to open for the question (one model call). It
  selects documents and never answers, as the AML contract requires.

Fairness rules: it uses the same LLM provider configuration as the Astrocyte
arm it is compared with, and its prompts carry Operator's own guidance (short,
coherent documents; update at the source; catalog entries with a coverage line
and an open-when condition). Nothing in it is specific to any benchmark.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrocyte.pipeline._json_tolerant import first_json_value
from astrocyte.types import Message
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from astrocyte_aml.app import (
    AddRequest,
    AddResponse,
    SearchItem,
    SearchRequest,
    SearchResponse,
    render_conversation,
)

logger = logging.getLogger("astrocyte.aml.docs_baseline")

#: Documents the agent may open per step. Operator relies on coherent documents
#: rather than many fragments; a bound keeps one step inside a model's context.
MAX_OPEN_DOCS = int(os.environ.get("DOCS_BASELINE_MAX_OPEN", "8"))
#: Operator's guidance: keep brain documents short and split long ones.
TARGET_DOC_CHARS = int(os.environ.get("DOCS_BASELINE_TARGET_DOC_CHARS", "3000"))

_SLUG = re.compile(r"[^a-z0-9-]+")

_GUIDANCE = f"""\
You maintain a Markdown knowledge base (the "brain") for one user, the way a \
careful engineer keeps documentation. Rules:
- Keep each document short and coherent (aim for under {TARGET_DOC_CHARS} \
characters); split a growing document into several with clear titles.
- Update knowledge at its source: when something changes, rewrite the \
document that holds it so it states what is true now. Do not append \
contradictory fragments. Keep dates: when a fact is tied to a time, write the \
date next to it.
- Every document has a catalog entry: a title, one line saying what it covers, \
and one line saying when to open it ("open if ...").
- Record what a reader would need later; skip pleasantries.
- Each message carries its own timestamp in brackets. That timestamp is "now" \
for that message: resolve relative times ("last week", "yesterday") against it \
and write absolute dates. Never use today's date or your own sense of the \
current date."""


def _slugify(text: str) -> str:
    slug = _SLUG.sub("-", text.lower()).strip("-")
    return slug[:60] or "note"


@dataclass
class CatalogEntry:
    slug: str
    title: str
    covers: str
    open_if: str


class Brain:
    """One bank's brain on disk: ``catalog.json`` plus ``docs/<slug>.md``.

    The catalog is stored as JSON for exact round-tripping and rendered as
    Markdown for the model, matching Operator's ``catalog.md``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = root / "docs"

    def catalog(self) -> list[CatalogEntry]:
        path = self.root / "catalog.json"
        if not path.exists():
            return []
        return [CatalogEntry(**e) for e in json.loads(path.read_text(encoding="utf-8"))]

    def render_catalog(self) -> str:
        entries = self.catalog()
        if not entries:
            return "# Catalog\n\n(empty: no documents yet)"
        lines = ["# Catalog", ""]
        for e in entries:
            lines.append(f"- `{e.slug}` **{e.title}**: {e.covers} Open if: {e.open_if}")
        return "\n".join(lines)

    def read(self, slug: str) -> str | None:
        path = self.docs / f"{_slugify(slug)}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write(self, entry: CatalogEntry, content: str) -> None:
        self.docs.mkdir(parents=True, exist_ok=True)
        (self.docs / f"{entry.slug}.md").write_text(content, encoding="utf-8")
        entries = [e for e in self.catalog() if e.slug != entry.slug] + [entry]
        (self.root / "catalog.json").write_text(
            json.dumps([e.__dict__ for e in entries], indent=1), encoding="utf-8"
        )


class DocsOnlyMemory:
    """The baseline memory system: one brain per bank, updated by an LLM agent."""

    def __init__(self, llm: Any, root: Path) -> None:
        self.llm = llm
        self.root = root
        self._locks: dict[str, asyncio.Lock] = {}

    def brain(self, bank_id: str) -> Brain:
        return Brain(self.root / _slugify(bank_id.replace(":", "-")))

    async def _json(self, prompt: str) -> dict[str, Any]:
        completion = await self.llm.complete(
            [
                Message(role="system", content=_GUIDANCE),
                Message(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        value = first_json_value(completion.text or "", dict)
        if value is None:
            raise ValueError(
                f"model returned no JSON object: {(completion.text or '')[:200]!r}"
            )
        return value

    async def add(self, bank_id: str, conversation: str) -> int:
        """Document one conversation batch. Returns how many documents were written."""
        lock = self._locks.setdefault(bank_id, asyncio.Lock())
        async with lock:  # one writer per brain, like one agent per workspace
            brain = self.brain(bank_id)
            plan = await self._json(
                f"{brain.render_catalog()}\n\n# New conversation\n\n{conversation}\n\n"
                "Which existing documents does this conversation affect, and which new "
                f"documents are needed? Open at most {MAX_OPEN_DOCS} existing documents. "
                'Answer as JSON: {"open": ["<slug>", ...], "create": ["<short title>", ...]}. '
                'If nothing is worth recording, answer {"open": [], "create": []}.'
            )
            known = {e.slug for e in brain.catalog()}
            opened = [
                s for s in plan.get("open", []) if isinstance(s, str) and s in known
            ][:MAX_OPEN_DOCS]
            created = [
                t for t in plan.get("create", []) if isinstance(t, str) and t.strip()
            ]
            if not opened and not created:
                return 0
            docs = "\n\n".join(
                f"## Document `{s}`\n\n{brain.read(s) or ''}" for s in opened
            )
            edits = await self._json(
                f"{brain.render_catalog()}\n\n# Opened documents\n\n{docs or '(none)'}\n\n"
                f"# New conversation\n\n{conversation}\n\n"
                f"Documents to create: {json.dumps(created)}\n\n"
                "Write the full new contents of every document you change or create. Answer "
                'as JSON: {"documents": [{"slug": "<existing slug, or a new short slug>", '
                '"title": "...", "covers": "...", "open_if": "...", "content": "<Markdown>"}]}.'
            )
            written = 0
            for doc in edits.get("documents", []):
                if (
                    not isinstance(doc, dict)
                    or not str(doc.get("content") or "").strip()
                ):
                    continue
                title = str(doc.get("title") or doc.get("slug") or "note").strip()
                entry = CatalogEntry(
                    slug=_slugify(str(doc.get("slug") or title)),
                    title=title,
                    covers=str(doc.get("covers") or "").strip(),
                    open_if=str(doc.get("open_if") or "").strip(),
                )
                content = str(doc["content"])
                if len(content) > 2 * TARGET_DOC_CHARS:
                    logger.info(
                        "docs baseline: %s is %d chars (target %d)",
                        entry.slug,
                        len(content),
                        TARGET_DOC_CHARS,
                    )
                brain.write(entry, content)
                written += 1
            return written

    async def search(self, bank_id: str, query: str, limit: int) -> list[SearchItem]:
        """The catalog (the preamble), then the documents the agent opens for ``query``."""
        brain = self.brain(bank_id)
        catalog = brain.catalog()
        items = [SearchItem(id="catalog", content=brain.render_catalog())]
        if not catalog:
            return items
        choice = await self._json(
            f"{brain.render_catalog()}\n\n# Question\n\n{query}\n\n"
            f"Which documents should be opened to answer this? At most {MAX_OPEN_DOCS}. "
            'Answer as JSON: {"open": ["<slug>", ...]}. Do not answer the question.'
        )
        known = {e.slug: e for e in catalog}
        for slug in [
            s for s in choice.get("open", []) if isinstance(s, str) and s in known
        ]:
            if len(items) >= max(1, limit):
                break
            text = brain.read(slug)
            if text:
                items.append(
                    SearchItem(id=slug, content=f"# {known[slug].title}\n\n{text}")
                )
        return items


class _Health(BaseModel):
    status: str


def create_docs_app(memory: DocsOnlyMemory | None = None) -> FastAPI:
    """The baseline as an AML-contract service. Local self-evaluation only.

    Without ``memory``, builds one from ``ASTROCYTE_CONFIG_PATH`` (its
    ``llm_provider``, the same configuration the Astrocyte arm uses) and
    ``DOCS_BASELINE_ROOT`` (where brains are written).
    """
    app = FastAPI(title="Documents-only baseline (Operator Memory model)", version="1")
    app.state.memory = memory

    def _memory() -> DocsOnlyMemory:
        if app.state.memory is None:
            from astrocyte.config import load_config
            from astrocyte.wiring import resolve_llm_provider

            config = load_config(os.environ["ASTROCYTE_CONFIG_PATH"])
            root = Path(os.environ.get("DOCS_BASELINE_ROOT", "docs-baseline-brains"))
            app.state.memory = DocsOnlyMemory(resolve_llm_provider(config), root)
        return app.state.memory

    @app.get("/health", response_model=_Health)
    async def health() -> _Health:
        try:
            _memory()
        except Exception as exc:  # any failure means not ready
            raise HTTPException(status_code=503, detail=f"not ready: {exc}") from exc
        return _Health(status="ok")

    @app.post("/add", response_model=AddResponse)
    async def add(req: AddRequest) -> AddResponse:
        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must be non-empty")
        try:
            await _memory().add(req.user_id, render_conversation(req.messages))
        except Exception as exc:
            logger.exception("docs baseline add failed")
            raise HTTPException(
                status_code=500, detail=f"documenting failed: {exc}"
            ) from exc
        return AddResponse(
            success=True,
            request_id=req.request_id,
            user_id=req.user_id,
            session_id=req.session_id,
        )

    @app.post("/search", response_model=SearchResponse)
    async def search(req: SearchRequest) -> SearchResponse:
        query = (
            req.query if not req.options else f"{req.query}\n" + "\n".join(req.options)
        )
        try:
            items = await _memory().search(req.user_id, query, req.top_k)
        except Exception as exc:
            logger.exception("docs baseline search failed")
            raise HTTPException(
                status_code=500, detail=f"search failed: {exc}"
            ) from exc
        return SearchResponse(data=items)

    return app
