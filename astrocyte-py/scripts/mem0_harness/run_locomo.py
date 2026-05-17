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

import sys
from pathlib import Path

_MEM0_BENCH_REPO = Path("/Users/calvin/AstrocyteAI/memory-benchmarks")
if not _MEM0_BENCH_REPO.exists():
    raise RuntimeError(
        f"memory-benchmarks repo not found at {_MEM0_BENCH_REPO}. "
        "Clone https://github.com/mem0ai/memory-benchmarks there first.",
    )
if str(_MEM0_BENCH_REPO) not in sys.path:
    sys.path.insert(0, str(_MEM0_BENCH_REPO))

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


if __name__ == "__main__":
    _locomo_run.main()
