"""AstrocyteClient — Mem0-harness-shaped adapter for Astrocyte.

M13.1: shim that matches the de-facto SPI of
``memory-benchmarks/benchmarks/common/mem0_client.Mem0Client`` so the
Mem0 runners (``benchmarks/locomo/run.py`` etc.) can drive Astrocyte
through their own dataset loaders, prompts, judges, and metrics.

Why a shim instead of forking the upstream harness: the SPI surface is
tiny (``add`` / ``search`` / ``delete_user`` + async context manager).
Re-implementing those against Astrocyte's stores keeps us in our repo
and avoids vendoring the whole memory-benchmarks codebase.

Design:

- **Lazy ingest**: ``add(messages, user_id, timestamp)`` only buffers
  in memory. The first ``search(query, user_id, ...)`` for a user
  triggers the full Astrocyte ingestion pipeline (markdown render →
  PageIndex tree → sections → embeddings → entities → facts → wiki).
- **Fact-grain search**: v1 returns top-K facts ranked by cross-encoder
  rerank. Sections + wiki + mental models are NOT surfaced — keeps the
  experiment focused on retrieval quality of a single primitive.
- **delete_user**: drops the user's bank in Astrocyte (and clears the
  in-memory buffer). Honours the harness's resume cleanup contract.

The adapter writes to Postgres via :class:`PostgresPageIndexStore`,
which requires ``DATABASE_URL`` / ``ASTROCYTE_PG_DSN`` in the env. For
isolation between Mem0-harness runs and our native bench runs, each
user gets its own ``bank_id`` namespace: ``m13.1:locomo:<user_id>``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the astrocyte package and the upstream PageIndex package are
# importable. Mirrors the path-management in scripts/bench_pageindex_*.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_PAGEINDEX_ROOT = Path("/Users/calvin/AstrocyteAI/PageIndex")
if str(_PAGEINDEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAGEINDEX_ROOT))

# M17 Phase 3: ingest-path selection
#
#   ASTROCYTE_M17_INGEST_PATH=document  (default, swaps external pageindex
#                                        for framework-native Document Engine)
#   ASTROCYTE_M17_INGEST_PATH=conversation  (NEW — routes through ConversationEngine
#                                            + ConversationIngestor + retain SPI;
#                                            this is the H1b experiment path)
#
# See docs/_design/m17-pageindex-ingestion.md.
import os as _os  # noqa: E402

_INGEST_PATH = _os.environ.get("ASTROCYTE_M17_INGEST_PATH", "document").lower()
_USE_CONVERSATION_ENGINE = _INGEST_PATH == "conversation"
# Backward-compat: keep the older env var meaning (framework-native Document Engine)
_USE_FRAMEWORK_TREE_BUILDER = (
    _os.environ.get(
        "ASTROCYTE_M17_FRAMEWORK_TREE_BUILDER",
        "1",
    )
    == "1"
)

if not _USE_FRAMEWORK_TREE_BUILDER and not _USE_CONVERSATION_ENGINE:
    # Legacy path — keep available for parity-comparison benches.
    from pageindex.page_index_md import md_to_tree  # noqa: E402,F401

from astrocyte.conversations import (  # noqa: E402
    Conversation,
    ConversationIngestor,
)
from astrocyte.documents.builders.md_builder import (  # noqa: E402
    build_markdown_tree,
)
from astrocyte.documents.builders.summarizer import (  # noqa: E402
    AdaptiveSummarizer,
)
from astrocyte.documents.types import DocumentTree, TreeNode  # noqa: E402
from astrocyte.types import Message, PageIndexDocument  # noqa: E402

logger = logging.getLogger(__name__)


def _isoformat_or_none(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _slice_section_around_line(md_text: str, line_num: int) -> str:
    """Return the markdown section starting at ``line_num`` until the
    next ``##`` header (or EOF). 1-indexed line numbers — mirrors
    ``bench_pageindex_locomo._slice_section_around_line``.
    """
    if not md_text:
        return ""
    lines = md_text.split("\n")
    if line_num < 1 or line_num > len(lines):
        return ""
    out_lines: list[str] = [lines[line_num - 1]]
    for line in lines[line_num:]:
        if line.startswith("## "):
            break
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def _truncate_chars(text: str, *, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` characters, appending an
    ellipsis when truncation actually happened. ~4 chars ≈ 1 token, so
    ``max_chars=2400`` ≈ 600-token cap.
    """
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rsplit("\n", 1)[0]
    return f"{cut}\n…"


def _slug_snake_case(text: str) -> str:
    """Lowercase + underscore-separated key, used by get_user_profile()
    so Mem0's prompt formatter renders titles back as Title Case.

    "Doctors visited" → "doctors_visited"
    "User's sleep habits" → "users_sleep_habits"
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return slug[:64]


# M17 Phase 3 helpers — framework-native tree builder produces an
# astrocyte.documents.DocumentTree, but the bench downstream
# (``_flatten_tree_to_sections``) expects PageIndex's nested-dict shape.
# These helpers bridge the two without touching the downstream pipeline.


def _make_summarizer_llm_call(provider: Any, model: str):
    """Adapt our LLMProvider.complete to AdaptiveSummarizer's ``LlmCall`` signature.

    AdaptiveSummarizer expects ``async (prompt: str) -> str``. The
    OpenAIProvider returns a Completion; we pluck its ``.text``.
    """

    async def call(prompt: str) -> str:
        completion = await provider.complete(
            messages=[Message(role="user", content=prompt)],
            model=model,
            max_tokens=512,
            temperature=0.0,
        )
        return completion.text or ""

    return call


# ── M17 Phase 3: Conversation Engine ingest path + retain SPI adapter ──
#
# The bench harness today inlines ``_populate_section_index`` rather than
# calling a Memory Engine ``retain(content, metadata)`` API. To bench the
# Conversation Engine we need a retain SPI that ConversationIngestor can
# call. ``_BenchRetainAdapter`` provides that — it buffers per-chunk
# (content, metadata) tuples, then on ``flush()`` synthesizes the section
# list + md_text the existing extraction pipeline expects.
#
# This keeps the extraction pipeline (entity extraction, embeddings, fact
# extraction, wiki generation) byte-identical between Document Engine and
# Conversation Engine paths. The only variable is the SHAPE of sections
# fed to extraction:
#   - Document Engine path: sections at heading boundaries (e.g. ## Session N)
#   - Conversation Engine path: sections at turn-aware chunk boundaries


class _BenchRetainAdapter:
    """retain SPI adapter that buffers chunks then flushes to bench extraction.

    Each ``retain(content, metadata)`` call appends one (content,
    metadata) tuple to an internal buffer. At ``flush(document_id)``
    time, the buffer is converted into:
      - one synthetic PageIndexSection per call (line_num auto-incremented)
      - one synthetic md_text where each chunk occupies its declared line range
    Then ``_populate_section_index`` runs over those — identical to the
    existing Document Engine path's downstream extraction.
    """

    def __init__(self) -> None:
        self._buffer: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self,
        *,
        bank_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        self._buffer.append((content, metadata))

    async def materialize(
        self,
        document_id: str,
        *,
        provider: Any | None = None,
        summarizer_model: str = "gpt-4o-mini",
    ) -> tuple[list[Any], str]:
        """Synthesize (sections, md_text) from the buffer.

        Each buffered (content, metadata) becomes one PageIndexSection.
        The md_text is the concatenation of all chunks with per-chunk
        headings — section line_num points to the heading line so the
        existing line_num-based content slicing in
        ``_populate_section_index`` works unchanged.

        Summary + speaker propagation (M17 follow-up):
        - Summaries are generated here via ``AdaptiveSummarizer`` (raw
          if chunk < threshold tokens, LLM otherwise). Without this the
          summary column was always NULL and ``section_embedding`` skipped
          every section, leaving the semantic strategy blind on the CE
          path.
        - Speaker is derived from ``metadata['turn_roles']``: a chunk
          containing only one role is tagged with that role; a mixed
          chunk that contains any assistant turn is tagged ``"assistant"``
          (so the LME ``speaker='assistant'`` keyword filter actually
          matches). Pure-user chunks are tagged ``"user"``.
        """
        # Local import to avoid bench-wide imports at module load time
        from scripts.bench_pageindex_locomo import (  # noqa: PLC0415
            PageIndexSection,
        )

        md_parts: list[str] = ["# Conversation (Conversation-Engine ingest)", ""]
        sections: list[Any] = []
        chunk_bodies: list[str] = []
        # md_text line numbers are 1-indexed; track cursor.
        cur_line = 3  # after "# Conversation" + blank
        for chunk_idx, (content, metadata) in enumerate(self._buffer):
            # Pattern: "## Session {date} — {topic_hint}". Discriminating
            # titles let the picker distinguish chunks by session date
            # and first-turn topic instead of seeing N identical
            # "## Conversation {user_id} ({idx})" headings.
            session_date_str = _format_session_date(metadata)
            topic_hint = _derive_chunk_topic_hint(
                content,
                metadata.get("turn_roles"),
            )
            heading_text = f"Session {session_date_str} — {topic_hint}"
            md_parts.append(f"## {heading_text}")
            md_parts.append("")
            heading_line = cur_line
            md_parts.append(content)
            md_parts.append("")
            # Bump cursor: heading(1) + blank(1) + body lines + blank(1)
            body_lines = content.count("\n") + 1
            cur_line += 2 + body_lines + 1

            speaker = _derive_chunk_speaker(metadata.get("turn_roles"))
            session_date = _parse_iso_timestamp(
                metadata.get("earliest_timestamp") or metadata.get("latest_timestamp"),
            )

            sections.append(
                PageIndexSection(
                    document_id=document_id,
                    line_num=heading_line,
                    node_id=metadata.get(
                        "turn_ids",
                        [str(uuid.uuid4())],
                    )[0]
                    if metadata.get("turn_ids")
                    else str(uuid.uuid4()),
                    title=heading_text,
                    summary=None,  # filled in below by AdaptiveSummarizer
                    speaker=speaker,
                    session_date=session_date,
                    parent_node=None,
                    depth=2,
                ),
            )
            chunk_bodies.append(content)

        md_text = "\n".join(md_parts)

        # Generate summaries per chunk so section_embedding has something
        # to embed. Mirrors the Document Engine path's AdaptiveSummarizer
        # call: < threshold_tokens uses raw text; otherwise one LLM call.
        if provider is not None and sections:
            summarizer = AdaptiveSummarizer(
                _make_summarizer_llm_call(provider, summarizer_model),
                threshold_tokens=200,
            )
            nodes = [
                TreeNode(
                    id=section.node_id or str(uuid.uuid4()),
                    parent_id=None,
                    depth=2,
                    title=section.title,
                    text=body,
                )
                for section, body in zip(sections, chunk_bodies)
            ]
            await asyncio.gather(
                *(summarizer.summarize_node(n) for n in nodes),
            )
            for section, node in zip(sections, nodes):
                if node.summary is not None and node.summary.text:
                    section.summary = node.summary.text

        return sections, md_text

    def clear(self) -> None:
        self._buffer.clear()


_QUESTION_STOPWORDS = {
    "What",
    "When",
    "Where",
    "Who",
    "Why",
    "How",
    "Which",
    "Whose",
    "Did",
    "Do",
    "Does",
    "Is",
    "Are",
    "Was",
    "Were",
    "Has",
    "Have",
    "Had",
    "Can",
    "Could",
    "Would",
    "Should",
    "Will",
    "Shall",
    "May",
    "Might",
    "The",
    "A",
    "An",
    "And",
    "Or",
    "But",
    "If",
    "Then",
    "So",
    "Of",
    "In",
    "On",
    "At",
    "To",
    "From",
    "By",
    "For",
    "With",
    "About",
    "As",
    "Into",
    "My",
    "Your",
    "His",
    "Her",
    "Their",
    "Our",
    "Its",
    "I",
    "Me",
    "You",
    "User",
    "Assistant",
}


def _extract_proper_noun_entities(question: str) -> list[str]:
    """Cheap proper-noun extraction (capitalized words minus question
    stopwords). Mirrors ``bench_pageindex_locomo._extract_question_entities``
    but lives here so the adapter doesn't import bench-specific helpers.
    Each token is emitted separately so multi-word names yield multiple
    entries — the entity-index lookup is case-insensitive ANY-match.
    """
    if not question:
        return []
    tokens = re.findall(r"\b[A-Z][a-zA-Z]+\b", question)
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in _QUESTION_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Coerce an ISO-8601 string back to tz-aware datetime; ``None``-safe.

    ConversationIngestor serializes chunk timestamps as isoformat strings
    when present, or ``None`` when no turn had a timestamp. The retain
    adapter rehydrates them here so downstream temporal recall has
    structured ``session_date`` to filter against.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _format_session_date(metadata: dict[str, Any]) -> str:
    """Format the chunk's earliest timestamp as ``YYYY-MM-DD`` (or ``no-date``).

    Used in chunk section headings so the picker can disambiguate
    same-topic chunks across sessions by date. The chunk-level
    timestamp comes from ConversationIngestor: ``earliest_timestamp``
    is the earliest turn ts; ``latest_timestamp`` is its sibling. We
    prefer earliest (start of the session) but fall back if missing.
    """
    ts = _parse_iso_timestamp(
        metadata.get("earliest_timestamp") or metadata.get("latest_timestamp"),
    )
    if ts is None:
        return "no-date"
    return ts.strftime("%Y-%m-%d")


_FIRST_TURN_RE = re.compile(r"^\*\*[^*]+\*\*:\s*(.*?)(?=\n\n\*\*|\Z)", re.DOTALL)


def _derive_chunk_topic_hint(
    content: str,
    turn_roles: list[str] | None,
) -> str:
    """Short topic descriptor for a chunk heading.

    Prefers the first turn's body (truncated to ~60 chars at a word
    boundary, newlines collapsed). Falls back to a role-mix descriptor
    when no first-turn body is available — keeps every chunk heading
    minimally discriminating so the picker sees distinct titles rather
    than 30× ``Conversation {user_id}``.
    """
    first_body = ""
    if content:
        m = _FIRST_TURN_RE.match(content)
        if m:
            first_body = " ".join(m.group(1).split())
    if first_body:
        if len(first_body) > 60:
            cut = first_body[:60].rsplit(" ", 1)[0] or first_body[:60]
            return f"{cut}…"
        return first_body
    if turn_roles:
        unique: list[str] = []
        seen: set[str] = set()
        for r in turn_roles:
            if r and r not in seen:
                seen.add(r)
                unique.append(r)
        if len(unique) == 1:
            return f"{unique[0]}-only"
        if unique:
            return "→".join(unique[:3])
    return "untitled"


def _derive_chunk_speaker(turn_roles: list[str] | None) -> str | None:
    """Pick a single section-grain speaker tag from a chunk's turn roles.

    Returns:
      - ``"user"`` if every turn is the user
      - ``"assistant"`` if every turn is the assistant, OR if the chunk
        is mixed and contains any assistant turn (so LME's
        ``speaker='assistant'`` keyword filter matches chunks where the
        assistant said something)
      - the sole unique role for any other single-role chunk
      - ``None`` for empty/missing role data
    """
    if not turn_roles:
        return None
    unique = {r for r in turn_roles if r}
    if not unique:
        return None
    if len(unique) == 1:
        return next(iter(unique))
    if "assistant" in unique:
        return "assistant"
    if "user" in unique:
        return "user"
    return None


def _node_to_pageindex_dict(node: TreeNode) -> dict[str, Any]:
    """Convert one TreeNode to PageIndex's nested-dict node shape.

    Keys produced (matching ``_flatten_tree_to_sections``'s expectations):
      - ``line_num``: int (1-indexed; required)
      - ``node_id``: str (the node UUID)
      - ``title``: str
      - ``summary`` or ``prefix_summary``: str (per summary_kind)
      - ``nodes``: list (children; only present if node has children)
    """
    out: dict[str, Any] = {
        "line_num": node.line_start if node.line_start is not None else 1,
        "node_id": node.id,
        "title": node.title,
    }
    if node.summary is not None:
        # PageIndex distinction: leaves get "summary"; internal nodes get "prefix_summary"
        if node.summary.kind == "prefix":
            out["prefix_summary"] = node.summary.text
        else:
            out["summary"] = node.summary.text
    if node.children:
        out["nodes"] = [_node_to_pageindex_dict(c) for c in node.children]
    return out


def _document_tree_to_pageindex_dict(tree: DocumentTree) -> dict[str, Any]:
    """Convert our DocumentTree → PageIndex-shape nested dict.

    Returns ``{"structure": [...]}`` matching what
    ``_flatten_tree_to_sections`` reads. Roots become top-level entries
    under ``structure``.
    """
    return {"structure": [_node_to_pageindex_dict(r) for r in tree.roots]}


def _format_messages_as_markdown(
    messages: list[dict[str, str]],
    *,
    session_idx: int,
    timestamp: int | None,
) -> str:
    """Render one ``add()`` call's messages as a Markdown session block.

    Mirrors the structure of ``bench_pageindex_locomo.conversation_to_markdown``
    but operates on the Mem0 input shape (``list[{role, content}]``)
    instead of LoCoMo's native session dict.
    """
    date_label = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        if timestamp is not None
        else "no-date"
    )
    lines = [f"## Session {session_idx} ({date_label})", ""]
    for msg in messages:
        role = msg.get("role", "user")
        content = str(msg.get("content", "")).replace("\n", " ").strip()
        lines.append(f"**{role}**: {content}")
    lines.append("")
    return "\n".join(lines)


class AstrocyteClient:
    """Mem0-shaped adapter over Astrocyte's PageIndex bench pipeline.

    Methods mirror ``Mem0Client`` so the Mem0-harness runners can drive
    Astrocyte unchanged at the import surface. ``Mem0Client`` constructor
    kwargs that don't apply to Astrocyte (cloud auth, event polling) are
    accepted and ignored — lets the runner CLI keep its --backend flag
    cheap.
    """

    def __init__(
        self,
        mode: str = "oss",  # accepted, ignored
        host: str | None = None,  # accepted, ignored
        api_key: str | None = None,  # accepted, ignored
        organization_id: str | None = None,  # accepted, ignored
        project_id: str | None = None,  # accepted, ignored
        max_retries: int = 5,
        retry_delay: float = 1.0,
        rpm: int = 60,  # accepted, ignored
        timeout: float = 300.0,  # accepted, ignored
        event_poll_interval: float = 0.5,  # accepted, ignored
        event_poll_timeout: float = 300.0,  # accepted, ignored
        *,
        bank_prefix: str = "m13.1:locomo",
        tree_build_model: str = "gpt-4o-mini",
        extraction_model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
        search_top_k_floor: int = 200,
    ) -> None:
        self.mode = mode
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.bank_prefix = bank_prefix
        self.tree_build_model = tree_build_model
        self.extraction_model = extraction_model
        self.embedding_model = embedding_model
        self.search_top_k_floor = search_top_k_floor

        # Per-user ingest state. Buffer holds (timestamp, messages) tuples.
        # ``_ingested`` flips to True after the lazy build completes.
        self._buffers: dict[str, list[tuple[int | None, list[dict[str, str]]]]] = defaultdict(list)
        self._ingested: set[str] = set()
        self._doc_ids: dict[str, str] = {}
        self._ingest_locks: dict[str, asyncio.Lock] = {}
        # M13.2: cache md_text per user so search() can slice section
        # excerpts around picker-returned line_nums without re-loading.
        self._md_text_by_user: dict[str, str] = {}
        # M17 follow-up: cache the document's reference date (latest
        # session timestamp) for query-time relative-temporal expansion.
        self._reference_date_by_user: dict[str, datetime] = {}

        # Resolve store + provider lazily on first use — avoids pulling
        # the Postgres pool open during __init__ when the runner is
        # still parsing CLI args.
        self._store: Any | None = None
        self._provider: Any | None = None
        self._mental_model_store: Any | None = None

    # ── lifecycle ───────────────────────────────────────────────────────

    async def _ensure_resources(self) -> None:
        if self._store is not None and self._provider is not None:
            return
        from astrocyte_postgres import (  # noqa: PLC0415
            PostgresMentalModelStore,
            PostgresPageIndexStore,
        )

        from astrocyte.providers.openai import OpenAIProvider  # noqa: PLC0415

        if not (os.environ.get("DATABASE_URL") or os.environ.get("ASTROCYTE_PG_DSN")):
            raise RuntimeError(
                "AstrocyteClient requires DATABASE_URL or ASTROCYTE_PG_DSN; "
                "run via `doppler run -- env DATABASE_URL=... ...`.",
            )
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("AstrocyteClient requires OPENAI_API_KEY in env.")

        self._store = PostgresPageIndexStore(bootstrap_schema=True)
        self._mental_model_store = PostgresMentalModelStore(bootstrap_schema=True)
        self._provider = OpenAIProvider(api_key=os.environ["OPENAI_API_KEY"])

    async def close(self) -> None:
        # Provider + store may have their own resources to drain. Be
        # defensive — neither shape is guaranteed.
        for attr in ("_provider", "_store", "_mental_model_store"):
            obj = getattr(self, attr, None)
            if obj is not None and hasattr(obj, "close"):
                try:
                    await obj.close()
                except Exception:  # noqa: BLE001
                    pass

    async def __aenter__(self) -> AstrocyteClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── add ─────────────────────────────────────────────────────────────

    async def add(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        observation_date: str | None = None,
        timestamp: int | None = None,
        custom_instructions: str | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Buffer messages for lazy ingestion.

        Returns Mem0-shaped ``{"results": [{"memory": str, "event": "ADD"}]}``.
        We synthesise one ``ADD`` result per non-empty message so the
        harness's debug log still has structure to print, but the actual
        fact extraction is deferred until ``search()`` first fires.
        """
        if observation_date is not None and timestamp is None:
            try:
                dt = datetime.strptime(observation_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                timestamp = int(dt.timestamp())
            except ValueError:
                timestamp = None
        self._buffers[user_id].append((timestamp, list(messages)))
        # Once new messages arrive, the bank for this user becomes
        # stale — re-trigger lazy ingest on the next search.
        self._ingested.discard(user_id)
        # Return a faux ADD list matching Mem0's debug-log expectations.
        results = [
            {"id": f"{user_id}:{i}", "event": "ADD", "memory": str(m.get("content", ""))}
            for i, m in enumerate(messages)
            if (m.get("content") or "").strip()
        ]
        return {"results": results}

    # ── search ──────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 200,
        rerank: bool = False,
        score_debug: bool = False,
    ) -> list[dict]:
        """Return ranked candidate memories — three grains, unified.

        M13.2 multi-grain: the Mem0-harness answerer reads candidates
        flat (no synth wrapping it the way our native bench does), so
        a pure-fact candidate list strands the answerer when the
        question needs conversational context. We surface three grains
        and let a unified cross-encoder rerank pick the best across:

        - **facts**: precise, atomic, dated (precision questions)
        - **section excerpts**: original markdown around the picker's
          chosen line_num (preserves assistant phrasing for
          single-session-assistant; gives multi-hop questions the
          conversational context they need to bridge)
        - **wiki observations**: per-document aggregations (multi-
          session counting, knowledge-update)
        """
        await self._ensure_resources()
        await self._ensure_ingested(user_id)
        bank_id = self._bank_for(user_id)
        document_id = self._doc_ids.get(user_id)
        if document_id is None:
            return []

        floor = max(top_k, self.search_top_k_floor)
        try:
            qvec = (await self._provider.embed([query]))[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed failed for user=%s: %s", user_id, exc)
            return []

        # ── Query-time temporal expansion ──────────────────────────
        # Map relative expressions ("a few weeks ago", "3 days ago") to
        # an absolute date range anchored at the latest session timestamp.
        # When present, route extra recall through the temporal strategy
        # (sections) and search_facts_temporal (facts).
        from astrocyte.pipeline.temporal_expressions import (  # noqa: PLC0415
            expand_temporal_expression,
        )

        anchor = self._reference_date_by_user.get(user_id)
        date_range = expand_temporal_expression(query, anchor) if anchor else None
        recall_mode = "temporal" if date_range is not None else "single-hop"

        # ── (1) Fact-grain candidates ──────────────────────────────
        try:
            fact_hits = await self._store.search_facts_semantic(
                bank_id,
                qvec,
                top_k=40,
                document_id=document_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fact semantic search failed for user=%s: %s", user_id, exc)
            fact_hits = []

        # ── (1b) Temporal fact hits (only if query had a time cue) ─
        temporal_fact_hits: list[Any] = []
        if date_range is not None:
            try:
                temporal_fact_hits = await self._store.search_facts_temporal(
                    bank_id,
                    date_range,
                    top_k=20,
                    document_id=document_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fact temporal search failed for user=%s: %s",
                    user_id,
                    exc,
                )
                temporal_fact_hits = []

        # ── (1c) Episodic fact hits (Fix 4) ────────────────────────
        # When the question carries a location/event cue, query the
        # episodic-marker index — facts tagged at retain time with
        # ``episodic:event`` because their text reads as an encounter
        # ("met X at Y", "attended Z"). These get prepended to the
        # candidate pool so the cross-encoder rerank sees them
        # alongside generic semantic hits.
        episodic_fact_hits: list[Any] = []
        try:
            from astrocyte.pipeline.episodic_extract import (  # noqa: PLC0415
                EPISODIC_MARKER,
                question_has_episodic_cue,
            )

            if question_has_episodic_cue(query):
                try:
                    episodic_fact_hits = await self._store.search_facts_by_entity(
                        bank_id,
                        EPISODIC_MARKER,
                        top_k=20,
                        document_id=document_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "fact episodic search failed for user=%s: %s",
                        user_id,
                        exc,
                    )
                    episodic_fact_hits = []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "episodic_extract import failed for user=%s: %s",
                user_id,
                exc,
            )

        # ── (2) Section-grain candidates via section_recall ────────
        # ``mode="temporal"`` when the query has a relative-time cue —
        # the orchestrator then fires the temporal strategy alongside
        # semantic/keyword/entity. Without a cue, fall back to single-hop.
        # ``question_entities`` is a cheap proper-noun extraction so the
        # entity strategy (and the alias-enriched entity index from
        # ``section_entity_extraction``) can actually fire.
        question_entities = _extract_proper_noun_entities(query)
        from astrocyte.pipeline.section_recall import section_recall  # noqa: PLC0415

        try:
            recall = await section_recall(
                store=self._store,
                bank_id=bank_id,
                question=query,
                mode=recall_mode,
                embedding_provider=self._provider,
                question_entities=question_entities,
                date_range=date_range,
                wiki_enabled=True,
                wiki_document_id=document_id,
                wiki_min_score=0.25,
                wiki_top_k=4,
                per_strategy_top_k=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("section_recall failed for user=%s: %s", user_id, exc)
            recall = None

        # ── (3) Build unified candidate list with grain metadata ───
        md_text = self._md_text_by_user.get(user_id, "")
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for fh in fact_hits:
            cid = f"fact:{fh.fact_id}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            candidates.append(
                {
                    "id": cid,
                    "text": fh.text,
                    "grain": "fact",
                    "fact": fh,
                }
            )

        for fh in temporal_fact_hits:
            cid = f"fact:{fh.fact_id}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            candidates.append(
                {
                    "id": cid,
                    "text": fh.text,
                    "grain": "fact",
                    "fact": fh,
                }
            )

        for fh in episodic_fact_hits:
            cid = f"fact:{fh.fact_id}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            candidates.append(
                {
                    "id": cid,
                    "text": fh.text,
                    "grain": "fact",
                    "fact": fh,
                }
            )

        # ── Fix 3 (conv-run-4): entity spreading activation ───────
        # Before turning ``recall.fused`` into candidates, expand by
        # one hop through entity co-occurrence. This bridges the
        # Denver/Disneyland conflation case (initial top-K surfaces
        # Disneyland by keyword; "Denver" entity overlap brings in
        # the correct Red Rocks session). The expansion runs over
        # ``astrocyte_pi_section_entities`` (dense per retain) rather
        # than the sparse LLM-extracted link table, so it actually
        # fires for conversation-ingest documents.
        spread_hits: list[tuple[str, int, float]] = []
        if recall is not None and recall.fused:
            from astrocyte.pipeline.spreading_activation import (  # noqa: PLC0415
                expand_via_shared_entities,
            )

            top_seeds = [(h.document_id, h.line_num) for h in recall.fused[:10]]
            try:
                spread_hits = await expand_via_shared_entities(
                    store=self._store,
                    bank_id=bank_id,
                    seeds=top_seeds,
                    top_k=10,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "spreading_activation failed for user=%s: %s",
                    user_id,
                    exc,
                )
                spread_hits = []

        if recall is not None and recall.fused:
            # Ceiling raised 8 → 20 so the unified cross-encoder rerank
            # below sees enough section candidates to surface the
            # correct chunk even when section_recall's fused ranking
            # ranks it past the previous cutoff. The rerank still
            # truncates to ``floor`` after scoring, so this only
            # widens the pre-rerank pool, not the final output size.
            for sh in recall.fused[:20]:
                excerpt = _truncate_chars(
                    _slice_section_around_line(md_text, sh.line_num),
                    max_chars=2400,  # ~600 tokens
                )
                if not excerpt:
                    continue
                cid = f"section:{sh.document_id}:{sh.line_num}"
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                candidates.append(
                    {
                        "id": cid,
                        "text": excerpt,
                        "grain": "section",
                        "section": sh,
                    }
                )

        # Append spread-activation hits as additional section candidates.
        # The cross-encoder rerank below scores them on equal footing
        # with the fused hits, so promotion is gated on actual question
        # relevance rather than the spread step blindly inflating recall.
        if spread_hits:
            from astrocyte.types import PageIndexSection  # noqa: PLC0415

            for doc_id, line_num, _score in spread_hits:
                cid = f"section:{doc_id}:{line_num}"
                if cid in seen_ids:
                    continue
                excerpt = _truncate_chars(
                    _slice_section_around_line(md_text, line_num),
                    max_chars=2400,
                )
                if not excerpt:
                    continue
                seen_ids.add(cid)
                # Lightweight shim so downstream score_debug code that
                # reads ``c["section"].line_num`` keeps working without
                # constructing a full DB-shaped section dataclass.
                shim_section = PageIndexSection(
                    document_id=doc_id,
                    line_num=line_num,
                    node_id="",
                    title="",
                    summary=None,
                )
                candidates.append(
                    {
                        "id": cid,
                        "text": excerpt,
                        "grain": "section",
                        "section": shim_section,
                    }
                )

        if recall is not None and getattr(recall, "wiki_hits", None):
            for w in recall.wiki_hits[:4]:
                wiki_text = f"[Observation: {w.title}] {w.content}".strip()
                cid = f"wiki:{w.page_id}"
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                candidates.append(
                    {
                        "id": cid,
                        "text": wiki_text,
                        "grain": "wiki",
                        "wiki": w,
                    }
                )

        if not candidates:
            return []

        # ── (4) Unified cross-encoder rerank over the union ────────
        try:
            from astrocyte.pipeline.cross_encoder_rerank import (  # noqa: PLC0415
                cross_encoder_rerank,
            )
            from astrocyte.pipeline.reranking import ScoredItem  # noqa: PLC0415

            items = [ScoredItem(id=c["id"], text=c["text"], score=0.0) for c in candidates]
            reranked = cross_encoder_rerank(items, query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("unified rerank failed for user=%s: %s", user_id, exc)
            reranked = [ScoredItem(id=c["id"], text=c["text"], score=0.0) for c in candidates]

        by_id = {c["id"]: c for c in candidates}
        out: list[dict[str, Any]] = []
        for item in reranked[:floor]:
            c = by_id.get(item.id)
            if c is None:
                continue
            entry: dict[str, Any] = {
                "memory": c["text"],
                "score": float(item.score),
                "id": item.id,
            }
            # Preserve grain-specific metadata for downstream debug.
            entry["metadata"] = {"grain": c["grain"]}
            if c["grain"] == "fact":
                fh = c["fact"]
                if fh.occurred_start:
                    entry["created_at"] = fh.occurred_start.isoformat()
                if score_debug:
                    entry["score_debug"] = {
                        "combined_score": float(item.score),
                        "grain": "fact",
                        "fact_type": fh.fact_type,
                        "entities": fh.entities,
                    }
            elif c["grain"] == "section" and score_debug:
                entry["score_debug"] = {
                    "combined_score": float(item.score),
                    "grain": "section",
                    "line_num": c["section"].line_num,
                }
            elif c["grain"] == "wiki" and score_debug:
                entry["score_debug"] = {
                    "combined_score": float(item.score),
                    "grain": "wiki",
                    "page_id": c["wiki"].page_id,
                }
            out.append(entry)

        # B-1 / B-1.1 / B-1.2 anchor injection removed (M14.7 revert,
        # 2026-05-13). The Hindsight comparison showed that auto-extracted
        # preferences injected into the answerer's context cause
        # cross-category regression (single-session-user 5/5 → 1/5 on LME
        # as profile bloated). Hindsight handles personalization via
        # USER-CURATED ``directive`` mental_models instead — explicit
        # hard-rule injection in the system prompt, not auto-extraction.
        # The shape detector (``scripts/mem0_harness/_shape.py``) and
        # M14.6 preference_compile pipeline are preserved as building
        # blocks for a future user-curation path; they're just not wired
        # into the bench retrieval/profile surface here.

        # ── Fix 1 (conv-run-4): raw conversation excerpts ──────────
        # Single-session-preference failures: implicit preferences
        # ("no screens after 9:30pm", said in passing) are visible in
        # the raw chunk but never extracted as facts, so the answerer
        # only sees summaries and atomic facts — both miss the cue.
        # Inject the top-3 raw chunks as separately-labeled context so
        # the answerer can read the implicit preference verbatim. They
        # ride above the existing reranked output by carrying a high
        # synthetic score, and are prefixed "[Conversation excerpt — ...]"
        # so the answerer prompt's "- {memory}" bullet list still
        # delimits them clearly from "Facts:" entries.
        excerpts = self._build_raw_conversation_excerpts(
            recall=recall,
            query=query,
            md_text=md_text,
            top_k=3,
        )
        if excerpts:
            out = excerpts + out
        return out

    def _build_raw_conversation_excerpts(
        self,
        *,
        recall: Any,
        query: str,
        md_text: str,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """Fix 1: top-K raw chunks prepended to the search output.

        Picks the top-K sections from ``recall.fused`` (which already
        carries the fused RRF ranking across semantic/keyword/entity/
        temporal strategies), slices their full chunk body from
        ``md_text``, prefixes each with a ``[Conversation excerpt — ...]``
        label, and emits them as ``{memory, score, id}`` entries. The
        synthetic scores are large enough that ``format_search_results``
        (which sorts by score desc) keeps them above the cross-encoder
        reranked entries.

        Why pull from ``recall.fused`` rather than re-rerank: section_recall
        already balances semantic + keyword + entity signal, and we
        want the LITERAL chunk text (not a re-summary) reaching the
        answerer. Limiting to ``top_k=3`` matches the user's spec and
        keeps the answerer prompt manageable.
        """
        if recall is None or not getattr(recall, "fused", None):
            return []
        if not md_text:
            return []
        out: list[dict[str, Any]] = []
        # Synthetic score high enough to outrank the cross-encoder
        # scores in ``out`` (which are typically in [-10, 10] for
        # cross-encoders); 1e6 puts excerpts unambiguously at the top
        # while preserving relative ordering among themselves.
        base_score = 1_000_000.0
        seen_lines: set[int] = set()
        for sh in recall.fused:
            if len(out) >= top_k:
                break
            line_num = sh.line_num
            if line_num in seen_lines:
                continue
            body = _truncate_chars(
                _slice_section_around_line(md_text, line_num),
                max_chars=2400,
            )
            if not body:
                continue
            seen_lines.add(line_num)
            # Pull the heading line as a session-date hint so the
            # excerpt is self-locating to the answerer (which has no
            # access to the underlying line_num metadata).
            heading = ""
            try:
                lines = md_text.split("\n")
                if 1 <= line_num <= len(lines):
                    heading = lines[line_num - 1].lstrip("# ").strip()
            except Exception:  # noqa: BLE001
                heading = ""
            label = f"[Conversation excerpt — {heading}]" if heading else "[Conversation excerpt]"
            out.append(
                {
                    "memory": f"{label}\n{body}",
                    "score": base_score - float(len(out)),
                    "id": f"excerpt:{sh.document_id}:{line_num}",
                    "metadata": {"grain": "raw_chunk"},
                }
            )
        return out

    # ── delete_user ─────────────────────────────────────────────────────

    async def get_user_profile(self, user_id: str) -> dict | None:
        """Serialize Astrocyte's per-document mental models as the
        Mem0-runner-shaped user-profile dict.

        Mem0's runner formats the returned dict as ``key: value`` lines
        under a ``## User Profile`` heading (``prompts._format_user_profile``).
        Keys are snake_case → Title Case for display; values are coerced
        to string. Empty values are omitted.

        We map each :class:`~astrocyte.types.MentalModel` to one
        ``key: content`` entry, with the model's ``title`` used as the
        key (slug-style snake_case so Mem0's formatter renders it
        cleanly). Returns ``None`` when no models exist so the runner
        skips the profile injection.
        """
        await self._ensure_resources()
        # The profile needs the bank ingested. If we haven't yet, the
        # caller is asking for profile pre-ingest — trigger it so the
        # models exist to read.
        if user_id not in self._ingested:
            try:
                await self._ensure_ingested(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ingest-for-profile failed user=%s: %s", user_id, exc)
                return None

        document_id = self._doc_ids.get(user_id)
        if document_id is None or self._mental_model_store is None:
            return None
        try:
            models = await self._mental_model_store.list(
                self._bank_for(user_id),
                scope=f"document:{document_id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("mental_model.list failed user=%s: %s", user_id, exc)
            return None
        if not models:
            return None

        # M14.7 revert (2026-05-13) preserved: ``kind='preference'`` rows
        # are NOT surfaced in the profile because M14.6 produced 12+ such
        # rows per doc and bloated the answerer prompt, regressing LME
        # single-session-user 5/5 → 1/5.
        #
        # M17 follow-up: surface ``kind='directive'`` rows. These are the
        # Hindsight-style sparse hard-rule equivalents — capped at 5 per
        # doc via ``directive_compile``, phrased imperatively. The cap
        # plus the imperative phrasing means the answerer gets a small
        # set of stable rules instead of a verbose preference dump, which
        # was the failure shape M14.7 reverted.
        #
        # Surface kind='general' (M11.2 profile facts) AND kind='directive'
        # (M17 hard rules). Skip kind='preference' to keep the M14.7
        # cross-category protection.
        profile: dict[str, str] = {}
        seen_keys: set[str] = set()
        for m in models:
            kind = getattr(m, "kind", "general")
            if kind not in {"general", "directive"}:
                continue
            title = (m.title or "").strip()
            content = (m.content or "").strip()
            if not title or not content:
                continue
            key = _slug_snake_case(title)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            profile[key] = content

        return profile or None

    async def delete_user(self, user_id: str) -> bool:
        """Clear the user's buffer and ingest state. Postgres rows for
        prior runs persist (cheaper than a cascading DELETE here); a
        fresh ``bank_id`` per run is the recommended isolation pattern.
        """
        self._buffers.pop(user_id, None)
        self._ingested.discard(user_id)
        self._doc_ids.pop(user_id, None)
        return True

    # ── lazy ingest plumbing ────────────────────────────────────────────

    def _bank_for(self, user_id: str) -> str:
        return f"{self.bank_prefix}:{user_id}"

    async def _ensure_ingested(self, user_id: str) -> None:
        """First-search-for-a-user trigger: build the Astrocyte bank
        from the buffered ``add()`` calls. Subsequent searches no-op."""
        if user_id in self._ingested:
            return
        lock = self._ingest_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if user_id in self._ingested:
                return
            await self._do_ingest(user_id)
            self._ingested.add(user_id)

    async def _do_ingest(self, user_id: str) -> None:
        from scripts.bench_pageindex_locomo import (  # noqa: PLC0415
            _flatten_tree_to_sections,
            _populate_section_index,
        )

        bank_id = self._bank_for(user_id)
        buffer = self._buffers.get(user_id, [])
        if not buffer:
            logger.warning("AstrocyteClient.ingest: empty buffer for user=%s", user_id)
            return

        # Mem0's LoCoMo runner uses CHUNK_SIZE=1 → ~22 add() calls per
        # session, all sharing the same session-epoch timestamp. Group
        # buffered calls by timestamp to reconstruct REAL sessions; if
        # we treat each add() as its own section, PageIndex's tree
        # explodes to ~420 single-turn sections and the structure that
        # makes section-grain retrieval work is lost.
        sessions: dict[int | None, list[dict[str, str]]] = {}
        # Preserve the order timestamps were first seen — sort by that
        # so chronological ordering matches Mem0's get_sorted_sessions.
        ts_order: list[int | None] = []
        for ts, msgs in buffer:
            if ts not in sessions:
                ts_order.append(ts)
                sessions[ts] = []
            sessions[ts].extend(msgs)

        # Stable chronological order. ``None`` timestamps go last
        # (no_date sessions slot to the end of the conversation).
        ts_order.sort(key=lambda t: (t is None, t or 0))

        latest_ts: int | None = next(
            (t for t in reversed(ts_order) if t is not None),
            None,
        )

        md_parts = [f"# Conversation {user_id}", ""]
        for idx, ts in enumerate(ts_order, start=1):
            md_parts.append(
                _format_messages_as_markdown(
                    sessions[ts],
                    session_idx=idx,
                    timestamp=ts,
                )
            )
        md_text = "\n".join(md_parts).strip()
        # Cache for M13.2 section-excerpt rendering at search time.
        self._md_text_by_user[user_id] = md_text
        reference_date = (
            datetime.fromtimestamp(latest_ts, tz=timezone.utc)
            if latest_ts is not None
            else datetime.now(tz=timezone.utc)
        )
        # Cache the anchor for query-time temporal expansion in search().
        self._reference_date_by_user[user_id] = reference_date

        # 1. Save document + build PageIndex tree.
        start = time.monotonic()
        doc = PageIndexDocument(
            id="",
            bank_id=bank_id,
            source_id=user_id,
            md_text=md_text,
            reference_date=reference_date,
            built_at=datetime.now(tz=timezone.utc),
        )
        document_id = await self._store.save_document(doc)
        self._doc_ids[user_id] = document_id

        # M17 Phase 3: ingest path selection
        #   - "conversation" path: Conversation Engine → ConversationIngestor
        #     → retain SPI adapter → existing extraction (the architecturally
        #     correct path for conversation-shaped data; this is H1b).
        #   - "document" path: Document Engine tree builder → flatten → existing
        #     extraction (parity-check vs external PageIndex; treats
        #     conversation as pseudo-markdown document).
        try:
            if _USE_CONVERSATION_ENGINE:
                # === Conversation Engine path ===
                # Build Conversation from sessions (with real per-turn timestamps).
                conv = Conversation.new(
                    source_uri=f"bench://{user_id}",
                    title=f"Conversation {user_id}",
                )
                for sess_idx, ts in enumerate(ts_order, start=1):
                    turn_ts = datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None
                    # session_id tells chunk_conversation where session
                    # boundaries are. Without it, the chunker would
                    # greedy-pack across sessions, smearing facts from
                    # different days into a single section and breaking
                    # session-anchored recall (LME single-session-*).
                    session_id = f"ts:{ts}" if ts is not None else f"no-ts:{sess_idx}"
                    for msg in sessions[ts]:
                        conv.add_turn(
                            role=msg.get("role", "user"),
                            content=str(msg.get("content", "")),
                            timestamp=turn_ts,
                            metadata={"session_id": session_id},
                        )

                # Chunk + emit via ConversationIngestor → retain SPI adapter
                adapter = _BenchRetainAdapter()
                ingestor = ConversationIngestor(retain=adapter, max_chars_per_chunk=12_000)
                ingest_result = await ingestor.ingest(conv, bank_id=bank_id)
                logger.info(
                    "ConversationIngestor: user=%s emitted=%d turns=%d chunks=%d",
                    user_id,
                    ingest_result.segments_emitted,
                    conv.turn_count(),
                    ingest_result.metadata.get("chunk_count", 0),
                )

                # Materialize buffered chunks into sections + md_text for the
                # SAME downstream extraction the Document Engine path uses.
                # ``provider`` is passed so AdaptiveSummarizer can produce
                # per-section summaries (otherwise summary_embedding stays
                # NULL and the semantic strategy goes blind on CE-path docs).
                sections, md_text_conv = await adapter.materialize(
                    document_id,
                    provider=self._provider,
                    summarizer_model=self.tree_build_model,
                )
                # Override the per-user md_text cache so search-time slicing
                # uses the chunk-derived md_text.
                self._md_text_by_user[user_id] = md_text_conv
                md_text = md_text_conv  # for the downstream extraction call

            elif _USE_FRAMEWORK_TREE_BUILDER:
                # === Document Engine path (framework-native, M17 default) ===
                doc_tree = build_markdown_tree(md_text, document_id)
                summarizer = AdaptiveSummarizer(
                    _make_summarizer_llm_call(self._provider, self.tree_build_model),
                    threshold_tokens=200,
                )
                await summarizer.summarize_tree(doc_tree)
                raw_tree = _document_tree_to_pageindex_dict(doc_tree)
                sections = _flatten_tree_to_sections(raw_tree, document_id)
            else:  # pragma: no cover — legacy parity path
                md_tmp = Path(f"/tmp/astrocyte-mem0-harness-{user_id.replace('/', '_')}.md")
                md_tmp.write_text(md_text)
                raw_tree = await md_to_tree(  # type: ignore[name-defined]
                    str(md_tmp),
                    if_add_node_summary="yes",
                    summary_token_threshold=200,
                    model=self.tree_build_model,
                    if_add_node_text="no",
                    if_add_node_id="yes",
                )
                sections = _flatten_tree_to_sections(raw_tree, document_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ingest tree/conversation build failed for user=%s: %s", user_id, exc)
            return

        try:
            await self._store.save_sections(document_id, sections)
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_sections failed for user=%s: %s", user_id, exc)
            return

        # 2. Populate entities + embeddings + facts + wikis + mental models.
        try:
            await _populate_section_index(
                provider=self._provider,
                store=self._store,
                document_id=document_id,
                sections=sections,
                md_text=md_text,
                entity_model=self.extraction_model,
                embedding_model=self.embedding_model,
                bank_id=bank_id,
                mental_model_store=self._mental_model_store,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_populate_section_index failed for user=%s: %s", user_id, exc)
            return

        elapsed = time.monotonic() - start
        logger.info(
            "AstrocyteClient ingested user=%s (%d buffer entries, doc=%s, %.1fs)",
            user_id,
            len(buffer),
            document_id,
            elapsed,
        )


# Helper that mirrors mem0_client.format_search_results for the runner.
def format_search_results(search_results: list[dict]) -> tuple[list[dict], dict | None]:
    """Pass-through; AstrocyteClient already returns normalised + sorted."""
    if not search_results:
        return [], None
    sorted_results = sorted(search_results, key=lambda x: x.get("score", 0), reverse=True)
    return sorted_results, None
