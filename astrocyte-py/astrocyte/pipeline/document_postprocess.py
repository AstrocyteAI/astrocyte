"""Document-level retain post-processing — core entry point.

After all per-section fact extraction completes for a document, run
zero-or-more document-wide passes:

  - **episodic_extract.tag_episodic_facts** — tag facts matching
    episodic-verb patterns with the ``EPISODIC_MARKER`` entity, so the
    recall path can surface them via ``search_facts_by_entity``.
  - **preference_compile.compile_preferences_for_document** — distill
    ``fact_type='preference'`` facts into ``MentalModel(kind='preference')``
    rows for advisory recall.
  - **directive_compile.compile_directives_for_document** — further
    distill preferences into ≤5 imperative directives stored as
    ``MentalModel(kind='directive')`` for hard-rule surface.

Each pass is gated by its config flag (``enabled: bool``). The function
is the single core call site any retain caller (bench harness today,
orchestrator hook tomorrow) uses to opt into these features.

Why a single function rather than 3 separate calls in each caller:

- Callers stay declarative: pass the config, get whatever passes are
  enabled. No re-implementing the gating logic per caller.
- New post-processors are added here; callers benefit automatically.
- The implicit pipeline order (tag → preference → directive) is
  encoded in one place so dependent passes (directive needs the
  preference compilation to have produced facts to read) are
  guaranteed to run in the right sequence.

Each pass is failure-isolated — a crash in one doesn't prevent the
others from running; the failure is logged and surfaced in the result.

Public API:
    run_document_postprocess(*, facts, store, mental_model_store,
                             provider, bank_id, document_id, config,
                             model, n_sessions=None)
        -> DocumentPostprocessResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte.config import AstrocyteConfig
    from astrocyte.types import PageIndexFact

_logger = logging.getLogger("astrocyte.pipeline.document_postprocess")


@dataclass
class DocumentPostprocessResult:
    """Summary of which passes ran + their outputs.

    ``ok`` is True iff every enabled pass completed without raising.
    Per-pass failures are recorded in ``failures`` (one entry per
    failing pass with name + error message).
    """

    episodic_tags_applied: int = 0
    preferences_compiled: int = 0
    directives_compiled: int = 0
    passes_run: list[str] = field(default_factory=list)
    passes_skipped: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


async def run_document_postprocess(
    *,
    facts: list[PageIndexFact],
    store: Any,
    mental_model_store: Any | None,
    provider: Any,
    bank_id: str | None,
    document_id: str,
    config: AstrocyteConfig,
    model: str | None = None,
    n_sessions: int | None = None,
) -> DocumentPostprocessResult:
    """Run document-level retain post-processing.

    Each pass is independently gated by its config flag. Order is fixed:
      1. ``config.episodic_extract.enabled``  → tag episodic facts in-place
      2. ``config.preference_compile.enabled`` → compile preference MentalModels
      3. ``config.directive_compile.enabled``  → compile directive MentalModels

    Order matters: tagging (in-place on facts) must happen BEFORE the
    caller persists facts so the EPISODIC_MARKER entity is included in
    ``save_facts``. The compile passes operate on the in-memory
    ``facts`` list directly (no store read), so they can run before or
    after the caller's save — but tag-then-save-then-compile is the
    expected lifecycle for retain callers.

    Args:
      facts: All extracted facts for the document. Tagged in-place
        when episodic_extract.enabled.
      store: PageIndexStore SPI handle.
      mental_model_store: Required when preference_compile or
        directive_compile are enabled; pass None when neither is.
      provider: LLM provider for compile passes. Required when either
        compile pass is enabled.
      bank_id: Bank scoping. Required when compile passes are enabled.
      document_id: The document being post-processed.
      config: AstrocyteConfig. The function reads its
        ``episodic_extract``, ``preference_compile`` (if exists),
        ``directive_compile`` sub-configs.
      model: LLM model for compile passes. Defaults to None (caller's
        provider default).
      n_sessions: Optional hint to directive_compile so it lowers its
        ≥2-mentions threshold for single-session docs.
    """
    result = DocumentPostprocessResult()

    # ─── 1. episodic_extract.tag_episodic_facts (in-place on facts list) ───
    if _is_enabled(config, "episodic_extract") and facts:
        try:
            from astrocyte.pipeline.episodic_extract import (  # noqa: PLC0415
                tag_episodic_facts,
            )

            tagged = tag_episodic_facts(facts)
            result.episodic_tags_applied = tagged
            result.passes_run.append("episodic_extract")
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "document_postprocess: episodic_extract failed doc=%s: %s",
                document_id, exc,
            )
            result.failures.append({"pass": "episodic_extract", "error": str(exc)})
    elif _is_enabled(config, "episodic_extract"):
        result.passes_skipped.append("episodic_extract (empty facts)")
    else:
        result.passes_skipped.append("episodic_extract (disabled)")

    # ─── 2. preference_compile.compile_preferences_for_document ───
    # (Operates on the in-memory ``facts`` list; no store read needed.)
    pref_enabled = _is_enabled_pref(config)
    if pref_enabled and mental_model_store is not None and provider is not None and bank_id:
        try:
            from astrocyte.pipeline.preference_compile import (  # noqa: PLC0415
                compile_preferences_for_document,
            )

            pref_ids = await compile_preferences_for_document(
                mental_model_store=mental_model_store,
                bank_id=bank_id,
                document_id=document_id,
                facts=facts,
                provider=provider,
                model=model,
            )
            result.preferences_compiled = len(pref_ids)
            result.passes_run.append("preference_compile")
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "document_postprocess: preference_compile failed doc=%s: %s",
                document_id, exc,
            )
            result.failures.append({"pass": "preference_compile", "error": str(exc)})
    elif pref_enabled:
        result.passes_skipped.append("preference_compile (missing deps)")
    else:
        result.passes_skipped.append("preference_compile (disabled)")

    # ─── 3. directive_compile.compile_directives_for_document ───
    if (
        _is_enabled(config, "directive_compile")
        and mental_model_store is not None
        and provider is not None
        and bank_id
        and facts
    ):
        try:
            from astrocyte.pipeline.directive_compile import (  # noqa: PLC0415
                compile_directives_for_document,
            )

            directive_ids = await compile_directives_for_document(
                mental_model_store=mental_model_store,
                bank_id=bank_id,
                document_id=document_id,
                facts=facts,
                provider=provider,
                model=model,
                n_sessions=n_sessions,
            )
            result.directives_compiled = len(directive_ids)
            result.passes_run.append("directive_compile")
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "document_postprocess: directive_compile failed doc=%s: %s",
                document_id, exc,
            )
            result.failures.append({"pass": "directive_compile", "error": str(exc)})
    elif _is_enabled(config, "directive_compile"):
        result.passes_skipped.append("directive_compile (missing deps)")
    else:
        result.passes_skipped.append("directive_compile (disabled)")

    return result


def _is_enabled(config: AstrocyteConfig, sub: str) -> bool:
    """Return True if ``config.<sub>.enabled`` is True. Defensive: returns False if missing."""
    sub_cfg = getattr(config, sub, None)
    if sub_cfg is None:
        return False
    return bool(getattr(sub_cfg, "enabled", False))


def _is_enabled_pref(config: AstrocyteConfig) -> bool:
    """Preference-compile gate — defaults to True if PreferenceCompileConfig
    doesn't exist yet (backward compat with current always-on bench behavior)."""
    sub_cfg = getattr(config, "preference_compile", None)
    if sub_cfg is None:
        return True
    return bool(getattr(sub_cfg, "enabled", True))
