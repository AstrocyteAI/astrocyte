"""M11: Entity resolution pipeline stage.

``EntityResolver.resolve()`` takes newly-extracted entities from a retain
call and, for each one, looks for existing candidates in the graph store
that might be the same entity under a different surface form.  Resolution
runs as a **tiered cascade** so the LLM is called only for genuinely
ambiguous pairs:

1. ``find_entity_candidates`` produces a list of candidates whose names
   pass the adapter's first-stage similarity filter.
2. **String-similarity tier** (this module): each (new, candidate) pair is
   scored with :func:`difflib.SequenceMatcher` ratio on lowercase-stripped
   names.
   - ``ratio >= autolink_threshold`` (default 0.95) — names are effectively
     identical (case differences, trailing whitespace, minor variants).
     Link is written **without** an LLM call. This is the common case for
     conversational benchmarks where the same person is mentioned by the
     same name across many sessions.
   - ``ratio < skip_threshold`` (default 0.6) — names are clearly distinct
     despite the candidate filter matching them. Skip without an LLM call.
   - Otherwise the pair is genuinely ambiguous and falls through to tier 3.
3. **LLM disambiguation tier**: only the pairs in the ambiguous middle
   band are sent to the LLM with a verbatim-evidence prompt.

This cuts LLM call volume by 95-99% on workloads with consistent naming
(LoCoMo, conversational memory benchmarks) while preserving full LLM
disambiguation on the genuinely hard cases.

Design goals
------------
- **Opt-in**: resolver is instantiated only when
  ``entity_resolution: enabled: true`` is in config.  A ``None`` resolver
  in the orchestrator means the stage is a no-op.
- **Bounded cost**: at most ``max_candidates_per_entity`` LLM calls per
  newly-extracted entity, controlled by the caller.
- **Testable**: all I/O goes through the ``GraphStore`` and ``LLMProvider``
  SPIs; ``InMemoryGraphStore`` + ``MockLLMProvider`` cover the full path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from astrocyte.types import Entity, EntityCandidateMatch, EntityLink, Message

if TYPE_CHECKING:
    from astrocyte.provider import GraphStore, LLMProvider

_logger = logging.getLogger("astrocyte.entity_resolution")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an entity resolution assistant.

You will be given:
- ENTITY A: a named entity extracted from a new piece of text.
- ENTITY B: a candidate entity already in the knowledge graph.
- SOURCE TEXT: the text that contained Entity A.

Your task: decide whether Entity A and Entity B refer to the same real-world
person, place, organisation, or concept.

Respond with ONLY valid JSON (no markdown, no preamble):

{
  "same_entity": true | false,
  "confidence": <float 0.0–1.0>,
  "evidence": "<verbatim quote from SOURCE TEXT that supports your decision>"
}

Rules:
- Set same_entity=true only when you are confident they refer to the same thing.
- confidence should reflect how certain you are (1.0 = certain, 0.5 = plausible).
- evidence must be a direct quote from SOURCE TEXT, not a paraphrase.
- If same_entity=false, set evidence to an empty string.\
"""


def _name_similarity(a: str, b: str) -> float:
    """Case-insensitive string-similarity ratio in ``[0.0, 1.0]``.

    Uses :class:`difflib.SequenceMatcher` over lowercase-stripped strings.
    Pure stdlib, microseconds per call. Used as the gate that decides
    whether a pair needs LLM disambiguation:

    - ``1.0`` — identical after normalisation
    - ``> 0.95`` — effectively the same name (e.g. ``"Alice"`` vs ``"Alice "``)
    - ``0.6 – 0.95`` — ambiguous (e.g. ``"Bob"`` vs ``"Robert"``)
    - ``< 0.6`` — clearly different (e.g. ``"Alice"`` vs ``"Charlie"``)
    """
    a_norm = (a or "").strip().lower()
    b_norm = (b or "").strip().lower()
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _build_user_message(entity_a: Entity, entity_b: Entity, source_text: str) -> str:
    aliases_a = f" (also known as: {', '.join(entity_a.aliases)})" if entity_a.aliases else ""
    aliases_b = f" (also known as: {', '.join(entity_b.aliases)})" if entity_b.aliases else ""
    return (
        f"ENTITY A: {entity_a.name}{aliases_a}\n"
        f"ENTITY B: {entity_b.name}{aliases_b}\n\n"
        f"SOURCE TEXT:\n{source_text}"
    )


# ---------------------------------------------------------------------------
# EntityResolver
# ---------------------------------------------------------------------------

class EntityResolver:
    """Identifies and persists alias-of links between entities (M11).

    Args:
        similarity_threshold: Minimum name-similarity required for a
            candidate to be sent to the LLM confirmation step.
            In the in-memory store this is a substring-match gate (0 or 1);
            in production adapters it is a cosine/edit-distance threshold.
        confirmation_threshold: Minimum ``confidence`` from the LLM for a
            link to be written to the graph.  Defaults to ``0.75``.
        max_candidates_per_entity: Hard cap on LLM calls per entity.
            Guards against runaway cost when the graph is large.
    """

    def __init__(
        self,
        *,
        similarity_threshold: float = 0.8,
        confirmation_threshold: float = 0.75,
        max_candidates_per_entity: int = 10,
        autolink_threshold: float = 0.95,
        skip_threshold: float = 0.5,
        embedding_autolink_threshold: float = 0.92,
        autolink_confidence: float = 0.95,
        trigram_threshold: float = 0.15,
        composite_threshold: float = 0.6,
        composite_name_weight: float = 0.5,
        composite_cooccurrence_weight: float = 0.3,
        composite_temporal_weight: float = 0.2,
        composite_temporal_window_days: float = 7.0,
        mention_count_bonus_cap: float = 0.05,
        mention_count_saturation: int = 50,
        enable_llm_disambiguation: bool = True,
        canonical_resolution: bool = False,
    ) -> None:
        """Args:
            similarity_threshold: Passed to ``find_entity_candidates`` —
                the adapter's first-stage filter (legacy path).
            confirmation_threshold: Minimum LLM-reported confidence for a
                disambiguated pair to be linked.
            max_candidates_per_entity: Hard cap on candidates evaluated per
                new entity. Default ``10`` — matches Hindsight's natural
                ``pg_trgm`` candidate count. The cascade's cheap tiers
                evaluate all of them; only the genuinely-ambiguous middle
                band reaches the LLM.
            autolink_threshold: Trigram name-similarity at or above which a
                pair is auto-linked **without** calling the LLM. Default
                ``0.95`` — effectively identical names.
            skip_threshold: Combined-score (max of name and embedding
                similarity) below which a candidate is dropped **without**
                an LLM call. Default ``0.5``.
            embedding_autolink_threshold: Cosine-similarity at or above
                which the embedding tier autolinks the pair. Catches
                semantic aliases (``"Bob"`` ↔ ``"Robert"``) that string
                similarity alone misses. Default ``0.92``.
            autolink_confidence: Confidence value persisted on alias-of
                links created via the autolink fast paths. Default ``0.95``.
            trigram_threshold: Adapter-side prefilter passed to
                ``find_entity_candidates_scored``. Candidates with name
                similarity below this are dropped before being returned.
                Default ``0.15`` matches Hindsight's empirically-tuned
                threshold — catches substring relationships while staying
                fully index-based.
            composite_threshold: Score at or above which the Hindsight-style
                composite tier autolinks. Composite =
                ``name_weight * name_sim + cooccurrence_weight * overlap +
                temporal_weight * decay``. Default ``0.6`` matches Hindsight.
            composite_name_weight: Weight applied to ``name_similarity`` in
                the composite score. Default ``0.5``.
            composite_cooccurrence_weight: Weight applied to the Jaccard-
                style overlap between the new entity's nearby-entity names
                and the candidate's stored co-occurring entity names.
                Default ``0.3``.
            composite_temporal_weight: Weight applied to a temporal-decay
                signal — ``max(0, 1 - days_diff / temporal_window_days)``.
                Default ``0.2``.
            composite_temporal_window_days: Days over which the temporal
                decay falls from 1.0 to 0.0. Default ``7.0``.
            mention_count_bonus_cap: Maximum additive bonus the
                ``mention_count`` popularity signal contributes to the
                composite score. Hindsight-parity tiebreaker — capped at
                ``0.05`` so a popular candidate breaks ties between
                near-identical composite scores without ever overriding
                the primary name/cooccurrence/temporal signals on its
                own. Set to ``0.0`` to disable.
            mention_count_saturation: Mention count at which the bonus
                reaches its cap. Bonus scales as
                ``cap * log1p(count) / log1p(saturation)``. Default
                ``50`` — popular entities saturate quickly so the signal
                stops growing once an entity is well-established.
            enable_llm_disambiguation: When ``True`` (default for backward
                compat), ambiguous middle-band pairs (composite below
                threshold, name/embedding combined above ``skip_threshold``)
                escalate to an LLM call for final disambiguation. Set to
                ``False`` to match Hindsight's LLM-free design — ambiguous
                pairs are simply not linked. For LoCoMo-scale benchmarks
                the cheap tiers (trigram, embedding, composite) handle the
                common cases; the LLM tier mostly fires on hard cases that
                don't move benchmark scores and adds significant retain
                latency.
            canonical_resolution: When ``True`` (Hindsight-style
                lookup-then-decide), :meth:`resolve_canonical_ids_in_place`
                rewrites each new entity's tentative ID to its existing
                canonical's ID before storage — so different surface forms
                that resolve to the same canonical never produce duplicate
                entity rows. The post-storage :meth:`resolve` aliasing
                pass becomes redundant in this mode. Default ``False``
                preserves the legacy two-stage flow (store-by-ID then
                resolve aliases). Orchestrator decides which path to run
                based on this flag.
        """
        if not (0.0 <= skip_threshold <= autolink_threshold <= 1.0):
            raise ValueError(
                "Expected 0.0 <= skip_threshold <= autolink_threshold <= 1.0, "
                f"got skip={skip_threshold}, autolink={autolink_threshold}"
            )
        if not (0.0 <= embedding_autolink_threshold <= 1.0):
            raise ValueError(
                "Expected 0.0 <= embedding_autolink_threshold <= 1.0, "
                f"got {embedding_autolink_threshold}"
            )
        if composite_temporal_window_days <= 0.0:
            raise ValueError(
                "composite_temporal_window_days must be > 0, "
                f"got {composite_temporal_window_days}"
            )
        if mention_count_bonus_cap < 0.0:
            raise ValueError(
                "mention_count_bonus_cap must be >= 0, "
                f"got {mention_count_bonus_cap}"
            )
        if mention_count_saturation <= 0:
            raise ValueError(
                "mention_count_saturation must be > 0, "
                f"got {mention_count_saturation}"
            )
        self.similarity_threshold = similarity_threshold
        self.confirmation_threshold = confirmation_threshold
        self.max_candidates_per_entity = max_candidates_per_entity
        self.autolink_threshold = autolink_threshold
        self.skip_threshold = skip_threshold
        self.embedding_autolink_threshold = embedding_autolink_threshold
        self.autolink_confidence = autolink_confidence
        self.trigram_threshold = trigram_threshold
        self.composite_threshold = composite_threshold
        self.composite_name_weight = composite_name_weight
        self.composite_cooccurrence_weight = composite_cooccurrence_weight
        self.composite_temporal_weight = composite_temporal_weight
        self.composite_temporal_window_days = composite_temporal_window_days
        self.mention_count_bonus_cap = mention_count_bonus_cap
        self.mention_count_saturation = mention_count_saturation
        self.enable_llm_disambiguation = enable_llm_disambiguation
        self.canonical_resolution = canonical_resolution
        # Optional benchmark collector — installed by the eval harness via
        # ``resolver.metrics_collector = collector``. Hot-path code probes
        # with ``getattr`` and silently no-ops when absent.
        self.metrics_collector: object | None = None

    async def resolve(
        self,
        new_entities: list[Entity],
        source_text: str,
        bank_id: str,
        graph_store: GraphStore,
        llm_provider: LLMProvider,
        *,
        event_date: datetime | None = None,
    ) -> list[EntityLink]:
        """Run the Hindsight-inspired entity-resolution cascade.

        Three tiers run in order, escalating only when the cheaper signal
        is inconclusive:

        1. **Trigram name similarity** (``find_entity_candidates_scored``).
           Adapter computes ``pg_trgm.similarity()`` (PostgreSQL) or
           :class:`difflib.SequenceMatcher` (in-memory) at indexed speed.
        2. **Embedding cosine similarity** — when the new entity carries
           an ``embedding`` and the candidate has one stored, the cosine
           score discriminates semantic aliases (``"Bob"`` / ``"Robert"``)
           that trigram similarity misses.
        3. **LLM disambiguation** — only fires for the genuinely ambiguous
           middle band where neither cheap signal is decisive.

        Per-candidate decision logic:

        - ``name_similarity >= autolink_threshold`` → autolink (no LLM)
        - ``embedding_similarity >= embedding_autolink_threshold`` → autolink
        - ``max(name, embedding) < skip_threshold`` → drop (no LLM)
        - otherwise → fall through to LLM disambiguation

        Adapters that haven't implemented ``find_entity_candidates_scored``
        yet (it's optional in the SPI) gracefully fall back to the legacy
        ``find_entity_candidates`` + Python-level scoring path.

        Returns:
            All ``EntityLink`` objects that were written to the graph store.

        Concurrency: each new entity's resolution work — candidate fetch,
        autolink decisions, LLM disambiguation — runs in parallel via
        :func:`asyncio.gather`. Within an entity, each candidate's tier
        decision runs concurrently. This brings retain-time entity
        resolution from sequential ``O(N×K)`` round-trips to a single
        async-batched pass.

        ``event_date`` (when supplied) feeds the composite tier's temporal
        signal — recent candidates score higher for the same name. Defaults
        to "now" if not provided. Pass the retain request's ``occurred_at``
        for benchmark workloads with simulated event timestamps.
        """
        if not new_entities:
            return []

        # Pre-compute the set of nearby entity names — every other new
        # entity in this batch is a "nearby" candidate for cooccurrence
        # overlap with each candidate's existing co_occurs links.
        nearby_names: set[str] = {
            (e.name or "").strip().lower()
            for e in new_entities
            if e.name and e.name.strip()
        }
        effective_event_date = event_date or datetime.now(timezone.utc)

        per_entity = await asyncio.gather(*[
            self._resolve_one_entity(
                entity, source_text, bank_id, graph_store, llm_provider,
                nearby_names=nearby_names - {(entity.name or "").strip().lower()},
                event_date=effective_event_date,
            )
            for entity in new_entities
        ])
        return [link for sublist in per_entity for link in sublist]

    async def resolve_canonical_ids_in_place(
        self,
        new_entities: list[Entity],
        bank_id: str,
        graph_store: GraphStore,
        *,
        event_date: datetime | None = None,
    ) -> dict[str, str]:
        """Hindsight-style lookup-then-decide canonical resolution.

        For each new entity, runs the cascade against existing canonicals
        and — if any tier crosses its threshold — **rewrites the entity's
        ID to the matched canonical's ID** in place. Entities that don't
        match keep their tentative ID and will be inserted as new
        canonicals at storage time.

        Returns a mapping ``{tentative_id: canonical_id}`` for callers
        that need to update co-occurrence links or memory associations
        that reference the original tentative IDs.

        Designed to run BEFORE ``store_entities``. Replaces the post-store
        :meth:`resolve` alias-creation pass when
        ``canonical_resolution=True`` is set on the resolver.

        No alias-of links are created — different surface forms that
        resolve to the same canonical simply share the canonical ID.
        """
        if not new_entities:
            return {}

        nearby_names: set[str] = {
            (e.name or "").strip().lower()
            for e in new_entities
            if e.name and e.name.strip()
        }
        effective_event_date = event_date or datetime.now(timezone.utc)
        id_remapping: dict[str, str] = {}

        # Process all entities in parallel — independent lookups against
        # existing canonicals. Each task returns the resolved canonical ID
        # (or the original tentative ID if no match).
        resolutions = await asyncio.gather(*[
            self._best_canonical_id(
                entity, bank_id, graph_store,
                nearby_names=nearby_names - {(entity.name or "").strip().lower()},
                event_date=effective_event_date,
            )
            for entity in new_entities
        ])
        for entity, canonical_id in zip(new_entities, resolutions, strict=True):
            tentative_id = entity.id
            if canonical_id != tentative_id:
                entity.id = canonical_id
                id_remapping[tentative_id] = canonical_id

        # Hindsight-parity: bump mention_count on every canonical we
        # resolved-to so the popularity signal stays current. Best-effort
        # (older adapters lacking the method are skipped). Run AFTER the
        # remap so we count the resolution event, not the lookup.
        if id_remapping:
            increment = getattr(graph_store, "increment_mention_counts", None)
            if increment is not None:
                try:
                    await increment(list(set(id_remapping.values())), bank_id)
                except Exception as exc:  # pragma: no cover — best-effort
                    _logger.warning(
                        "increment_mention_counts failed (%s) — composite "
                        "scoring will use stale popularity signal until next "
                        "successful resolve.", exc,
                    )
        return id_remapping

    async def _best_canonical_id(
        self,
        entity: Entity,
        bank_id: str,
        graph_store: GraphStore,
        *,
        nearby_names: set[str],
        event_date: datetime,
    ) -> str:
        """Return the canonical entity ID for *entity*.

        Looks up candidates, scores each via the same tier signals as
        :meth:`resolve`, and returns the best match's ID when the
        strongest signal crosses the corresponding tier's threshold. Falls
        back to the entity's tentative ID when no candidate matches.
        """
        scored = await self._score_candidates(entity, bank_id, graph_store)
        if not scored:
            self._record_cascade("no_match")
            return entity.id

        best_match: EntityCandidateMatch | None = None
        best_score = 0.0
        best_decision: str | None = None
        for match in scored[: self.max_candidates_per_entity]:
            if match.entity.id == entity.id:
                # Same-name same-ID candidate (deterministic-ID path) —
                # already canonical, no remapping needed.
                self._record_cascade("already_canonical")
                return entity.id
            score, decision = self._resolution_score_with_tier(
                match, nearby_names, event_date,
            )
            if score > best_score:
                best_score = score
                best_match = match
                best_decision = decision

        if best_match is not None and best_score > 0.0:
            composite = self._composite_score(
                best_match.name_similarity, best_match, nearby_names, event_date,
            )
            self._record_cascade(best_decision or "composite_autolink", composite_score=composite)
            self._record_canonical_resolution(resolved=True)
            return best_match.entity.id
        self._record_cascade("no_match")
        self._record_canonical_resolution(resolved=False)
        return entity.id

    def _resolution_score(
        self,
        match: EntityCandidateMatch,
        nearby_names: set[str],
        event_date: datetime,
    ) -> float:
        """Unified score across the cascade tiers (legacy entry point)."""
        score, _ = self._resolution_score_with_tier(match, nearby_names, event_date)
        return score

    def _resolution_score_with_tier(
        self,
        match: EntityCandidateMatch,
        nearby_names: set[str],
        event_date: datetime,
    ) -> tuple[float, str | None]:
        """Tier-aware unified score.

        Returns ``(score, tier_label)`` where tier_label is one of
        ``"trigram_autolink"``, ``"embedding_autolink"``,
        ``"composite_autolink"`` (matching the names recorded by the
        benchmark metrics collector), or ``None`` when no tier qualifies.
        """
        name_sim = match.name_similarity
        emb_sim = match.embedding_similarity or 0.0
        composite = self._composite_score(name_sim, match, nearby_names, event_date)

        candidates: list[tuple[float, str]] = []
        if name_sim >= self.autolink_threshold:
            candidates.append((name_sim, "trigram_autolink"))
        if emb_sim >= self.embedding_autolink_threshold:
            candidates.append((emb_sim, "embedding_autolink"))
        if composite >= self.composite_threshold:
            candidates.append((composite, "composite_autolink"))

        if not candidates:
            return 0.0, None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]

    def _record_cascade(self, decision: str, *, composite_score: float | None = None) -> None:
        """Forward decision to the optional metrics collector. No-op when
        no collector is attached. Caller-side error suppression — metrics
        observability must never break a real call."""
        collector = self.metrics_collector
        if collector is None:
            return
        try:
            collector.record_cascade_decision(decision, composite_score=composite_score)
        except Exception:
            pass  # metrics are best-effort; never let collection failures disrupt the pipeline

    def _record_canonical_resolution(self, *, resolved: bool) -> None:
        collector = self.metrics_collector
        if collector is None:
            return
        try:
            collector.record_canonical_resolution(resolved=resolved)
        except Exception:
            pass  # metrics are best-effort; never let collection failures disrupt the pipeline

    async def _resolve_one_entity(
        self,
        entity: Entity,
        source_text: str,
        bank_id: str,
        graph_store: GraphStore,
        llm_provider: LLMProvider,
        *,
        nearby_names: set[str],
        event_date: datetime,
    ) -> list[EntityLink]:
        """Cascade for a single new entity. Candidates are processed concurrently."""
        scored = await self._score_candidates(entity, bank_id, graph_store)
        if not scored:
            return []

        eligible = [
            match
            for match in scored[: self.max_candidates_per_entity]
            if match.entity.id != entity.id
        ]
        if not eligible:
            return []

        results = await asyncio.gather(*[
            self._handle_candidate(
                entity, match,
                source_text, bank_id, graph_store, llm_provider,
                nearby_names=nearby_names,
                event_date=event_date,
            )
            for match in eligible
        ])
        return [link for link in results if link is not None]

    async def _handle_candidate(
        self,
        entity: Entity,
        match: EntityCandidateMatch,
        source_text: str,
        bank_id: str,
        graph_store: GraphStore,
        llm_provider: LLMProvider,
        *,
        nearby_names: set[str],
        event_date: datetime,
    ) -> EntityLink | None:
        """Route a single (entity, candidate) pair through the cascade tiers."""
        candidate = match.entity
        name_sim = match.name_similarity
        emb_sim = match.embedding_similarity

        # Tier A: trigram autolink — names are effectively identical.
        if name_sim >= self.autolink_threshold:
            self._record_cascade("trigram_autolink")
            return await self._autolink(
                entity, candidate, name_sim, "trigram",
                bank_id, graph_store,
            )

        # Tier B: embedding autolink — semantic alias (Bob/Robert).
        if emb_sim is not None and emb_sim >= self.embedding_autolink_threshold:
            self._record_cascade("embedding_autolink")
            return await self._autolink(
                entity, candidate, emb_sim, "embedding",
                bank_id, graph_store,
            )

        # Tier C: Hindsight-style composite — combine name similarity with
        # cooccurrence-overlap and temporal proximity. Catches cases where
        # name alone is borderline (e.g. ``"Bob"`` vs ``"Robert"``) but the
        # broader context (same friends mentioned, recent activity) gives
        # the resolver enough evidence to autolink without an LLM.
        composite = self._composite_score(
            name_sim, match, nearby_names, event_date,
        )
        if composite >= self.composite_threshold:
            self._record_cascade("composite_autolink", composite_score=composite)
            return await self._autolink(
                entity, candidate, composite, "composite",
                bank_id, graph_store,
            )

        # Combined-signal skip: cheap signals say "no" — embedding has been
        # consulted (when available) and the composite didn't cross the
        # threshold either. Drop without an LLM.
        combined = max(name_sim, emb_sim or 0.0)
        if combined < self.skip_threshold:
            _logger.debug(
                "entity resolution: skipping %r vs %r "
                "(name_sim=%.2f, emb_sim=%s, composite=%.2f, max < %.2f)",
                entity.name, candidate.name,
                name_sim,
                f"{emb_sim:.2f}" if emb_sim is not None else "n/a",
                composite,
                self.skip_threshold,
            )
            self._record_cascade("skipped", composite_score=composite)
            return None

        # Tier D: ambiguous middle band — escalate to LLM (or skip when
        # ``enable_llm_disambiguation`` is False, matching Hindsight's
        # LLM-free design).
        if not self.enable_llm_disambiguation:
            _logger.debug(
                "entity resolution: skipping ambiguous %r vs %r "
                "(LLM tier disabled, name_sim=%.2f, composite=%.2f)",
                entity.name, candidate.name, name_sim, composite,
            )
            self._record_cascade("skipped_no_llm", composite_score=composite)
            return None
        self._record_cascade("llm_disambiguation", composite_score=composite)
        return await self._confirm_and_link(
            entity, candidate, source_text, bank_id, graph_store, llm_provider,
        )

    def _composite_score(
        self,
        name_sim: float,
        match: EntityCandidateMatch,
        nearby_names: set[str],
        event_date: datetime,
    ) -> float:
        """Weighted-sum signal blending name + cooccurrence + temporal.

        Mirrors Hindsight's retain-time scoring (paper §4.1.3). Each
        component is in ``[0, 1]``; weights sum to 1.0 so the composite
        is also in ``[0, 1]``. Missing inputs (no nearby names, no
        ``last_seen``) contribute ``0`` for that signal — the composite
        remains well-defined even when the cooccurrence/temporal data
        isn't populated yet.
        """
        # Cooccurrence overlap: Jaccard-style intersection of nearby names
        # with the candidate's stored co-occurring entity names. Both sides
        # are lowercased and stripped on the way in.
        cooc_score = 0.0
        if nearby_names and match.co_occurring_names:
            candidate_names = {n for n in match.co_occurring_names if n}
            if candidate_names:
                overlap = len(nearby_names & candidate_names)
                # Normalise by the smaller side to keep the signal in [0,1]
                # without penalising large candidate co-occurrence sets.
                cooc_score = overlap / min(len(nearby_names), len(candidate_names))

        # Temporal decay: 1.0 if seen today, 0.0 once outside the window.
        temporal_score = 0.0
        if match.last_seen is not None:
            last_seen = match.last_seen
            now = event_date
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            days_diff = abs((now - last_seen).total_seconds() / 86400.0)
            if days_diff < self.composite_temporal_window_days:
                temporal_score = 1.0 - (days_diff / self.composite_temporal_window_days)

        base = (
            self.composite_name_weight * name_sim
            + self.composite_cooccurrence_weight * cooc_score
            + self.composite_temporal_weight * temporal_score
        )

        # Hindsight-parity popularity bonus — log-scaled so a 10-mention
        # entity scores roughly half as much as a 50-mention one, and a
        # 100-mention entity is fully saturated. Hard-capped at
        # ``mention_count_bonus_cap`` so this signal can never on its own
        # promote a candidate past the composite threshold; it only
        # breaks ties between near-equal name/cooccurrence/temporal blends.
        if self.mention_count_bonus_cap > 0.0 and match.mention_count > 1:
            import math
            saturation = max(1, self.mention_count_saturation)
            bonus = self.mention_count_bonus_cap * min(
                1.0,
                math.log1p(match.mention_count) / math.log1p(saturation),
            )
            base += bonus

        return base

    async def _score_candidates(
        self,
        entity: Entity,
        bank_id: str,
        graph_store: GraphStore,
    ) -> list[EntityCandidateMatch]:
        """Fetch candidates from the graph store with all signals populated.

        Tries the new ``find_entity_candidates_scored`` first (returns
        :class:`EntityCandidateMatch` instances with name / embedding /
        cooccurrence / temporal signals). Falls back to the legacy
        ``find_entity_candidates`` plus Python-level string scoring when
        the adapter hasn't implemented the scored variant — old adapters
        keep working, they just lose the embedding, cooccurrence, and
        temporal tiers.
        """
        scored_method = getattr(graph_store, "find_entity_candidates_scored", None)
        if scored_method is not None:
            try:
                matches = await scored_method(
                    entity.name,
                    bank_id,
                    name_embedding=entity.embedding,
                    trigram_threshold=self.trigram_threshold,
                    limit=self.max_candidates_per_entity + 5,
                )
            except Exception as exc:
                _logger.warning(
                    "find_entity_candidates_scored failed for %r: %s — "
                    "falling back to legacy path", entity.name, exc,
                )
                matches = None
            if matches is not None:
                return list(matches)

        # Legacy fallback: adapter only implements find_entity_candidates.
        try:
            candidates = await graph_store.find_entity_candidates(
                entity.name,
                bank_id,
                threshold=self.similarity_threshold,
                limit=self.max_candidates_per_entity + 5,
            )
        except Exception as exc:
            _logger.warning("find_entity_candidates failed for %r: %s", entity.name, exc)
            return []

        return [
            EntityCandidateMatch(
                entity=c,
                name_similarity=_name_similarity(entity.name, c.name),
                embedding_similarity=None,
            )
            for c in candidates
        ]

    async def _autolink(
        self,
        entity_a: Entity,
        entity_b: Entity,
        score: float,
        tier: str,
        bank_id: str,
        graph_store: GraphStore,
    ) -> EntityLink | None:
        """Persist an alias-of link without LLM confirmation.

        Used when one of the cheap signals (trigram or embedding) is high
        enough that LLM consultation would be wasted spend. The persisted
        ``evidence`` records both the tier and the score so audits can
        distinguish trigram autolinks, embedding autolinks, and
        LLM-confirmed links from one another.
        """
        link = EntityLink(
            entity_a=entity_a.id,
            entity_b=entity_b.id,
            link_type="alias_of",
            evidence=f"{tier} similarity {score:.2f}",
            confidence=self.autolink_confidence,
            created_at=datetime.now(timezone.utc),
        )
        try:
            await graph_store.store_entity_link(link, bank_id)
        except Exception as exc:
            _logger.warning(
                "store_entity_link (autolink/%s) failed for %r <-> %r: %s",
                tier, entity_a.name, entity_b.name, exc,
            )
            return None

        _logger.debug(
            "entity resolution: autolinked (%s) %r (%s) alias_of %r (%s) score=%.2f",
            tier,
            entity_a.name, entity_a.id, entity_b.name, entity_b.id, score,
        )
        return link

    async def _confirm_and_link(
        self,
        entity_a: Entity,
        entity_b: Entity,
        source_text: str,
        bank_id: str,
        graph_store: GraphStore,
        llm_provider: LLMProvider,
    ) -> EntityLink | None:
        """Ask the LLM to confirm whether two entities are aliases, then store the link."""
        import json

        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=_build_user_message(entity_a, entity_b, source_text)),
        ]

        try:
            completion = await llm_provider.complete(messages, max_tokens=256, temperature=0.0)
            raw = (completion.text or "").strip()
        except Exception as exc:
            _logger.warning(
                "LLM confirmation failed for %r <-> %r: %s", entity_a.name, entity_b.name, exc
            )
            return None

        # Strip markdown fences
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            _logger.warning(
                "entity resolution LLM returned non-JSON for %r <-> %r: %r",
                entity_a.name, entity_b.name, raw[:120],
            )
            return None

        same = bool(data.get("same_entity", False))
        if not same:
            return None

        confidence_raw = data.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            confidence = 0.0

        if confidence < self.confirmation_threshold:
            _logger.debug(
                "entity resolution: %r <-> %r confidence %.2f below threshold %.2f — skipped",
                entity_a.name, entity_b.name, confidence, self.confirmation_threshold,
            )
            return None

        evidence = str(data.get("evidence", ""))

        link = EntityLink(
            entity_a=entity_a.id,
            entity_b=entity_b.id,
            link_type="alias_of",
            evidence=evidence,
            confidence=confidence,
            created_at=datetime.now(timezone.utc),
        )

        try:
            await graph_store.store_entity_link(link, bank_id)
        except Exception as exc:
            _logger.warning(
                "store_entity_link failed for %r <-> %r: %s", entity_a.name, entity_b.name, exc
            )
            return None

        _logger.info(
            "entity resolution: linked %r (%s) alias_of %r (%s) confidence=%.2f",
            entity_a.name, entity_a.id, entity_b.name, entity_b.id, confidence,
        )
        return link
