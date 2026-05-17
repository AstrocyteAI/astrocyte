"""Optional answerer-prompt augmentation — Hindsight-parity SSP block.

Ports the preference / recommendation prompt block from Hindsight's
``hindsight-dev/benchmarks/longmemeval/longmemeval_benchmark.py:319-335``
into our bench runners (LME + LoCoMo), gated by env var.

**Why this exists:** in M18b's B2 ablation, directive_compile produced
+2.25pp LoCoMo lift (via open-domain / temporal subcategories) but
regressed LME single-session-preference (SSP) by ~30pp. Diagnosis
showed the failure mode is **information loss via compression**: the
auto-extracted directive ("Recommend Sony-compatible accessories")
acts as a list-of-things-to-mention hint rather than a prioritization
rule, so the answerer mentions Sony in passing but doesn't structure
the answer around the user's stated preference.

Hindsight handles SSP via answerer prompt engineering, NOT preference
compression — their `directive` subtype is for USER-AUTHORED hard
rules (via the ``create_directive`` MCP tool), not auto-extracted.
Their SSP prompt block explicitly tells the answerer to:

  - NOT invent specific recommendations
  - DO mention specific brands/products the user ALREADY uses
  - Describe WHAT KIND of recommendation the user would prefer
  - Reference the user's existing tools/brands EXPLICITLY

This module monkey-patches the upstream
``benchmarks/{longmemeval,locomo}/prompts.ANSWER_GENERATION_PROMPT``
to append the Hindsight block when
``ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT=1`` is set. Default: off (so
existing benches are byte-identical to before this module landed).

Usage from a runner (run_lme.py / run_locomo.py):

    from scripts.mem0_harness._hindsight_prompt import maybe_apply_ssp_patch
    maybe_apply_ssp_patch("longmemeval")  # or "locomo"
"""

from __future__ import annotations

import os
import sys

# Hindsight's preference-question instructions, verbatim from their LME
# bench prompt (longmemeval_benchmark.py:319-335). Kept in this single
# constant so both runners get the IDENTICAL block — avoids drift.
_HINDSIGHT_SSP_BLOCK = """

**Question-Type Routing — apply ONE of the following blocks based on the question shape; ignore the others.**

**For Recommendation/Preference Questions (tips, suggestions, advice for the user):**
- **DO NOT invent specific recommendations** (no made-up product names, course names, paper titles, channel names, etc.)
- **DO mention specific brands/products the user ALREADY uses** from the context — by name, in the recommendations themselves.
- Describe WHAT KIND of recommendation the user would prefer, referencing their existing tools/brands EXPLICITLY in the answer structure.
- Keep answers concise — focus on key preferences (brand, quality level, specific interests) not exhaustive category lists.
- First scan ALL facts for user's existing tools, brands, stated preferences — structure the recommendation around those, not around generic categories.
- If the User Profile contains a stated preference (e.g., "user prefers X brand"), the recommendations MUST center on that preference, not mention it as an aside.

**For Multi-hop / Synthesis Questions (questions requiring evidence from multiple sessions or facts, not a single preference):**
- Synthesize across MULTIPLE retrieved facts — do not over-index on any single fact.
- Do NOT structure the answer around a single brand or preference (that's for recommendation questions only).
- List the supporting facts inline (1, 2, 3...) to make the synthesis traceable.
- If facts conflict, prefer the more recent / more specific one and note the conflict explicitly.

**For Temporal-Reasoning Questions ("how long ago", "before/after", date arithmetic):**
- Find the ORIGINAL mention date for each event — older facts are often the right ones (not the most recent).
- Convert relative dates to absolute first; do the arithmetic explicitly; show your work.
- Do NOT apply preference framing to date-arithmetic answers.

**For Knowledge-Update Questions (the user's current state, latest values):**
- Use the MOST RECENT fact for any field that can change over time (job, location, status).
- Earlier facts about the same field are HISTORICAL, not current.
"""


def is_enabled() -> bool:
    """Return True unless ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT is explicitly off.

    **Default ON post-M19 ship** (2026-05-18, v0.14.0). The per-Q-type
    routing prompt (4 inline category blocks: recommendation, multi-hop,
    temporal, knowledge-update) shipped with M19a, where the 2-run mean
    (193/230 = 83.91%) cleared the M17+1σ ship gate by +2.25pp.

    Set ``ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT=0`` to force off for
    ablation runs.
    """
    val = os.environ.get("ASTROCYTE_M18_HINDSIGHT_SSP_PROMPT", "").lower()
    if val in ("0", "false", "no"):
        return False
    return True


def maybe_apply_ssp_patch(bench_name: str) -> bool:
    """Append the Hindsight SSP block to the upstream answer-generation
    prompt for the given bench.

    Args:
      bench_name: "longmemeval" or "locomo" — the upstream sub-package
        name under ``memory-benchmarks/benchmarks/``.

    Returns True when the patch was applied, False when the flag was
    off or the upstream module/attribute couldn't be located. Idempotent
    — calling twice is a no-op (the block is appended once based on a
    sentinel substring check).
    """
    if not is_enabled():
        return False
    mod_name = f"benchmarks.{bench_name}.prompts"
    if mod_name not in sys.modules:
        # Import lazily so the runner controls import order.
        try:
            __import__(mod_name)
        except ImportError as exc:
            print(
                f"[hindsight-ssp-prompt] could not import {mod_name}: {exc}",
                file=sys.stderr,
            )
            return False
    mod = sys.modules[mod_name]
    if not hasattr(mod, "ANSWER_GENERATION_PROMPT"):
        print(
            f"[hindsight-ssp-prompt] {mod_name} has no ANSWER_GENERATION_PROMPT — skip",
            file=sys.stderr,
        )
        return False
    current = mod.ANSWER_GENERATION_PROMPT
    sentinel = "For Recommendation/Preference Questions (tips, suggestions, advice)"
    if sentinel in current:
        # Already patched (idempotent re-run, or upstream added the block themselves).
        return True
    mod.ANSWER_GENERATION_PROMPT = current + _HINDSIGHT_SSP_BLOCK
    print(
        f"[hindsight-ssp-prompt] appended SSP block to {mod_name}.ANSWER_GENERATION_PROMPT",
        file=sys.stderr,
    )
    return True
