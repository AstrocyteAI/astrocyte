"""LoCoMo bench runner via Mem0's harness, but driving AstrocyteClient.

M13.1: minimal-fork strategy — add the upstream ``memory-benchmarks``
repo to ``sys.path``, monkey-patch ``Mem0Client`` → ``AstrocyteClient``
in the runner module, then invoke their ``main()`` unchanged.

This gives us the cleanest apples-to-apples comparison against Mem0's
own re-runnable numbers: same dataset loader, same per-cutoff scoring,
same judge prompt, same metric aggregation. Only the memory backend
behind the SPI changes.

Usage:
    cd astrocyte-py
    doppler run -- env DATABASE_URL=... ASTROCYTE_PG_DSN=... \\
        uv run python scripts/mem0_harness/run_locomo.py \\
            --project-name astrocyte-m13.1 \\
            --backend oss \\
            --judge-model gpt-4o --judge-provider openai \\
            --answerer-model gpt-4o --provider openai \\
            --max-workers 2 --rpm 60 \\
            --top-k 200

The ``--backend oss`` flag is required by the upstream runner but is a
no-op for AstrocyteClient (we ignore it).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# memory-benchmarks is installed as the ``benchmarks`` package (the
# AstrocyteAI/memory-benchmarks fork, which adds packaging metadata
# upstream lacks) via ``make bench-runner-deps`` — no sys.path shim,
# no hardcoded local path. Probed via ``find_spec`` rather than a bare
# ``import benchmarks`` so the availability check doesn't leave an
# unused import behind (py/unused-import).
if importlib.util.find_spec("benchmarks") is None:  # pragma: no cover
    raise RuntimeError(
        "memory-benchmarks package not importable — run `make bench-runner-deps` "
        "(installs the AstrocyteAI/memory-benchmarks fork).",
    )

# Add our astrocyte-py to sys.path so the adapter resolves before the
# upstream import happens.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import our adapter and the upstream runner module. We import the
# runner LAST so we can monkey-patch its module-level Mem0Client symbol
# before main() runs.
from scripts.mem0_harness.astrocyte_client import (  # noqa: E402
    AstrocyteClient,
    format_search_results,
)


class _LoCoMoAstrocyteClient(AstrocyteClient):
    """LoCoMo-flavoured AstrocyteClient — uses its own bank prefix so
    LME runs on the same Postgres don't collide on bank ids.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("bank_prefix", "m13.1.locomo")
        super().__init__(*args, **kwargs)


# Replace Mem0Client + format_search_results in BOTH the common module
# and the locomo runner so any direct imports route to our adapter.
from benchmarks.common import mem0_client as _mem0_client_mod  # noqa: E402

_mem0_client_mod.Mem0Client = _LoCoMoAstrocyteClient
_mem0_client_mod.format_search_results = format_search_results

from benchmarks.locomo import run as _locomo_run  # noqa: E402

_locomo_run.Mem0Client = _LoCoMoAstrocyteClient
_locomo_run.format_search_results = format_search_results

# M18b experimental: optional Hindsight SSP prompt block.
# Gated by ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT=1. Default off — existing
# benches unchanged. See _hindsight_prompt.py for rationale (B2 SSP
# regression diagnosis).
from scripts.mem0_harness._hindsight_prompt import maybe_apply_ssp_patch  # noqa: E402

maybe_apply_ssp_patch("locomo")

# M22 — Hindsight-parity answerer prompt + structured context (Gaps 1-4).
# Gated by ASTROCYTE_M22_HINDSIGHT_ANSWERER=1. Default off. See
# _hindsight_answerer.py for the porting rationale.
from scripts.mem0_harness._hindsight_answerer import (  # noqa: E402
    maybe_install_hindsight_answerer_patch,
)

maybe_install_hindsight_answerer_patch("locomo")

# M39 — append per-category focused prompts (our M19a + M31d Fix E
# blocks) to the upstream answerer prompt. Independent of M22; gated
# by ASTROCYTE_M39_QTYPE_PROMPTS=1.
from scripts.mem0_harness._qtype_prompts_patch import (  # noqa: E402
    maybe_install_qtype_prompts_patch,
)

maybe_install_qtype_prompts_patch("locomo")

# M35-4 — token-budget cutoff reporting. When
# ASTROCYTE_MAX_TOKENS_CUTOFFS is set (e.g. "1024,2048,4096,8192"),
# replace the framework's item-count cutoff slicing with token-count
# slicing. The cutoff labels become ``max_tokens_N`` instead of
# ``top_N``. Without the env var, the framework's original
# item-count cutoff behaviour is preserved.
from scripts.mem0_harness._token_cutoffs_patch import (  # noqa: E402
    maybe_install_token_cutoffs_patch,
)

# Side-effect import: the patch installs itself on the upstream module.
# Return value is intentionally discarded.
maybe_install_token_cutoffs_patch("locomo")


# ── M20 Day 3 (rewrite) — Hindsight-parity integrated reflect mode ─────
# When ``ASTROCYTE_USE_REFLECT=1``, monkey-patch ``_locomo_run.process_question``
# so the predicted answer is ``mem0.reflect()`` output DIRECTLY, not routed
# through the bench's downstream answerer. Mirrors Hindsight's
# ``LoComoReflectAnswerGenerator`` (``needs_external_search() = False``) —
# see ``hindsight-dev/benchmarks/locomo/locomo_benchmark.py:198``. This is
# the only fair way to bench reflect: the bench measures what the public
# API actually returns to a caller, not what an extra answerer LLM call
# would post-process out of reflect's answer + sources.
#
# Implementation: we keep the original ``search()`` call (so the result
# JSON's ``retrieval`` payload remains populated for traceability), but
# replace the per-cutoff ``answerer.generate()`` + judge loop with a
# single ``mem0.reflect()`` call whose answer is reused at every cutoff.
# This matches Hindsight's design — reflect has no cutoff concept.
import os as _os  # noqa: E402
from typing import Any  # noqa: E402


def _maybe_install_reflect_process_question() -> None:
    if _os.environ.get("ASTROCYTE_USE_REFLECT", "").lower() not in ("1", "true", "yes", "hybrid", "auto"):
        return

    _original_process_question = _locomo_run.process_question

    # M36 — per-question hybrid routing. Imported at install time so
    # the per-call hot path is just a function call + simple check.
    from scripts.mem0_harness._reflect_routing import (  # noqa: PLC0415
        is_hybrid_routing,
        should_use_reflect_for_question,
    )

    _hybrid = is_hybrid_routing()

    # Pull symbols needed by the patched function from the upstream module
    # at install time — avoids per-call import overhead.
    import time as _time  # noqa: PLC0415

    from benchmarks.locomo.run import (  # noqa: PLC0415
        CATEGORY_NAMES,
        JUDGE_SYSTEM_PROMPT,
        cutoff_label,
        format_search_results,
        get_judge_prompt,
        get_judge_prompt_with_evidence,
        preprocess_answer,
    )

    async def _reflect_process_question(
        qa: dict,
        qa_idx: int,
        conv_idx: int,
        user_id: str,
        mem0: Any,
        answerer: Any,
        judge_llm: Any,
        cutoffs: list[int],
        top_k: int,
        reference_date_human: str | None,
        user_profile: dict | None,
        evidence_lookup: dict | None,
        predict_only: bool,
        logger: Any,
        score_debug: bool = False,
    ) -> dict[str, Any]:
        question_id = f"conv{conv_idx}_q{qa_idx}"
        question = qa["question"]
        category = qa["category"]
        answer = str(qa["answer"])

        # M36 — per-question hybrid routing. When enabled (the M36
        # default for ASTROCYTE_USE_REFLECT=1), questions that do NOT
        # trigger temporal extraction skip reflect entirely and go
        # through the standard recall+answerer path. M20 measured that
        # all-on reflect regresses synthesis-heavy categories; this
        # selective routing realises M21's deferred +5q projection.
        if _hybrid and not await should_use_reflect_for_question(question):
            return await _original_process_question(
                qa, qa_idx, conv_idx, user_id, mem0, answerer, judge_llm,
                cutoffs, top_k, reference_date_human, user_profile,
                evidence_lookup, predict_only, logger,
                score_debug=score_debug,
            )

        # Run the standard search() for retrieval traceability — the
        # result JSON still gets a populated ``retrieval`` block so
        # downstream diff tools / per-cutoff debug still works.
        start = _time.monotonic()
        search_results = await mem0.search(question, user_id, top_k=top_k, score_debug=score_debug)
        search_latency = (_time.monotonic() - start) * 1000
        formatted, query_debug = format_search_results(search_results)

        result: dict[str, Any] = {
            "question_id": question_id,
            "conversation_idx": conv_idx,
            "category": category,
            "category_name": CATEGORY_NAMES.get(category, "unknown"),
            "question": question,
            "ground_truth_answer": answer,
            "evidence": qa.get("evidence", []),
            "user_id": user_id,
            "reference_date": reference_date_human,
            "retrieval": {
                "search_query": question,
                "search_results": formatted,
                "search_latency_ms": round(search_latency, 1),
                "total_results": len(formatted),
            },
        }
        if query_debug:
            result["retrieval"]["query_debug"] = query_debug
        if user_profile:
            result["user_profile"] = user_profile
        if predict_only:
            return result

        # Reflect — one call per question; same answer at every cutoff.
        reflect_start = _time.monotonic()
        try:
            reflect_out = await mem0.reflect(question=question, user_id=user_id)
            generated_answer = reflect_out.get("answer", "")
            iterations = reflect_out.get("iterations", 0)
            n_sources = len(reflect_out.get("sources", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("reflect failed for %s: %s — falling back to empty answer", question_id, exc)
            generated_answer = ""
            iterations = 0
            n_sources = 0
        reflect_latency = (_time.monotonic() - reflect_start) * 1000

        result["reflect"] = {
            "iterations": iterations,
            "num_sources": n_sources,
            "latency_ms": round(reflect_latency, 1),
        }

        # Judge once, copy verdict to every cutoff (reflect's answer is
        # cutoff-agnostic). Matches Hindsight's integrated-mode pattern.
        processed_answer = preprocess_answer(category, answer)
        ev_ctx = ""
        if evidence_lookup:
            for ref in qa.get("evidence", []):
                key = (conv_idx, ref)
                if key in evidence_lookup:
                    ev_ctx += evidence_lookup[key] + "\n"
            ev_ctx = ev_ctx.strip()
        if ev_ctx:
            judge_prompt = get_judge_prompt_with_evidence(
                category, question, processed_answer, generated_answer, ev_ctx,
            )
        else:
            judge_prompt = get_judge_prompt(category, question, processed_answer, generated_answer)
        raw = await judge_llm.generate_structured(
            system=JUDGE_SYSTEM_PROMPT,
            user=judge_prompt,
        )
        if isinstance(raw, dict):
            correct = raw.get("label", "").upper() == "CORRECT"
            reason = raw.get("reasoning", "")
        else:
            correct = False
            reason = ""
        score = 1.0 if correct else 0.0
        judgment = "CORRECT" if correct else "WRONG"

        cutoff_results: dict[str, dict] = {}
        for c in cutoffs:
            cutoff_results[cutoff_label(c)] = {
                "judgment": judgment,
                "score": score,
                "generated_answer": generated_answer,
                # ``memories_evaluated`` reflects what reflect ACTUALLY used
                # (cited sources), not the bench's per-cutoff slice — the
                # cutoff has no meaning in integrated mode.
                "memories_evaluated": n_sources,
                "reason": reason,
            }
        result["cutoff_results"] = cutoff_results
        return result

    _locomo_run.process_question = _reflect_process_question
    print(
        "[reflect-bench] ASTROCYTE_USE_REFLECT=1 — patched process_question to use mem0.reflect() directly "
        "(Hindsight integrated-mode parity).",
        file=sys.stderr,
    )


_maybe_install_reflect_process_question()


if __name__ == "__main__":
    _locomo_run.main()
