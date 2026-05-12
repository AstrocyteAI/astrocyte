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


if __name__ == "__main__":
    _lme_run.main()
