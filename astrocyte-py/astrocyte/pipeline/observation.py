"""Observation consolidation — semantic deduplication and synthesis layer.

After each ``retain()`` call, this module fires a background LLM pass that
maintains a deduplicated *observations layer* on top of raw memories.  The
design is inspired by the Hindsight memory system (vectorize-io/hindsight,
MIT licence) and adapted to Astrocyte's existing multi-strategy RRF pipeline.

Terminology
-----------
- **Raw memory** — a chunk stored verbatim by ``retain()``; ``fact_type`` is
  ``"world"``, ``"experience"``, or ``None``.
- **Observation** — a distilled, deduplicated atomic fact synthesized from one
  or more raw memories; stored as a ``VectorItem`` with ``fact_type="observation"``
  and extra bookkeeping metadata (``_obs_proof_count``, ``_obs_source_ids``,
  ``_obs_confidence``).

Lifecycle
---------
1. ``retain()`` stores raw chunks as usual.
2. Immediately after, ``ObservationConsolidator.consolidate()`` is called
   (fire-and-forget by default; awaitable for tests).
3. The consolidator fetches the top-K semantically-related existing
   observations, then calls the LLM to produce structured
   ``create`` / ``update`` / ``delete`` actions.
4. Actions are applied to the vector store:
   - ``create`` → new ``VectorItem(fact_type="observation", ...)``
   - ``update`` → delete the old item, insert a revised one with incremented
     ``_obs_proof_count`` and merged ``_obs_source_ids``
   - ``delete`` → remove the observation from the store
5. During ``recall()``, the orchestrator runs an additional ``observation``
   strategy (``search_similar`` with ``fact_types=["observation"]``) and
   injects its results into the RRF fusion with a configurable weight boost,
   so confirmed multi-evidence observations naturally rank above single-mention
   raw memories.

Prompt design
-------------
The LLM receives a brief of existing observations (with proof counts) and
the new memory, then outputs a JSON array of actions.  The prompt is kept
deliberately short to minimise token cost — the consolidator is in the
critical path of every retain, even in fire-and-forget mode.  A ``max_obs``
cap (default 10) limits context window growth.

Metadata schema on observation VectorItems
------------------------------------------
``_obs_proof_count``  int (stored as int)   — # supporting memories
``_obs_source_ids``   str (JSON list)       — raw memory IDs that contributed
``_obs_confidence``   str (float, 0–1)      — LLM-assigned confidence
``_obs_updated_at``   str (ISO datetime)    — last mutation timestamp
``_obs_scope``        str                   — scope key ("bank" or caller tag)
``_obs_freshness``    str                   — "fresh" | "stale"
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, VectorStore
    from astrocyte.types import VectorHit

logger = logging.getLogger("astrocyte.pipeline.observation")

# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------

_VALID_ACTIONS = frozenset({"create", "update", "delete"})

# ---------------------------------------------------------------------------
# Observation bank naming
# ---------------------------------------------------------------------------

_OBS_SUFFIX = "::obs"


def obs_bank_id(bank_id: str) -> str:
    """Return the dedicated observation bank for ``bank_id``.

    Observations are stored in a *separate* bank (``{bank_id}::obs``) so that
    the main semantic/keyword/temporal strategies never retrieve them.  This
    prevents observations from double-counting in RRF fusion (they would
    otherwise appear in both the ``semantic`` results and the ``observation``
    strategy results, crowding out the raw source memories that carry verbatim
    answers).
    """
    return f"{bank_id}{_OBS_SUFFIX}"


def observation_scope(bank_id: str, tags: list[str] | None = None) -> str:
    """Return a stable observation scope for a retain/recall context."""
    if tags:
        return "|".join(sorted(str(tag) for tag in tags))
    return f"bank:{bank_id}"

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ObservationConsolidationResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a memory consolidation agent.  Your job is to maintain a concise, \
deduplicated set of *observations* — atomic facts distilled from raw memories.

Given:
- A list of existing observations (each with an ID, proof count, and text).
- A new memory to integrate.

Output a JSON array of actions.  Each action is one of:
  {"action": "create", "text": "<observation>", "confidence": <0.0–1.0>}
  {"action": "update", "obs_id": "<id>", "text": "<revised observation>", \
"confidence": <0.0–1.0>}
  {"action": "delete", "obs_id": "<id>"}

Rules (Hindsight-style three-strategy reconciliation):
1. REDUNDANT — same fact in different words: UPDATE the existing observation \
to the cleaner phrasing and raise confidence. Don't create a near-duplicate.
2. STATE UPDATE — new info supersedes old state: UPDATE the observation to \
preserve the *journey*. Use phrases like "used to X, now Y" or "changed from \
X to Y" so the temporal evolution is captured. Never silently overwrite — \
the agent must be able to answer questions about the prior state.
3. DIRECT CONTRADICTION (rare) — irreconcilable claim with no temporal frame: \
DELETE the old observation only when the new memory definitively supersedes \
it AND no journey can be expressed (e.g. an outright correction of a wrong \
fact). When in doubt, prefer UPDATE-with-journey over DELETE.
4. CREATE if the new memory introduces a fact not already covered.
5. If the new memory is fully redundant *and* the existing observation \
already captures it precisely, respond with [].
6. Preserve stable persona and preference facts: identities, goals, hobbies, \
relationships, repeated activities, values, career plans, and stated likes/dislikes.

Constraints:
- Each observation must be a single, independently-useful sentence (≤ 30 words).
- Output valid JSON only.  No prose before or after the array.
- Maximum 5 actions per call.
"""


def _build_user_prompt(
    existing_obs: list[VectorHit],
    new_memory_text: str,
    *,
    max_obs: int = 10,
) -> str:
    lines: list[str] = ["Existing observations:"]
    if existing_obs:
        for obs in existing_obs[:max_obs]:
            proof = (obs.metadata or {}).get("_obs_proof_count", 1)
            conf = (obs.metadata or {}).get("_obs_confidence", "?")
            lines.append(f"[{obs.id}] (proof_count={proof}, confidence={conf}): {obs.text}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("New memory:")
    lines.append(new_memory_text.strip())
    lines.append("")
    lines.append("Actions (JSON array):")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action parsing
# ---------------------------------------------------------------------------


def _parse_actions(raw: str) -> list[dict[str, Any]]:
    """Extract the JSON array from the LLM response.

    The LLM is instructed to output *only* a JSON array, but may include
    leading/trailing whitespace or a markdown code fence.  We extract the
    first ``[...`` block.
    """
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        ).strip()

    # Find the first JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        logger.debug("No JSON array found in consolidation response: %r", raw[:200])
        return []

    try:
        actions = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error in consolidation response: %s — %r", exc, raw[:200])
        return []

    if not isinstance(actions, list):
        return []

    validated: list[dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action not in _VALID_ACTIONS:
            continue
        validated.append(item)

    return validated


# ---------------------------------------------------------------------------
# Core consolidator
# ---------------------------------------------------------------------------


class ObservationConsolidator:
    """Maintains the observations layer via post-retain LLM consolidation.

    Parameters
    ----------
    max_context_obs:
        Maximum number of existing observations fetched for the LLM prompt.
        Higher values give the LLM more context but increase token cost.
    min_confidence:
        Observations with confidence below this threshold are not stored.
    observation_recall_limit:
        How many existing observations to fetch per consolidation call.
        This is the ``limit`` passed to ``vector_store.search_similar``.
    """

    def __init__(
        self,
        *,
        max_context_obs: int = 10,
        min_confidence: float = 0.5,
        observation_recall_limit: int = 15,
    ) -> None:
        self.max_context_obs = max_context_obs
        self.min_confidence = min_confidence
        self.observation_recall_limit = observation_recall_limit

    async def consolidate(
        self,
        new_memory_text: str,
        new_memory_ids: list[str],
        bank_id: str,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        *,
        query_vector: list[float] | None = None,
        scope: str | None = None,
    ) -> ObservationConsolidationResult:
        """Integrate a new memory into the observations layer.

        Args:
            new_memory_text: The raw text of the newly retained memory (or
                the first chunk when a retain produces multiple chunks — the
                consolidator works at the retain-call level, not chunk level).
            new_memory_ids: IDs of the vector items stored in this retain call.
            bank_id: The memory bank being updated.
            vector_store: Backing store (must support ``search_similar`` and
                ``store_vectors``).
            llm_provider: LLM used for the consolidation call.
            query_vector: Pre-computed embedding of ``new_memory_text``.  When
                ``None``, the method embeds the text itself.  Pass the vector
                already computed during retain to avoid a redundant embedding
                call.
        """
        result = ObservationConsolidationResult()
        obs_scope = scope or observation_scope(bank_id)

        try:
            # 1. Embed the new memory (or reuse the caller's vector)
            if query_vector is None:
                vecs = await llm_provider.embed([new_memory_text])
                query_vector = vecs[0]

            # 2. Fetch semantically-related existing observations from the
            # dedicated observation bank (separate from the raw memory bank).
            from astrocyte.types import VectorFilters

            obs_bank = obs_bank_id(bank_id)
            existing_obs = await vector_store.search_similar(
                query_vector,
                obs_bank,
                limit=self.observation_recall_limit,
                filters=VectorFilters(bank_id=obs_bank),
            )

            # 3. Build and execute the LLM prompt
            user_prompt = _build_user_prompt(
                existing_obs,
                new_memory_text,
                max_obs=self.max_context_obs,
            )
            from astrocyte.types import Message

            completion = await llm_provider.complete(
                messages=[
                    Message(role="system", content=_SYSTEM_PROMPT),
                    Message(role="user", content=user_prompt),
                ],
                max_tokens=512,
                temperature=0.0,
            )

            # 4. Parse actions
            actions = _parse_actions(completion.text)
            if not actions:
                return result

            # Build a quick lookup: obs_id → VectorHit
            obs_by_id: dict[str, VectorHit] = {h.id: h for h in existing_obs}

            # 5. Apply actions
            now_iso = datetime.now(timezone.utc).isoformat()
            new_ids_json = json.dumps(new_memory_ids)

            for action in actions:
                act = action.get("action")
                try:
                    if act == "create":
                        r = await self._apply_create(
                            action, bank_id, vector_store, llm_provider,
                            source_ids=new_memory_ids,
                            now_iso=now_iso,
                            scope=obs_scope,
                        )
                        if r:
                            result.created += 1
                        else:
                            result.skipped += 1

                    elif act == "update":
                        obs_id = action.get("obs_id", "")
                        existing = obs_by_id.get(obs_id)
                        r = await self._apply_update(
                            action, existing, bank_id, vector_store, llm_provider,
                            new_source_ids=new_memory_ids,
                            now_iso=now_iso,
                            new_ids_json=new_ids_json,
                            scope=obs_scope,
                        )
                        if r:
                            result.updated += 1
                        else:
                            result.skipped += 1

                    elif act == "delete":
                        obs_id = action.get("obs_id", "")
                        if obs_id and obs_id in obs_by_id:
                            deleted = await vector_store.delete([obs_id], obs_bank)
                            if deleted:
                                result.deleted += 1
                        else:
                            result.skipped += 1

                except Exception as exc:
                    msg = f"action={act} failed: {exc}"
                    logger.warning("Observation consolidation %s in bank %s: %s", act, bank_id, exc)
                    result.errors.append(msg)

        except Exception as exc:
            logger.warning(
                "Observation consolidation failed for bank %s: %s",
                bank_id, exc, exc_info=True,
            )
            result.errors.append(str(exc))

        logger.debug(
            "Observation consolidation bank=%s created=%d updated=%d deleted=%d skipped=%d errors=%d",
            bank_id,
            result.created,
            result.updated,
            result.deleted,
            result.skipped,
            len(result.errors),
        )
        return result

    async def invalidate_sources(
        self,
        source_ids: list[str],
        bank_id: str,
        vector_store: VectorStore,
    ) -> int:
        """Delete observations whose provenance references any source ID."""
        if not source_ids:
            return 0
        source_set = set(source_ids)
        obs_bank = obs_bank_id(bank_id)
        deleted = 0
        offset = 0
        while True:
            page = await vector_store.list_vectors(obs_bank, offset=offset, limit=200)
            if not page:
                break
            to_delete: list[str] = []
            for item in page:
                metadata = item.metadata or {}
                try:
                    obs_sources = set(json.loads(str(metadata.get("_obs_source_ids", "[]"))))
                except (json.JSONDecodeError, TypeError):
                    obs_sources = set()
                if obs_sources & source_set:
                    to_delete.append(item.id)
            if to_delete:
                deleted += await vector_store.delete(to_delete, obs_bank)
            if len(page) < 200:
                break
            offset += len(page)
        return deleted

    async def _apply_create(
        self,
        action: dict[str, Any],
        bank_id: str,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        *,
        source_ids: list[str],
        now_iso: str,
        scope: str,
    ) -> bool:
        """Store a new observation."""
        text = (action.get("text") or "").strip()
        if not text:
            return False
        confidence = float(action.get("confidence", 0.7))
        if confidence < self.min_confidence:
            return False

        vecs = await llm_provider.embed([text])
        obs_id = uuid.uuid4().hex[:16]
        target_bank = obs_bank_id(bank_id)

        from astrocyte.types import VectorItem

        item = VectorItem(
            id=obs_id,
            bank_id=target_bank,
            vector=vecs[0],
            text=text,
            fact_type="observation",
            metadata={
                "_obs_proof_count": 1,
                "_obs_source_ids": json.dumps(source_ids),
                "_obs_confidence": str(round(confidence, 3)),
                "_obs_updated_at": now_iso,
                "_obs_scope": scope,
                "_obs_freshness": "fresh",
                "_created_at": now_iso,
            },
            retained_at=datetime.now(timezone.utc),
        )
        await vector_store.store_vectors([item])
        return True

    async def _apply_update(
        self,
        action: dict[str, Any],
        existing: VectorHit | None,
        bank_id: str,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        *,
        new_source_ids: list[str],
        now_iso: str,
        new_ids_json: str,
        scope: str,
    ) -> bool:
        """Delete old observation and store the revised version."""
        text = (action.get("text") or "").strip()
        if not text:
            return False
        confidence = float(action.get("confidence", 0.7))

        obs_id = action.get("obs_id", "")

        # Merge proof count and source IDs from the existing observation
        old_proof = 1
        old_sources: list[str] = []
        old_created_at = now_iso
        old_scope = scope
        if existing is not None:
            meta = existing.metadata or {}
            old_proof = int(meta.get("_obs_proof_count", 1))
            old_created_at = str(meta.get("_created_at", now_iso))
            old_scope = str(meta.get("_obs_scope", scope))
            try:
                old_sources = json.loads(str(meta.get("_obs_source_ids", "[]")))
            except (json.JSONDecodeError, TypeError):
                old_sources = []
            # Delete the stale observation from the obs bank
            target_bank = obs_bank_id(bank_id)
            await vector_store.delete([obs_id], target_bank)

        target_bank = obs_bank_id(bank_id)
        merged_sources = list({*old_sources, *new_source_ids})
        new_proof = old_proof + 1

        vecs = await llm_provider.embed([text])
        new_id = uuid.uuid4().hex[:16]

        from astrocyte.types import VectorItem

        item = VectorItem(
            id=new_id,
            bank_id=target_bank,
            vector=vecs[0],
            text=text,
            fact_type="observation",
            metadata={
                "_obs_proof_count": new_proof,
                "_obs_source_ids": json.dumps(merged_sources),
                "_obs_confidence": str(round(confidence, 3)),
                "_obs_updated_at": now_iso,
                "_obs_scope": old_scope,
                "_obs_freshness": "fresh",
                "_created_at": old_created_at,
            },
            retained_at=datetime.now(timezone.utc),
        )
        await vector_store.store_vectors([item])
        return True
