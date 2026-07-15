"""LongMemEval bench runner via Mem0's harness, driving AstrocyteClient.

M13.1 sibling to ``run_locomo.py``. Same monkey-patch strategy:
swap ``Mem0Client`` → ``AstrocyteClient`` inside the upstream LME
runner module, then hand off to its ``main()``.

LME differs from LoCoMo on two axes the adapter already handles
correctly:
  - ``CHUNK_SIZE = 2`` (Mem0 calls ``add()`` per user+assistant pair).
    Our timestamp-grouped session reconstruction is chunk-size-agnostic.
  - ``user_id = longmemeval_{question_id}_{run_id}`` — per-question
    isolation. Each question gets its own bank in Astrocyte
    (``m13.1.lme:<user_id>``). Cold-start cost is paid per question.

Usage:
    cd astrocyte-py
    doppler run -- env DATABASE_URL=... ASTROCYTE_PG_DSN=... \\
        uv run python scripts/mem0_harness/run_lme.py \\
            --project-name astrocyte-m13.1 \\
            --backend oss \\
            --judge-model gpt-4o --judge-provider openai \\
            --answerer-model gpt-4o --provider openai \\
            --max-workers 4 --rpm 60 \\
            --top-k 200 \\
            [--user-profile]
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.mem0_harness.astrocyte_client import (  # noqa: E402
    AstrocyteClient,
    format_search_results,
)


# Distinct bank prefix so LME and LoCoMo banks don't collide on a
# shared DB. The dispatch should also use a different DB port, but this
# is defence-in-depth.
class _LMEAstrocyteClient(AstrocyteClient):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("bank_prefix", "m13.1.lme")
        super().__init__(*args, **kwargs)


from benchmarks.common import mem0_client as _mem0_client_mod  # noqa: E402

_mem0_client_mod.Mem0Client = _LMEAstrocyteClient
_mem0_client_mod.format_search_results = format_search_results

from benchmarks.longmemeval import run as _lme_run  # noqa: E402

_lme_run.Mem0Client = _LMEAstrocyteClient
_lme_run.format_search_results = format_search_results

# M18b experimental: optional Hindsight SSP prompt block.
# Gated by ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT=1. Default off — existing
# benches unchanged. See _hindsight_prompt.py for rationale (B2 SSP
# regression diagnosis).
from scripts.mem0_harness._hindsight_prompt import maybe_apply_ssp_patch  # noqa: E402

maybe_apply_ssp_patch("longmemeval")

# M22 — Hindsight-parity answerer prompt + structured context (Gaps 1-4).
# Gated by ASTROCYTE_M22_HINDSIGHT_ANSWERER=1. Default off so prior
# benches stay byte-identical until the flag flips. When ON, replaces
# the upstream get_answer_generation_prompt with a Hindsight-style
# directive prompt and structured context shape (fact + chunk +
# entity observations + mental models) and routes per question type.
# See _hindsight_answerer.py for the full porting rationale + format.
from scripts.mem0_harness._hindsight_answerer import (  # noqa: E402
    maybe_install_hindsight_answerer_patch,
)

maybe_install_hindsight_answerer_patch("longmemeval")

# M39 — append per-category focused prompts (our M19a + M31d Fix E
# blocks) to the upstream answerer prompt. Independent of M22; gated
# by ASTROCYTE_M39_QTYPE_PROMPTS=1.
from scripts.mem0_harness._qtype_prompts_patch import (  # noqa: E402
    maybe_install_qtype_prompts_patch,
)

maybe_install_qtype_prompts_patch("longmemeval")

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
maybe_install_token_cutoffs_patch("lme")


# M36 — per-question hybrid reflect routing for LME. When
# ASTROCYTE_USE_REFLECT=1 (default hybrid mode), questions whose
# query-analyzer returns a temporal_constraint go through reflect;
# everything else stays on the standard recall+answerer path. Mirrors
# the LoCoMo installer in run_locomo.py.
import os as _os  # noqa: E402
from typing import Any as _Any  # noqa: E402


def _maybe_install_lme_reflect_process_question() -> None:
    if _os.environ.get("ASTROCYTE_USE_REFLECT", "").lower() not in (
        "1", "true", "yes", "hybrid", "auto",
    ):
        return

    _original_process_question = _lme_run.process_question_answerer

    from scripts.mem0_harness._reflect_routing import (  # noqa: PLC0415
        is_hybrid_routing,
        should_use_reflect_for_question,
    )

    _hybrid = is_hybrid_routing()

    import time as _time  # noqa: PLC0415

    from benchmarks.longmemeval.run import (  # noqa: PLC0415
        cutoff_label,
        format_search_results,
        get_judge_prompt,
        parse_longmemeval_date_human,
    )

    async def _reflect_process_question(
        question: dict,
        user_id: str,
        mem0: _Any,
        answerer: _Any,
        judge_llm: _Any,
        cutoffs: list[int],
        top_k: int,
        user_profile: dict | None,
        predict_only: bool,
        logger: _Any,
        score_debug: bool = False,
        existing_search_results: list | None = None,
    ) -> dict[str, _Any]:
        question_text = question["question"]
        question_type = question["question_type"]
        question_id = question["question_id"]
        answer = str(question["answer"])
        question_date = question.get("question_date", "")
        question_date_human = (
            parse_longmemeval_date_human(question_date) if question_date else ""
        )

        # M36 — when hybrid is on (default for ASTROCYTE_USE_REFLECT=1),
        # route non-temporal questions through the standard path.
        if _hybrid and not await should_use_reflect_for_question(question_text):
            return await _original_process_question(
                question, user_id, mem0, answerer, judge_llm, cutoffs, top_k,
                user_profile, predict_only, logger,
                score_debug=score_debug,
                existing_search_results=existing_search_results,
            )

        # Retrieval traceability — populate the `retrieval` block from
        # the standard search(), same as the all-on reflect mode.
        if existing_search_results is not None:
            formatted = existing_search_results
            query_debug = None
            search_latency = 0.0
        else:
            start = _time.monotonic()
            search_results = await mem0.search(
                question_text, user_id, top_k=top_k, score_debug=score_debug,
            )
            search_latency = (_time.monotonic() - start) * 1000
            formatted, query_debug = format_search_results(search_results)

        result: dict[str, _Any] = {
            "question_id": question_id,
            "question_type": question_type,
            "question": question_text,
            "ground_truth_answer": answer,
            "question_date": question_date,
            "is_abstention": question_id.endswith("_abs"),
            "user_id": user_id,
            "answer_session_ids": question.get("answer_session_ids", []),
            "retrieval": {
                "search_query": question_text,
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

        # Reflect — one call per question; answer applies at every cutoff.
        reflect_start = _time.monotonic()
        try:
            reflect_out = await mem0.reflect(question=question_text, user_id=user_id)
            generated_answer = reflect_out.get("answer", "")
            iterations = reflect_out.get("iterations", 0)
            n_sources = len(reflect_out.get("sources", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reflect failed for %s: %s — falling back to empty answer",
                question_id, exc,
            )
            generated_answer = ""
            iterations = 0
            n_sources = 0
        reflect_latency = (_time.monotonic() - reflect_start) * 1000

        result["reflect"] = {
            "iterations": iterations,
            "num_sources": n_sources,
            "latency_ms": round(reflect_latency, 1),
        }

        # Judge once, copy verdict to every cutoff.
        judge_prompt = get_judge_prompt(
            question_type=question_type,
            question_id=question_id,
            question=question_text,
            answer=answer,
            response=generated_answer,
            question_date=question_date_human,
        )
        correct, judge_raw = await judge_llm.judge_yes_no(judge_prompt)
        score = 1.0 if correct else 0.0
        judgment = "PASS" if correct else "FAIL"

        cutoff_results: dict[str, dict] = {}
        for c in cutoffs:
            cutoff_results[cutoff_label(c)] = {
                "judgment": judgment,
                "score": score,
                "generated_answer": generated_answer,
                "judge_raw": judge_raw,
                "memories_evaluated": n_sources,
                "reason": f"Reflect answer (iterations={iterations}): {generated_answer[:500]}",
            }
        result["cutoff_results"] = cutoff_results
        return result

    _lme_run.process_question_answerer = _reflect_process_question
    mode = "hybrid" if _hybrid else "all-on"
    print(
        f"[reflect-bench-lme] ASTROCYTE_USE_REFLECT=1 ({mode}) — patched LME "
        "process_question to route temporal queries through mem0.reflect().",
        file=sys.stderr,
    )


_maybe_install_lme_reflect_process_question()


# M46 (arithmetic-as-tool) — patch file kept as banked infrastructure
# (``scripts/mem0_harness/_temporal_tool_patch.py``) but installer is
# NOT wired here. The cycle benched at N=2 M46-ON measurements vs N=3
# M44-ON baseline (2026-05-24, see v0.15.0-ship-decision.md Appendix C
# §M46+M47 variance experiment). Verdict:
#   - LME TR (target metric): +1.2q above baseline mean (barely above
#     the ±1q per-category noise floor at n=15)
#   - LME overall: -4.9q below baseline mean (above the ±4q noise floor)
#   - Combined: mechanism is sound (function-calling for arithmetic),
#     bench signal does NOT defensibly support shipping default-ON.
#
# To re-enable for ablation / future revisit, add:
#   from scripts.mem0_harness._temporal_tool_patch import (
#       maybe_install_temporal_tool_patch_lme,
#   )
#   maybe_install_temporal_tool_patch_lme()
# and set ASTROCYTE_M46_TEMPORAL_TOOL=1.


if __name__ == "__main__":
    _lme_run.main()
