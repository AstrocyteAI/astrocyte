"""M39 — append per-question-type focused prompts to the upstream
answerer prompt.

Slim alternative to M22 (``_hindsight_answerer.maybe_install_hindsight_answerer_patch``)
which replaces the *entire* upstream answerer prompt with a Hindsight-
ported version. M39 reuses just the per-category focused blocks (which
ARE ours — M19a + M31d Fix E) and APPENDS them to the standard
upstream prompt without replacing it.

Why a separate patch
--------------------

M22's wholesale prompt replacement borrows Hindsight's directive
prompt verbatim. The user pushed back on that as "borrowing rather
than improving." But the per-category blocks (single-session-user,
single-session-preference, multi-session, knowledge-update,
temporal-reasoning, single-session-assistant) are OUR tuned
instructions — written based on Astrocyte's bench failures, not
ported from Hindsight. Extracting those into a thin append-only patch
lets v0.15.0 ship them without inheriting the controversial base
prompt port.

Gated by ASTROCYTE_M39_QTYPE_PROMPTS=1. Default OFF.

Reference
---------

- ``scripts/mem0_harness/_hindsight_answerer.py:_QTYPE_FOCUSED_PROMPTS``
  — the source of truth for the per-category blocks.
- ``docs/_design/m31-lme-quality.md`` §7 Fix E — temporal-reasoning
  block rationale.
- ``docs/_design/m19a-per-qtype-routing.md`` — the original cycle that
  introduced per-Q-type prompt routing for SSP / SSU / SSA / MS / KU.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_logger = logging.getLogger("astrocyte.mem0_harness.qtype_prompts")


def is_enabled() -> bool:
    """``ASTROCYTE_M39_QTYPE_PROMPTS`` set to truthy."""
    return os.environ.get("ASTROCYTE_M39_QTYPE_PROMPTS", "").lower() in ("1", "true", "yes")


def maybe_install_qtype_prompts_patch(bench: str) -> bool:
    """Wrap the upstream ``get_answer_generation_prompt`` to append a
    per-category focused block at the END of the standard prompt.

    Args:
        bench: ``"longmemeval"`` or ``"locomo"`` — determines which
            upstream prompts module to patch.

    Returns:
        True if patch installed. False when disabled, double-install,
        or the upstream module isn't importable.
    """
    if not is_enabled():
        return False

    if bench == "longmemeval":
        prompts_module_path = "benchmarks.longmemeval.prompts"
    elif bench == "locomo":
        prompts_module_path = "benchmarks.locomo.prompts"
    else:
        raise ValueError(f"unknown bench: {bench!r}")

    try:
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(prompts_module_path)
    except ImportError as exc:
        _logger.warning(
            "qtype_prompts patch: cannot import %s: %s",
            prompts_module_path, exc,
        )
        return False

    if getattr(mod, "_ASTROCYTE_M39_INSTALLED", False):
        return False

    # Reuse the per-category dict from _hindsight_answerer (our blocks,
    # M19a + M31d Fix E). Imported lazily so this patch module doesn't
    # require M22 to be loadable at all.
    from scripts.mem0_harness._hindsight_answerer import (  # noqa: PLC0415
        _qtype_focused_prompt,
    )

    _original_get_answer = mod.get_answer_generation_prompt

    if bench == "longmemeval":
        # LME signature: (question, search_results, *, question_date, user_profile)
        def _wrapped(
            question: str,
            search_results: list,
            *,
            question_date: str = "",
            user_profile: dict | None = None,
            **kwargs: Any,
        ) -> str:
            base = _original_get_answer(
                question=question,
                search_results=search_results,
                question_date=question_date,
                user_profile=user_profile,
                **kwargs,
            )
            # Question type comes from the bench harness's question dict;
            # the upstream prompt builder doesn't carry it, so we infer
            # from the LME question_id pattern stored elsewhere isn't
            # available at this layer. Instead, fall back to a generic
            # block when type is unknown — the framework's
            # process_question_answerer DOES pass question_type but the
            # signature doesn't expose it. Future improvement: thread
            # question_type through the call site.
            qtype = kwargs.get("question_type") or _detect_qtype_from_context(question)
            block = _qtype_focused_prompt(qtype)
            if not block:
                return base
            return f"{base}\n\n--- Per-category instructions ---\n{block}"
    else:
        # LoCoMo signature: (question, sliced, *, reference_date, user_profile)
        def _wrapped(
            question: str,
            sliced: list,
            *,
            reference_date: str | None = None,
            user_profile: dict | None = None,
            **kwargs: Any,
        ) -> str:
            base = _original_get_answer(
                question=question,
                sliced=sliced,
                reference_date=reference_date,
                user_profile=user_profile,
                **kwargs,
            )
            qtype = kwargs.get("category") or _detect_qtype_from_context(question)
            block = _qtype_focused_prompt(qtype)
            if not block:
                return base
            return f"{base}\n\n--- Per-category instructions ---\n{block}"

    mod.get_answer_generation_prompt = _wrapped
    mod._ASTROCYTE_M39_INSTALLED = True

    print(
        f"[m39-qtype-prompts] ASTROCYTE_M39_QTYPE_PROMPTS=1 — patched "
        f"{prompts_module_path}.get_answer_generation_prompt to append "
        f"per-category focused blocks (M19a + M31d Fix E).",
        file=sys.stderr,
    )
    return True


# ---------------------------------------------------------------------------
# Question-type detection (cheap regex, no LLM)
# ---------------------------------------------------------------------------
#
# When the upstream prompt builder doesn't pass question_type explicitly
# (which is the common case — LME's process_question_answerer has it
# but the prompt-builder signature drops it), infer from the question
# text. Cheap regex; not perfect but good enough to route on the
# obvious shapes.


import re


_QTYPE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # temporal-reasoning — "how many weeks ago", "how long since",
    # "when did I", "before or after", "which came first"
    (re.compile(r"\bhow\s+(?:many|long)\s+(?:weeks?|days?|months?|years?|hours?)\b", re.I), "temporal-reasoning"),
    (re.compile(r"\bhow\s+long\b", re.I), "temporal-reasoning"),
    (re.compile(r"\bwhen\s+(?:did|do|does|have)\s+(?:i|we|you)\b", re.I), "temporal-reasoning"),
    (re.compile(r"\b(?:before|after)\s+or\s+(?:after|before)\b", re.I), "temporal-reasoning"),
    (re.compile(r"\bwhich\s+\w+\s+(?:came|happened|was)\s+(first|last|earliest|latest)\b", re.I), "temporal-reasoning"),
    # single-session-assistant — "what did (you|the assistant) say/recommend".
    # Listed BEFORE SSP so the "recommend" verb routes to assistant when
    # the subject is the assistant, not the user.
    (re.compile(r"\bwhat\s+did\s+(?:you|the\s+assistant)\s+(?:say|recommend|suggest|tell)\b", re.I), "single-session-assistant"),
    # single-session-preference — "what do I prefer", "what's my favourite"
    (re.compile(r"\b(?:what\s+(?:do|did|does)\s+(?:i|you)\s+(?:prefer|like|love|enjoy))\b", re.I), "single-session-preference"),
    (re.compile(r"\b(?:what(?:\'s|\s+is)\s+my\s+(?:favourite|favorite|preferred))\b", re.I), "single-session-preference"),
    # "recommend (something) (for|to) me" — user-side recommendation request
    (re.compile(r"\brecommend\b.*\bfor\s+me\b", re.I), "single-session-preference"),
    # multi-session — "across", "throughout", "in our (chats|conversations)"
    (re.compile(r"\bacross\s+(?:our\s+)?(?:chats?|conversations?|sessions?)\b", re.I), "multi-session"),
    (re.compile(r"\bthroughout\s+(?:our\s+)?(?:chats?|conversations?|history)\b", re.I), "multi-session"),
    (re.compile(r"\bhave\s+(?:we|i)\s+(?:ever|always|usually)\b", re.I), "multi-session"),
    # knowledge-update — "current", "now", "latest", state-change verbs
    (re.compile(r"\b(?:current|currently|now|latest|new)\b.{0,40}\b(?:job|role|location|address|status|relationship|name)\b", re.I), "knowledge-update"),
)


def _detect_qtype_from_context(question: str) -> str | None:
    """Lightweight regex-based qtype detection.

    Returns the first matching question_type, or None when no pattern
    matches (caller falls back to no focused block).
    """
    if not question:
        return None
    for pat, qtype in _QTYPE_PATTERNS:
        if pat.search(question):
            return qtype
    return None


__all__ = [
    "is_enabled",
    "maybe_install_qtype_prompts_patch",
]
