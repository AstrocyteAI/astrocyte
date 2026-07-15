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

# Ensure the astrocyte package is importable. PageIndex is installed as
# the ``pageindex`` package (AstrocyteAI fork) via ``make bench-runner-deps``
# — no sys.path shim, no hardcoded path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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

            # M31 Fix 2 — propagate session_id from turn metadata onto the
            # emitted section. ``_do_ingest`` already tags every turn with
            # ``metadata['session_id']`` (one of ``ts:<epoch>`` or
            # ``no-ts:<idx>``). When ConversationIngestor groups turns into
            # a chunk, it preserves shared metadata fields — so reading
            # ``metadata.get('session_id')`` here recovers the session
            # boundary for the chunk. Real (non-bench) callers can pass
            # any opaque string they use to identify sessions in their
            # application.
            chunk_session_id = metadata.get("session_id")

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
                    session_id=chunk_session_id,
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
        # M37b — per-user MM embedding cache. Computed lazily on first
        # M37 retrieval per user, reused for all subsequent queries.
        # Avoids a migration (no embedding column on MM table); cost
        # is one batched embed call per user (~1-2s for ~1000 MMs)
        # amortised across all of that user's questions.
        # Shape: {user_id: {model_id: (mm, embedding_vec)}}
        self._mm_embedding_cache: dict[str, dict[str, tuple[Any, list[float]]]] = {}

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
        session_id: str | None = None,
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

        M31 Fix 2 — ``session_id`` opt-in filter: when the calling
        application knows which session a query targets (real chat
        apps with explicit session boundaries; LME bench questions
        with ``question_session_id`` metadata), filter fact retrieval
        to that session. ``None`` (default) preserves v0.14.0 behaviour.
        Threads through ``fact_recall(session_filter=...)`` which
        applies it to semantic/episodic/temporal branches but NOT
        link-expansion (the latter is cross-session by design — see
        M31 cycle doc §3 Fix 2 rationale).
        """
        await self._ensure_resources()
        await self._ensure_ingested(user_id)
        bank_id = self._bank_for(user_id)
        document_id = self._doc_ids.get(user_id)
        if document_id is None:
            return []

        # M31 Fix 2 — when the bench harness's per-question wrapper has
        # set the session_id contextvar (LME questions with
        # ``question_session_id`` metadata), fall back to that if the
        # caller didn't pass session_id explicitly. The upstream
        # ``mem0.search(...)`` SPI doesn't take a session_id parameter,
        # so contextvar plumbing is how the wrapper hands the value
        # through without widening the SPI.
        if session_id is None:
            try:
                from scripts.mem0_harness._hindsight_answerer import (  # noqa: PLC0415
                    current_session_id,
                )

                session_id = current_session_id()
            except ImportError:
                # _hindsight_answerer is optional (bench-time patch);
                # absent in production. Leave session_id None and let
                # downstream callers fall back to bank-wide search.
                pass

        floor = max(top_k, self.search_top_k_floor)

        # M38 — LLM query rewriting (cheap, opt-in). Reformulates the
        # natural question into a search-optimized form BEFORE retrieval.
        # The ORIGINAL ``query`` still reaches the answerer prompt so
        # reasoning context is preserved; only the retrieval embedding +
        # entity extraction use ``retrieval_query``.
        #
        # Gated by ASTROCYTE_M38_QUERY_REWRITE (default OFF for ablation
        # parity with v015p). When ON, one extra gpt-4o-mini call per
        # question (~300ms, ~$0.0001).
        retrieval_query = query
        if _os.environ.get("ASTROCYTE_M38_QUERY_REWRITE", "").lower() in ("1", "true", "yes"):
            try:
                from astrocyte.pipeline.query_rewrite import (  # noqa: PLC0415
                    rewrite_query,
                )

                rewritten = await rewrite_query(
                    query,
                    llm_provider=self._provider,
                    model=getattr(self, "tree_build_model", None),
                )
                if rewritten:
                    retrieval_query = rewritten
                    logger.info(
                        "M38 query_rewrite: %r → %r",
                        query[:60],
                        rewritten[:60],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("M38 query_rewrite failed user=%s: %s", user_id, exc)
                # retrieval_query stays as original

        try:
            qvec = (await self._provider.embed([retrieval_query]))[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed failed for user=%s: %s", user_id, exc)
            return []

        # ── Query-time temporal expansion (M18a-1 core integration) ──
        # Pass A (extended regex) + Pass B (dateparser, Hindsight parity).
        # Invoked via `query_analyzer.analyze_query(allow_temporal_expansion=...)`,
        # date range fed to `fact_recall` which RRF-fuses semantic +
        # episodic + temporal as parallel siblings.
        #
        # **Default ON post-M18b ship** (2026-05-17): B1-dp+RRF cleared
        # the M17+1σ LoCoMo ship gate by 1.75pp (2-run mean 83.75% vs
        # baseline 80.5%). Bench env override:
        #   ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION=0  → force off (ablation)
        #   ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION=1  → force on (default)
        #   unset                                       → on (post-ship)
        from astrocyte.pipeline.query_analyzer import analyze_query  # noqa: PLC0415

        _env = _os.environ.get("ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION", "").lower()
        if _env in ("0", "false", "no"):
            _allow_expansion = False
        else:
            _allow_expansion = True
        anchor = self._reference_date_by_user.get(user_id)
        analysis = await analyze_query(
            query,
            reference_date=anchor,
            llm_provider=None,  # bench harness path stays regex-only
            allow_llm_fallback=False,
            allow_temporal_expansion=_allow_expansion,
        )
        date_range = (
            (analysis.temporal_constraint.start_date, analysis.temporal_constraint.end_date)
            if analysis.temporal_constraint and analysis.temporal_constraint.is_bounded()
            else None
        )
        recall_mode = "temporal" if date_range is not None else "single-hop"

        # ── (1) Fact-grain candidates ──────────────────────────────
        # Unified semantic + (optional) episodic via the core
        # ``fact_recall`` entry point. The bench builds an ad-hoc
        # AstrocyteConfig with ``episodic_extract.enabled`` driven by
        # ASTROCYTE_M18_ENABLE_EPISODIC_EXTRACTION=1 (default OFF,
        # parity with M17 baseline). When on, queries matching
        # ``question_has_episodic_cue`` additionally search by
        # EPISODIC_MARKER and merge the hits (dedupe by fact_id).
        # The retain-side tag step is gated by the same env var via
        # ``run_document_postprocess`` in bench_pageindex_locomo.
        from astrocyte.config import (  # noqa: PLC0415
            AstrocyteConfig,
            EpisodicExtractConfig,
        )
        from astrocyte.pipeline.fact_recall import fact_recall  # noqa: PLC0415

        _enable_episodic = _os.environ.get(
            "ASTROCYTE_M18_ENABLE_EPISODIC_EXTRACTION", "",
        ).lower() in ("1", "true", "yes")
        _fr_cfg = AstrocyteConfig()
        _fr_cfg.episodic_extract = EpisodicExtractConfig(enabled=_enable_episodic)

        # M18a-1 (RRF refactor): fact_recall now runs semantic + episodic +
        # temporal as PARALLEL SIBLINGS internally and RRF-fuses them.
        # When date_range is None, the temporal branch is simply skipped —
        # same external behavior, just no separate post-fact_recall append.
        # See docs/_design/m18-quick-wins.md §3.3 for the rationale
        # (Hindsight-parity architecture; rerank pool is now source-blind
        # but the candidates entering rerank have already been RRF-weighted
        # so single-strategy junk gets damped).
        # M27 — extract query entities BEFORE fact_recall so we can
        # pass them in for the new cross-session link-expansion branch.
        # Cheap regex pass; the same entities are also re-used by
        # section_recall below. M38 — use ``retrieval_query`` (the
        # rewritten form when M38 is on) so entity extraction works
        # off the search-optimized text. Falls back to original when
        # M38 is off.
        question_entities = _extract_proper_noun_entities(retrieval_query)

        # ── (2) Section-grain candidates via section_recall ────────
        # ``mode="temporal"`` when the query has a relative-time cue —
        # the orchestrator then fires the temporal strategy alongside
        # semantic/keyword/entity. Without a cue, fall back to single-hop.
        # ``question_entities`` was extracted above for fact_recall's
        # link-expansion branch (M27); reused here for section_recall's
        # entity strategy.
        from astrocyte.pipeline.section_recall import section_recall  # noqa: PLC0415

        # M18a-3 — entity-co-occurrence spread is now a section_recall
        # internal post-fusion step, gated by `enable_spreading_activation`.
        # Bench-time env override: ASTROCYTE_M18_ENABLE_SPREADING_ACTIVATION=1.
        # Default OFF (parity with M17 baseline). The spread hits land
        # inside `recall.fused`, so the downstream candidate-building loop
        # needs no separate spread-merge code.
        _enable_spreading = _os.environ.get(
            "ASTROCYTE_M18_ENABLE_SPREADING_ACTIVATION", "",
        ).lower() in ("1", "true", "yes")

        # M34-7 (v015l) — per-fact-type segmentation ONLY, no intent
        # classification. v015k's forensic showed the regex intent
        # classifier mis-routed LME questions (7/15 TR queries
        # classified as FACTUAL → temporal weight 0.3 → killed TR's
        # gain; SSP queries fell through to UNKNOWN with no
        # PREFERENCE intent; SSA queries 15/15 UNKNOWN). This matches
        # Hindsight's architecture exactly: they have NO intent
        # classifier — only temporal-presence detection (already in
        # query_analyzer) plus per-fact-type segmentation. The
        # segmentation alone delivered LoCoMo wins in v015k
        # (multi-hop +5.1pp, open-domain +3.2pp); LME regression was
        # the intent-bias damage. Removing intent should keep the
        # LoCoMo wins while letting LME categories use the
        # equal-weight RRF that worked in v015f.
        # Per fact_type-segmentation: the LME corpus is dominated by
        # experience + preference + world facts. Plan / opinion are
        # rare and fold into experience semantically at retrieval time.
        _BENCH_FACT_TYPES = ["experience", "preference", "world"]

        # M30-L1 — fact_recall + section_recall are independent retrieval
        # pipelines; running them sequentially wastes ~1-3s per question.
        # Fire as parallel sibling tasks via asyncio.gather. fact_recall
        # internally already fans semantic+episodic+temporal+link_expansion
        # in parallel; this lifts the same pattern one level up so the
        # section pipeline runs concurrently with the fact pipeline rather
        # than waiting for it. Per-branch exceptions are isolated so a
        # section_recall failure can't take fact_recall down.
        fact_coro = fact_recall(
            store=self._store,
            bank_id=bank_id,
            document_id=document_id,
            # M38 — pass the rewritten query (when M38 is on) so any
            # text-based fact retrieval (BM25, episodic cue match) sees
            # the search-optimized form. qvec was already embedded
            # from retrieval_query above.
            query=retrieval_query,
            query_embedding=qvec,
            config=_fr_cfg,
            temporal_range=date_range,
            query_entities=question_entities,  # M27 cross-session graph
            top_k_semantic=40,
            top_k_episodic=20,
            # M34-6 — restore top_k_temporal=20 now that per-fact-type
            # segmentation prevents the v015i flood. The M33-3 Path A
            # cap=5 was a workaround for the single-pool dilution; with
            # M34's segmentation the temporal channel can't drown out
            # other fact_types' pools (each type has its own RRF).
            top_k_temporal=20,
            top_k_link_expansion=20,
            # M34-7 (v015l) — segmentation only, NO intent.
            # When intent is None, fact_recall uses equal-weight RRF
            # and the BM25 channel stays off (M31c-era decision).
            fact_types=_BENCH_FACT_TYPES,
            # M35-2 — token budget cap on the merged output. Hindsight
            # uses max_tokens=8192 as their bench default. v015l's
            # winning top_20 cap roughly translates to ~1500-3000
            # tokens; we set a more generous 4096 budget here so
            # focused-but-not-starved context reaches the answerer.
            # Per-cutoff sweep will tell us the real curve in v015m.
            max_tokens=4096,
            # M31 Fix 2 — caller-supplied session boundary. ``None`` (the
            # default) preserves v0.14.0 behaviour; non-None scopes
            # semantic/episodic/temporal to facts whose anchoring section
            # has matching session_id. Link-expansion stays cross-session
            # (its whole purpose).
            session_filter=session_id,
        )
        section_coro = section_recall(
            store=self._store,
            bank_id=bank_id,
            # M38 — section_recall consumes the question text for its
            # keyword + embedding strategies. Use retrieval_query for
            # the same reason as fact_recall.
            question=retrieval_query,
            mode=recall_mode,
            embedding_provider=self._provider,
            question_entities=question_entities,
            date_range=date_range,
            wiki_enabled=True,
            wiki_document_id=document_id,
            wiki_min_score=0.25,
            wiki_top_k=4,
            per_strategy_top_k=20,
            enable_spreading_activation=_enable_spreading,
            session_filter=session_id,  # M31 Fix 2
        )
        fact_result, section_result = await asyncio.gather(
            fact_coro, section_coro, return_exceptions=True,
        )
        if isinstance(fact_result, BaseException):
            logger.warning("fact_recall failed for user=%s: %s", user_id, fact_result)
            fact_hits = []
        else:
            fact_hits = fact_result
        if isinstance(section_result, BaseException):
            logger.warning("section_recall failed for user=%s: %s", user_id, section_result)
            recall = None
        else:
            recall = section_result

        # ── (3) Build unified candidate list with grain metadata ───
        md_text = self._md_text_by_user.get(user_id, "")
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # M24 architectural fix — pair each fact with its source-chunk
        # text inline (Hindsight parity for ``_format_context_structured``).
        # Without this pairing the answerer can't cross-reference fact
        # → source chunk and M22's ``read the chunks carefully`` rule
        # lands on a flat unpaired list, regressing LME SSP by −2q.
        # See ``MemoryFact.chunk_id`` for the stable identity contract.
        for fh in fact_hits:
            cid = f"fact:{fh.fact_id}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            # Look up the source chunk via the fact's (document_id, line_num)
            # anchor when available. Top-level facts (no anchor) leave
            # ``source_chunk`` as ``None`` and render without the inline
            # Source: block — same behaviour as Hindsight's facts that
            # have ``chunk_id IS NULL``.
            source_chunk: str | None = None
            if md_text and fh.line_num is not None:
                excerpt = _slice_section_around_line(md_text, fh.line_num)
                if excerpt:
                    source_chunk = _truncate_chars(excerpt, max_chars=1200)
            candidates.append(
                {
                    "id": cid,
                    "text": fh.text,
                    "grain": "fact",
                    "fact": fh,
                    "chunk_id": fh.chunk_id,  # Hindsight parity
                    "source_chunk": source_chunk,
                    # M28 Workstream B — surface M27 per-fact confidence
                    # and mentioned-at timestamp so the answerer can hedge
                    # on low-confidence claims and disambiguate "occurred"
                    # vs "mentioned" date arithmetic. None when absent
                    # (legacy rows / facts without a session date).
                    "confidence_score": fh.confidence_score,
                    "mentioned_at": fh.mentioned_at,
                    # M31 Fix 4 — surface resolved absolute event date so
                    # the answerer reads ``Event: 2024-05-07`` instead of
                    # doing "last Tuesday" → "2024-05-07" arithmetic at
                    # query time. ``None`` for legacy / no-anchor facts.
                    "event_date": fh.event_date,
                }
            )

        # NOTE: the separate `temporal_fact_hits` candidate-build loop was
        # removed in the M18a-1-rrf cycle — temporal candidates are now
        # folded into `fact_hits` above by ``fact_recall`` (RRF fusion of
        # semantic + episodic + temporal as parallel siblings, Hindsight-
        # parity). The fact-grain `for fh in fact_hits` loop above already
        # picks up any temporal hits.

        # M18a-3: spreading_activation is now done inside section_recall
        # (gated by the flag passed above). `recall.fused` already contains
        # any spread hits when the flag is on. The separate spread-merge
        # block below is preserved as a no-op for compatibility (spread_hits
        # always empty after M18a-3).
        spread_hits: list[tuple[str, int, float]] = []

        if recall is not None and recall.fused:
            # M35-aside — token-budget the section excerpts that flow
            # into the unified cross-encoder rerank. Pre-M35 we hard-
            # capped at recall.fused[:20] + max_chars=2400 (~600 tokens)
            # per excerpt = ~12000 tokens of section input to the
            # rerank. That mixes "rank cap" (20 items) with "per-item
            # text cap" (2400 chars).
            #
            # Post-M35: still cap each excerpt at ~600 tokens (slicing
            # cost is per-section, not corpus-wide), but cap the TOTAL
            # section budget at 6000 tokens — matches Hindsight's
            # section context bound and gives the rerank a coherent
            # input size. Per-fact-type segmentation in fact_recall
            # already gives the rerank fact-grain diversity; sections
            # complement at the chunk level.
            _SECTION_TOTAL_TOKEN_BUDGET = 6000
            _section_tokens_used = 0
            from astrocyte.pipeline.token_budget import count_tokens  # noqa: PLC0415

            for sh in recall.fused[:30]:  # widened pre-budget pool to 30
                excerpt = _truncate_chars(
                    _slice_section_around_line(md_text, sh.line_num),
                    max_chars=2400,  # ~600 tokens per excerpt
                )
                if not excerpt:
                    continue
                cid = f"section:{sh.document_id}:{sh.line_num}"
                if cid in seen_ids:
                    continue
                excerpt_tokens = count_tokens(excerpt)
                # Always accept the first section (avoid empty section
                # pool on oversize top hit); after that, stop when the
                # cumulative section-token total would exceed the budget.
                if candidates and _section_tokens_used + excerpt_tokens > _SECTION_TOTAL_TOKEN_BUDGET:
                    break
                seen_ids.add(cid)
                _section_tokens_used += excerpt_tokens
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

        # ── (3.5) M37 — Mental-model candidates ──────────────────────
        # Surface top-K query-relevant mental models alongside fact /
        # section / wiki candidates. The cross-encoder rerank below
        # decides whether they survive into the final top-N.
        #
        # 2003+ MMs typically exist per LME user (1015 preference +
        # 988 general). Pre-M37 they were either:
        #   - dumped wholesale into user_profile (M14.6 — regressed
        #     SSU 5/5 → 1/5 from prompt bloat)
        #   - skipped entirely (M14.7 revert — preference kind suppressed)
        # M37: score MMs by token-overlap with the question, take top-K,
        # mix into candidate pool. Cross-encoder picks the few that
        # actually match.
        #
        # Gated by ASTROCYTE_M37_MM_CANDIDATES (default ON = "1"); set
        # to "0" to skip for ablation. Default ON because M37 is the
        # cycle's central thesis.
        _m37_on = _os.environ.get("ASTROCYTE_M37_MM_CANDIDATES", "1").lower() in ("1", "true", "yes")
        if _m37_on and self._mental_model_store is not None:
            # M35 — token-budget cap is the API consistent primitive.
            # Default 512 tokens ≈ 2-4 MMs of typical length. The
            # downstream unified candidate pool already gets cross-
            # encoder reranked and token-budgeted at max_tokens=4096
            # via fact_recall's pack_to_budget, so this knob is just
            # the upper bound on how many MMs the reranker even sees.
            try:
                _m37_mm_max_tokens = int(_os.environ.get("ASTROCYTE_M37_MM_MAX_TOKENS", "512"))
            except ValueError:
                _m37_mm_max_tokens = 512
            _m37_mm_max_tokens = max(0, _m37_mm_max_tokens)
            # M37b — embedding-based MM search. When ON, cosine score
            # MMs against the query embedding (catches paraphrases that
            # token-overlap misses). When OFF (default), use token-overlap
            # for backward compat with v015p baseline.
            _m37b_embed = _os.environ.get("ASTROCYTE_M37B_MM_EMBED", "1").lower() in ("1", "true", "yes")
            if _m37_mm_max_tokens > 0:
                try:
                    bank_id = self._bank_for(user_id)
                    all_mms = await self._mental_model_store.list(
                        bank_id,
                        scope=f"document:{self._doc_ids.get(user_id, '')}",
                    )
                    scored: list[tuple[float, Any]] = []
                    if _m37b_embed:
                        # M37b — cosine on cached MM embeddings.
                        # First call per user: batched embed of all MMs
                        # (~1-2s for ~1000 MMs); subsequent calls use
                        # cache (sub-ms).
                        if user_id not in self._mm_embedding_cache:
                            self._mm_embedding_cache[user_id] = {}
                            uncached_mms = [
                                mm for mm in all_mms if mm is not None
                                and getattr(mm, "model_id", None) is not None
                            ]
                            if uncached_mms:
                                _mm_texts = [
                                    f"{(getattr(mm, 'title', '') or '').strip()} {(getattr(mm, 'content', '') or '').strip()}".strip()
                                    for mm in uncached_mms
                                ]
                                try:
                                    _mm_vecs = await self._provider.embed(_mm_texts)
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("M37b MM embed batch failed: %s", exc)
                                    _mm_vecs = []
                                for mm, vec in zip(uncached_mms, _mm_vecs):
                                    if vec:
                                        self._mm_embedding_cache[user_id][mm.model_id] = (mm, vec)
                        # Cosine score (qvec was computed earlier from retrieval_query).
                        cache = self._mm_embedding_cache.get(user_id, {})
                        if cache and qvec:
                            from math import sqrt  # noqa: PLC0415

                            _q_norm = sqrt(sum(x * x for x in qvec))
                            for _model_id, (mm, vec) in cache.items():
                                if not vec:
                                    continue
                                _v_norm = sqrt(sum(x * x for x in vec))
                                if _q_norm == 0 or _v_norm == 0:
                                    continue
                                dot = sum(a * b for a, b in zip(qvec, vec))
                                cos = dot / (_q_norm * _v_norm)
                                if cos > 0.0:  # cheap dedupe; cosine [0,1] expected
                                    scored.append((float(cos), mm))
                    else:
                        # Token-overlap scoring (v015p behaviour): count
                        # distinct query terms appearing in the MM's
                        # title + content.
                        _q_terms = {t for t in query.lower().split() if len(t) > 2}
                        for mm in all_mms:
                            if mm is None:
                                continue
                            haystack = f"{getattr(mm, 'title', '') or ''} {getattr(mm, 'content', '') or ''}".lower()
                            if not haystack.strip() or not _q_terms:
                                continue
                            score = sum(1 for t in _q_terms if t in haystack)
                            if score > 0:
                                scored.append((float(score), mm))
                    scored.sort(key=lambda kv: kv[0], reverse=True)
                    # M35 — pack scored MMs into the token budget. The
                    # always-include-first rule (see pack_to_budget) means
                    # an oversize top MM still surfaces (we'd rather show
                    # a long highly-scored MM than skip it for budget).
                    from astrocyte.pipeline.token_budget import pack_to_budget  # noqa: PLC0415

                    mm_text_of = lambda kv: (  # noqa: E731
                        f"[Mental Model: {(getattr(kv[1], 'title', '') or '').strip()} "
                        f"(kind={getattr(kv[1], 'kind', 'general')})] "
                        f"{(getattr(kv[1], 'content', '') or '').strip()}"
                    )
                    packed = pack_to_budget(
                        scored,
                        max_tokens=_m37_mm_max_tokens,
                        text_of=mm_text_of,
                    )
                    for _score, mm in packed:
                        title = (getattr(mm, "title", "") or "").strip()
                        content = (getattr(mm, "content", "") or "").strip()
                        if not title and not content:
                            continue
                        mm_id = f"mental_model:{getattr(mm, 'model_id', '')}"
                        if mm_id in seen_ids:
                            continue
                        kind = getattr(mm, "kind", "general")
                        mm_text = f"[Mental Model: {title} (kind={kind})] {content}".strip()
                        seen_ids.add(mm_id)
                        candidates.append(
                            {
                                "id": mm_id,
                                "text": mm_text,
                                "grain": "mental_model",
                                "mental_model": mm,
                            },
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "M37 mental-model candidate injection failed user=%s: %s",
                        user_id, exc,
                    )

        if not candidates:
            return []

        # ── (4) Unified cross-encoder rerank over the union ────────
        try:
            from astrocyte.pipeline.cross_encoder_rerank import (  # noqa: PLC0415
                cross_encoder_rerank,
            )
            from astrocyte.pipeline.reranking import ScoredItem  # noqa: PLC0415

            # M49 — Date prefix on cross-encoder inputs (Hindsight parity,
            # reranking.py:197-208). When a candidate has a resolved date
            # (event_date or occurred_start for facts; refreshed_at for
            # MMs is too coarse to bother with here), prepend
            # ``[Date: <human> (<iso>)] `` to its text BEFORE handing to
            # the cross-encoder. The reranker can then break vector-
            # similarity ties on temporal proximity / specificity, which
            # the M44 audit identified as missing infrastructure. Gated
            # by ASTROCYTE_M49_DATE_PREFIX_RERANK (default ON — port
            # of a Hindsight default behavior, not an opt-in feature).
            #
            # The prefix is applied ONLY to the text shown to the
            # cross-encoder; the candidate's own text in the returned
            # entry stays unchanged (so the answerer sees the original
            # fact text, not the prefix).
            _m49_on = _os.environ.get("ASTROCYTE_M49_DATE_PREFIX_RERANK", "1").lower() in (
                "1", "true", "yes",
            )

            def _fact_date_for_rerank(c: dict[str, Any]) -> str | None:
                """Return YYYY-MM-DD if the candidate has a usable date.

                Priority for facts: event_date > occurred_start > mentioned_at.
                Skips mental_model / section / wiki grains (their dates are
                refreshed-at / session-date; less semantically the "when
                the event happened" the reranker can exploit).
                """
                if c.get("grain") != "fact":
                    return None
                fh = c.get("fact")
                if fh is None:
                    return None
                for attr in ("event_date", "occurred_start", "mentioned_at"):
                    dt = getattr(fh, attr, None)
                    if dt is not None:
                        try:
                            return dt.strftime("%Y-%m-%d")
                        except (AttributeError, ValueError):
                            continue
                return None

            def _humanize_iso(iso: str) -> str:
                try:
                    from datetime import datetime as _dt  # noqa: PLC0415

                    d = _dt.fromisoformat(iso)
                    return d.strftime("%B %-d, %Y")
                except (ValueError, AttributeError):
                    return iso

            if _m49_on:
                items = []
                for c in candidates:
                    text = c["text"]
                    iso_date = _fact_date_for_rerank(c)
                    if iso_date:
                        prefix = f"[Date: {_humanize_iso(iso_date)} ({iso_date})] "
                        text = prefix + text
                    items.append(ScoredItem(id=c["id"], text=text, score=0.0))
            else:
                items = [ScoredItem(id=c["id"], text=c["text"], score=0.0) for c in candidates]

            # M38 — the cross-encoder scores text similarity between
            # query and candidate. retrieval_query (rewritten) is the
            # search-intent expression; using it here keeps the ranker
            # aligned with the retrieval. Falls back to original when
            # M38 is off.
            reranked = cross_encoder_rerank(items, retrieval_query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("unified rerank failed for user=%s: %s", user_id, exc)
            reranked = [ScoredItem(id=c["id"], text=c["text"], score=0.0) for c in candidates]

        by_id = {c["id"]: c for c in candidates}

        # ── M40 — Trend-based rerank boost on mental-model candidates ─────
        # When ASTROCYTE_M40_TREND_BOOST is enabled, multiply each MM
        # candidate's rerank score by a per-trend weight. For v1 we only
        # boost mental_model grain — facts mostly have single-evidence
        # (NEW/STALE collapse) so the signal is degenerate there.
        # The MM "evidence timestamp" is the model's refreshed_at (single-
        # point fallback in compute_observation_trend semantics): a
        # recently-refreshed MM trends NEW; an old one trends STALE.
        # Reference time = the question's reference_date (anchor for the
        # LME conversation timeline), not wall-clock now.
        _m40_boost_on = _os.environ.get("ASTROCYTE_M40_TREND_BOOST", "0").lower() in ("1", "true", "yes")
        _m40_trends_by_id: dict[str, str] = {}
        if _m40_boost_on:
            try:
                from astrocyte.pipeline.trend import (  # noqa: PLC0415
                    Trend,
                    compute_trend,
                )

                _trend_recent_days = int(_os.environ.get("ASTROCYTE_TREND_RECENT_DAYS", "30"))
                _trend_old_days = int(_os.environ.get("ASTROCYTE_TREND_OLD_DAYS", "90"))
                # Additive deltas — applied as fractions of the observed
                # score range in this candidate pool. Sign-agnostic across
                # MiniLM (logits [-12, 0]) / mxbai-rerank-v2 (sigmoid
                # [0, 1]) / bge-reranker (logits). STALE gets the sharpest
                # penalty because a stale preference is the SSP failure
                # mode we want to suppress.
                _trend_deltas = {
                    Trend.STRENGTHENING: 0.05,
                    Trend.STABLE: 0.0,
                    Trend.NEW: 0.0,
                    Trend.WEAKENING: -0.05,
                    Trend.STALE: -0.15,
                }
                ref_now = self._reference_date_by_user.get(user_id)
                _scores_all = [float(it.score) for it in reranked]
                _score_range = (
                    (max(_scores_all) - min(_scores_all))
                    if len(_scores_all) >= 2 else 1.0
                )
                if _score_range <= 0:
                    _score_range = 1.0
                _boosted: list[Any] = []
                for item in reranked:
                    c = by_id.get(item.id)
                    if c is None or c.get("grain") != "mental_model":
                        _boosted.append(item)
                        continue
                    mm = c.get("mental_model")
                    # M40-5 — prefer persisted source_timestamps (in
                    # conversation/LME time) over refreshed_at (wall-
                    # clock). The latter is broken in bench-time because
                    # ingest just ran "now" while reference_date is in
                    # 2023 — every MM trends NEW under that fallback.
                    # Legacy rows pre-M40-4 (source_timestamps=None)
                    # still fall back to refreshed_at — degraded signal
                    # but not crashing.
                    evidence_ts: list[Any] = []
                    if mm is not None:
                        persisted = getattr(mm, "source_timestamps", None)
                        if persisted:
                            evidence_ts = list(persisted)
                        else:
                            refreshed = getattr(mm, "refreshed_at", None)
                            if refreshed is not None:
                                evidence_ts = [refreshed]
                    if not evidence_ts:
                        _boosted.append(item)
                        continue
                    trend = compute_trend(
                        evidence_ts,
                        now=ref_now,
                        recent_days=_trend_recent_days,
                        old_days=_trend_old_days,
                    )
                    _m40_trends_by_id[item.id] = trend.value
                    delta = _trend_deltas.get(trend, 0.0)
                    _boosted.append(
                        ScoredItem(
                            id=item.id,
                            text=item.text,
                            score=float(item.score) + (delta * _score_range),
                        ),
                    )
                # Re-sort after boost; rerank scores are no longer monotonic.
                _boosted.sort(key=lambda s: float(s.score), reverse=True)
                reranked = _boosted
            except Exception as exc:  # noqa: BLE001
                logger.warning("M40 trend boost failed user=%s: %s", user_id, exc)

        # NOTE: M19 Workstream C (multiplicative bounded boosts on unified
        # rerank, mirroring Hindsight reranking.py) was implemented and
        # benched here in two iterations. Both regressed:
        #   - C0 (boosts on, no date plumbing): parity-noise on bench-pool
        #     because most candidates lacked date metadata; multipliers
        #     collapsed to 1.0. No-op.
        #   - C0' (boosts on + load_skeleton date plumbing): -7q vs shipped
        #     B1-dp+RRF; top_50/top_200 IMPROVED but top_20 dropped because
        #     the recency boost (Hindsight default alpha=0.2) penalises
        #     OLDER sections — and our temporal-reasoning / multi-hop
        #     questions often need older sections for date arithmetic and
        #     cross-session synthesis.
        # Conclusion: Hindsight's boost alpha defaults are tuned for their
        # workload (recent = relevant); ours is mixed-temporality. The
        # boost layer stays available via `astrocyte.pipeline.rerank_boosts`
        # (still used by `section_rerank`), but is NOT applied to the
        # unified fact+section+wiki pool in the bench. Per-question-type
        # routing (M19a) achieved the +4q ship without the regression
        # cost. Full diagnosis: docs/_design/m19-prompt-routing.md §8.

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
                # M24 architectural fix — surface the Hindsight-parity
                # chunk_id + inline source_chunk text so the answerer
                # prompt can render Fact ↔ Source as a pair (not in
                # separate sections of the context).
                if c.get("chunk_id"):
                    entry["chunk_id"] = c["chunk_id"]
                if c.get("source_chunk"):
                    entry["source_chunk"] = c["source_chunk"]
                # M28 Workstream B — surface M27 fields so the answerer's
                # structured context renderer can show confidence and the
                # dual occurred/mentioned timestamps.
                if c.get("confidence_score") is not None:
                    entry["confidence_score"] = c["confidence_score"]
                mentioned = c.get("mentioned_at")
                if mentioned is not None:
                    entry["mentioned_at"] = (
                        mentioned.isoformat() if hasattr(mentioned, "isoformat") else mentioned
                    )
                # M31 Fix 4 — resolved absolute event date (when present).
                # Answerer renderer prefers this over relative-phrase math.
                event_date = c.get("event_date")
                if event_date is not None:
                    entry["event_date"] = (
                        event_date.isoformat() if hasattr(event_date, "isoformat") else event_date
                    )
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

            # ── M40 — Trend annotation on mental_model context text ──────
            # When ASTROCYTE_M40_TREND_ANNOTATE is enabled, prefix MM
            # memory text with a short trend tag so the answerer prompt
            # can prefer stable preferences over weakening ones. Always
            # surface trend on entry["metadata"] for debug regardless of
            # annotation, when it was computed by the boost branch above.
            if c["grain"] == "mental_model":
                _trend_value = _m40_trends_by_id.get(item.id)
                if _trend_value:
                    entry["metadata"]["trend"] = _trend_value
                    _m40_annotate_on = _os.environ.get(
                        "ASTROCYTE_M40_TREND_ANNOTATE", "0",
                    ).lower() in ("1", "true", "yes")
                    # Skip the "stable" no-signal default — it would just
                    # pollute the prompt with the common case.
                    if _m40_annotate_on and _trend_value != "stable":
                        entry["memory"] = f"[trend:{_trend_value}] {entry['memory']}"

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
        # M31b Fix A — DISABLED raw-chunk anchor-injection (was: prepend
        # top-3 raw chunks at synthetic score=1_000_000 forcing them to
        # the top of the answerer's context regardless of cross-encoder
        # relevance). Diagnosis in M31b post-mortem: the override was
        # BURYING relevant facts at ranks 15-55 because cross-encoder
        # scores are typically in [-12, 0]; the 1M override jumped all
        # generic raw chunks to top-3 of the answerer prompt, crowding
        # out user-specific facts that actually answer the question
        # (concrete example: SSP 06f04340 — "tomatoes/herbs" question
        # — relevant basil/mint/cherry-tomato facts ranked 15-55, never
        # made it into the answerer's attention window).
        #
        # The same sections fed by ``recall.fused`` already enter the
        # candidate pool at the section-grain loop above (line ~944),
        # where they get scored by cross-encoder alongside facts. Keep
        # ``_build_raw_conversation_excerpts`` defined for ad-hoc reuse
        # but no longer prepend its output. If a future cycle wants
        # raw-chunk context, it should feed it through the rerank pool
        # at a sane score (not a hard-coded override).
        return out

    async def reflect(
        self,
        question: str,
        user_id: str,
        *,
        max_iterations: int | None = None,
        top_k_seed: int | None = None,
    ) -> dict[str, Any]:
        """Run the native-function-calling reflect loop and return the
        synthesized answer directly.

        Hindsight-parity integrated mode (M20 Day 3 rewrite, see
        ``docs/_design/m20-reflect-agent.md`` §7): the predicted answer
        is ``result.text`` from the reflect loop — NOT routed through
        a downstream answerer. Mirrors
        ``hindsight-dev/benchmarks/locomo/locomo_benchmark.py:LoComoReflectAnswerGenerator``
        which sets ``needs_external_search() = False`` so the runner
        skips the recall+answerer chain entirely.

        Returns:
            ``{"answer": str, "sources": [{"text", "score", "id"}, ...],
              "iterations": int}``. Caller should use ``answer`` as the
            predicted answer at every cutoff (reflect has no cutoff
            concept — same answer for top_10/20/50/200).
        """
        await self._ensure_resources()
        await self._ensure_ingested(user_id)
        from astrocyte.pipeline.agentic_reflect import (  # noqa: PLC0415
            AgenticReflectParams,
            agentic_reflect,
        )
        from astrocyte.types import MemoryHit  # noqa: PLC0415

        # Seed evidence: reuse the existing search() pipeline so reflect
        # starts from the same retrieval pool the recall+answerer baseline
        # gets. ``search()`` already integrates fact_recall + section_recall
        # + cross-encoder rerank + raw excerpt injection.
        #
        # **M20 R1c**: bumped default from 50 → 200 so reflect's loop sees
        # the SAME breadth of evidence the R0 answerer sees at top_200
        # (where R0 scores 173/200, +12q above R1b's 162/200). Tests
        # whether reflect's regression is information-limited (loop sees
        # less than answerer) or synthesis-limited (loop sees same but
        # composes worse). Env override: ``ASTROCYTE_REFLECT_SEED_TOP_K``.
        if top_k_seed is None:
            _env_seed = _os.environ.get("ASTROCYTE_REFLECT_SEED_TOP_K", "")
            if _env_seed.isdigit():
                top_k_seed = int(_env_seed)
            else:
                top_k_seed = 200
        seed_top = max(top_k_seed, self.search_top_k_floor)
        base_candidates = await self.search(
            query=question,
            user_id=user_id,
            top_k=seed_top,
        )

        def _to_hit(c: dict) -> MemoryHit:
            # M26 Phase 3: thread M24's inline ``source_chunk`` into the
            # MemoryHit's metadata so the agent's tool-response formatter
            # (``_format_hits_for_tool_response`` in agentic_reflect.py)
            # can render Fact + Source pair inline — Hindsight parity for
            # the reflect loop's sub-recall outputs. Without this, the
            # agent sees fact text only and loses the chunk-level
            # verbatim cross-reference that made M24 work for SSP.
            meta = dict(c.get("metadata") or {})
            if c.get("source_chunk"):
                meta["source_chunk"] = c["source_chunk"]
            if c.get("chunk_id"):
                meta["chunk_id"] = c["chunk_id"]
            return MemoryHit(
                text=c.get("memory", ""),
                score=float(c.get("score", 0.0)),
                memory_id=c.get("id"),
                metadata=meta if meta else None,
            )

        initial_hits = [_to_hit(c) for c in base_candidates]

        async def _loop_recall(sub_query: str, max_results: int) -> list[MemoryHit]:
            sub_candidates = await self.search(
                query=sub_query,
                user_id=user_id,
                top_k=max(max_results, self.search_top_k_floor),
            )
            return [_to_hit(c) for c in sub_candidates[:max_results]]

        if max_iterations is None:
            max_iter_env = _os.environ.get("ASTROCYTE_REFLECT_MAX_ITERATIONS", "5")
            try:
                max_iterations = max(1, int(max_iter_env))
            except ValueError:
                max_iterations = 5

        params = AgenticReflectParams(
            max_iterations=max_iterations,
            recall_step_max_results=10,
            max_evidence_pool_size=30,
            adversarial_defense=False,
        )

        result = await agentic_reflect(
            query=question,
            initial_hits=initial_hits,
            recall_fn=_loop_recall,
            llm_provider=self._provider,
            params=params,
        )

        return {
            "answer": result.answer,
            "sources": [
                {
                    "text": src.text,
                    "score": float(src.score),
                    "id": src.memory_id or "",
                }
                for src in result.sources
            ],
            "iterations": max_iterations,
        }

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

    async def list_mental_models_for_bench(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """M22 Gap 3 — return user's mental models as JSON dicts for the
        Hindsight-style answerer prompt's ``=== Mental Models ===``
        section.

        Filters to ``kind in {general, directive}`` per the M14.7 revert
        rationale (``preference`` rows bloated context and regressed LME
        single-session-user 5/5 → 1/5). The list is bounded by ``limit``
        with most-recently-refreshed first.

        Returns an empty list when no models exist or the bench hasn't
        ingested this user yet — the prompt formatter then skips the
        section gracefully.
        """
        await self._ensure_resources()
        if self._mental_model_store is None:
            return []
        if user_id not in self._ingested:
            return []
        document_id = self._doc_ids.get(user_id)
        if document_id is None:
            return []
        try:
            models = await self._mental_model_store.list(
                self._bank_for(user_id),
                scope=f"document:{document_id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_mental_models_for_bench user=%s: %s", user_id, exc)
            return []
        out: list[dict] = []
        for m in models:
            kind = getattr(m, "kind", "general")
            if kind not in {"general", "directive"}:
                continue
            title = (m.title or "").strip()
            content = (m.content or "").strip()
            if not title or not content:
                continue
            out.append({"title": title, "content": content, "kind": kind})
            if len(out) >= limit:
                break
        return out

    async def list_observations_for_bench(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """M22 Gap 3 — return user's wiki-page observations as JSON dicts
        for the Hindsight-style answerer prompt's ``=== Entity
        Observations ===`` section.

        The bench harness ingest path (``_build_document_pageindex`` →
        ``wiki_compile``) produces per-document wiki summaries that
        consolidate facts across sessions — the closest analog in the
        bench-side ingestion to Hindsight's reflective observations.
        True ``ObservationConsolidator`` output never reaches the bench
        because the bench bypasses ``PipelineOrchestrator``; surfacing
        wikis under the same prompt section gives the answerer the
        same cross-session aggregate signal at the same cost.

        Returns an empty list when no wikis exist yet (e.g. small
        sessions where the compiler produced zero pages).
        """
        await self._ensure_resources()
        if self._store is None:
            return []
        if user_id not in self._ingested:
            return []
        document_id = self._doc_ids.get(user_id)
        if document_id is None:
            return []
        try:
            wikis = await self._store.list_wiki_pages_for_doc(
                self._bank_for(user_id),
                document_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_observations_for_bench user=%s: %s", user_id, exc)
            return []
        out: list[dict] = []
        for w in wikis:
            text = (getattr(w, "content", "") or "").strip()
            if not text:
                continue
            out.append(
                {
                    "text": text,
                    "trend": "stable",  # wikis don't carry computed trend; render as stable
                    "proof_count": getattr(w, "n_sources", 1) or 1,
                }
            )
            if len(out) >= limit:
                break
        return out

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
            _enrich_nodes_with_dates,  # M33-3a — fixes section.session_date population
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
                # M33-3a — populate per-node ``date`` field from section
                # titles so _flatten_tree_to_sections can set
                # section.session_date. Without this, mentioned_at /
                # event_date / temporal recall all degrade to no-ops.
                _enrich_nodes_with_dates(raw_tree)
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
                # M33-3a — same enrichment as the framework-native path.
                _enrich_nodes_with_dates(raw_tree)
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
