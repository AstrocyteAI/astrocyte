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

from pageindex.page_index_md import md_to_tree  # noqa: E402

from astrocyte.types import PageIndexDocument  # noqa: E402
from scripts.mem0_harness._shape import is_recommendation_shape  # noqa: E402

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

        # ── (1) Fact-grain candidates ──────────────────────────────
        try:
            fact_hits = await self._store.search_facts_semantic(
                bank_id, qvec, top_k=40, document_id=document_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fact semantic search failed for user=%s: %s", user_id, exc)
            fact_hits = []

        # ── (2) Section-grain candidates via section_recall ────────
        # ``mode="single-hop"`` fires the semantic + entity + link
        # strategies without temporal-specific pre-filtering — a sane
        # default when the Mem0 harness has no question category.
        from astrocyte.pipeline.section_recall import section_recall  # noqa: PLC0415
        try:
            recall = await section_recall(
                store=self._store, bank_id=bank_id, question=query,
                mode="single-hop", embedding_provider=self._provider,
                wiki_enabled=True, wiki_document_id=document_id,
                wiki_min_score=0.25, wiki_top_k=4,
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
            candidates.append({
                "id": cid, "text": fh.text, "grain": "fact",
                "fact": fh,
            })

        if recall is not None and recall.fused:
            for sh in recall.fused[:8]:
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
                candidates.append({
                    "id": cid, "text": excerpt, "grain": "section",
                    "section": sh,
                })

        if recall is not None and getattr(recall, "wiki_hits", None):
            for w in recall.wiki_hits[:4]:
                wiki_text = f"[Observation: {w.title}] {w.content}".strip()
                cid = f"wiki:{w.page_id}"
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                candidates.append({
                    "id": cid, "text": wiki_text, "grain": "wiki",
                    "wiki": w,
                })

        if not candidates:
            return []

        # ── (4) Unified cross-encoder rerank over the union ────────
        try:
            from astrocyte.pipeline.cross_encoder_rerank import (  # noqa: PLC0415
                cross_encoder_rerank,
            )
            from astrocyte.pipeline.reranking import ScoredItem  # noqa: PLC0415

            items = [
                ScoredItem(id=c["id"], text=c["text"], score=0.0)
                for c in candidates
            ]
            reranked = cross_encoder_rerank(items, query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("unified rerank failed for user=%s: %s", user_id, exc)
            reranked = [
                ScoredItem(id=c["id"], text=c["text"], score=0.0)
                for c in candidates
            ]

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

        # ── (5) B-1 / B-2a: preference-anchor pinning for recommendation-
        # shape questions. See ``_is_recommendation_shape`` docstring for
        # the rationale and the diagnostic that motivated this.
        #
        # We prepend up to 5 consolidated preference-kind MentalModels at
        # the top of the candidate list with an inline instruction prefix
        # (B-2a — "[Preference — apply this to your suggestion below]")
        # so the answerer treats them as anchors rather than ordinary
        # memories. They appear in BOTH the User Profile block AND the
        # memories list — redundancy is intentional because the LME
        # answerer rule #12 explicitly tells it to scan the top memories.
        #
        # Capped at 5 anchors so even at top_10 floor we still keep 5
        # ranked candidates. The hard-trim to ``floor`` at the end ensures
        # no caller sees an oversized list.
        if (
            self._mental_model_store is not None
            and is_recommendation_shape(query)
        ):
            try:
                pref_mms = await self._mental_model_store.list(
                    bank_id,
                    scope=f"document:{document_id}",
                    kind="preference",
                )
            except TypeError:
                # Older MentalModelStore SPI without ``kind`` kwarg.
                all_mms = await self._mental_model_store.list(
                    bank_id, scope=f"document:{document_id}",
                )
                pref_mms = [
                    m for m in all_mms
                    if getattr(m, "kind", "general") == "preference"
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "B-1 anchor list failed user=%s: %s", user_id, exc,
                )
                pref_mms = []

            if pref_mms:
                top_score = (out[0]["score"] if out else 0.0) + 1.0
                anchor_entries: list[dict[str, Any]] = []
                for m in pref_mms[:5]:
                    title = (m.title or "").strip()
                    content = (m.content or "").strip()
                    if not title or not content:
                        continue
                    anchor_entries.append({
                        "memory": (
                            "[Preference — apply this to your suggestion "
                            f"below] {title}: {content}"
                        ),
                        "score": float(top_score),
                        "id": f"anchor:{m.model_id}",
                        "metadata": {"grain": "anchor", "kind": "preference"},
                    })
                    top_score -= 0.001  # preserve insertion order on sort
                if anchor_entries:
                    out = (anchor_entries + out)[:floor]
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

        # M14.6: prefix preference-kind keys with "preference_" so Mem0's
        # `snake_case → Title Case` renderer surfaces them as
        # ``Preference …`` entries — visually distinct from general
        # profile statements, and lexicographically grouped at the top
        # of the formatted ``## User Profile`` block (dict iteration
        # order is insertion-order in Python 3.7+; we insert preferences
        # first below).
        profile: dict[str, str] = {}
        seen_keys: set[str] = set()

        # Pass 1: preference-kind models first (most relevant for
        # single-session-preference and related categories).
        for m in models:
            if getattr(m, "kind", "general") != "preference":
                continue
            title = (m.title or "").strip()
            content = (m.content or "").strip()
            if not title or not content:
                continue
            key = "preference_" + _slug_snake_case(title)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            profile[key] = content

        # Pass 2: general-kind models second.
        for m in models:
            if getattr(m, "kind", "general") == "preference":
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
            (t for t in reversed(ts_order) if t is not None), None,
        )

        md_parts = [f"# Conversation {user_id}", ""]
        for idx, ts in enumerate(ts_order, start=1):
            md_parts.append(_format_messages_as_markdown(
                sessions[ts], session_idx=idx, timestamp=ts,
            ))
        md_text = "\n".join(md_parts).strip()
        # Cache for M13.2 section-excerpt rendering at search time.
        self._md_text_by_user[user_id] = md_text
        reference_date = (
            datetime.fromtimestamp(latest_ts, tz=timezone.utc)
            if latest_ts is not None
            else datetime.now(tz=timezone.utc)
        )

        # 1. Save document + build PageIndex tree.
        start = time.monotonic()
        doc = PageIndexDocument(
            id="", bank_id=bank_id, source_id=user_id, md_text=md_text,
            reference_date=reference_date,
            built_at=datetime.now(tz=timezone.utc),
        )
        document_id = await self._store.save_document(doc)
        self._doc_ids[user_id] = document_id

        try:
            md_tmp = Path(f"/tmp/astrocyte-mem0-harness-{user_id.replace('/', '_')}.md")
            md_tmp.write_text(md_text)
            raw_tree = await md_to_tree(
                str(md_tmp),
                if_add_node_summary="yes",
                summary_token_threshold=200,
                model=self.tree_build_model,
                if_add_node_text="no",
                if_add_node_id="yes",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("md_to_tree failed for user=%s: %s", user_id, exc)
            return

        try:
            sections = _flatten_tree_to_sections(raw_tree, document_id)
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
            user_id, len(buffer), document_id, elapsed,
        )


# Helper that mirrors mem0_client.format_search_results for the runner.
def format_search_results(search_results: list[dict]) -> tuple[list[dict], dict | None]:
    """Pass-through; AstrocyteClient already returns normalised + sorted."""
    if not search_results:
        return [], None
    sorted_results = sorted(search_results, key=lambda x: x.get("score", 0), reverse=True)
    return sorted_results, None
