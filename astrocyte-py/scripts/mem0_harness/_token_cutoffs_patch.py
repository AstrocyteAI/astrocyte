"""M35-4 — token-budget cutoff reporting for the Mem0 bench framework.

Pre-M35 the bench framework's ``process_question`` loops over
``cutoffs: list[int]`` (e.g. ``[10, 20, 50, 200]``) and slices the
formatted search results by **item count** at each cutoff:

    for c in cutoffs:
        sliced = formatted[:c]
        cutoff_results[f"top_{c}"] = {... score from answerer(sliced) ...}

This patch swaps the slicing primitive from item-count to **token-count**
to match our internal ``max_tokens`` recall budget (M35-2). With the
patch enabled and ``cutoffs=[1024, 2048, 4096, 8192]``, the loop runs:

    for c in cutoffs:
        sliced = _pack_to_budget(formatted, c)
        cutoff_results[f"max_tokens_{c}"] = {...}

Why this matters
----------------

Without this patch, our adapter returns a token-budgeted list (~25-30
facts at ``max_tokens=4096``) but the framework's per-cutoff scoring
chops the list at item-count cutoffs (10, 20, 50, 200). The "top_20"
report measures the framework's artificial truncation of our list,
not our actual recall quality.

Gating
------

Patch installs when ``ASTROCYTE_MAX_TOKENS_CUTOFFS`` env var is set to
a comma-separated list of integer budgets (e.g. ``1024,2048,4096,8192``).
When unset, leaves the framework's item-count cutoff loop alone — pre-
M35 benches stay reproducible.

References
----------

- Upstream cutoff loop: ``memory-benchmarks/benchmarks/locomo/run.py``
  ``process_question`` (~line 460) and same in
  ``benchmarks/longmemeval/run.py``.
- Internal token counter: ``astrocyte.pipeline.token_budget.count_tokens``.
- Design doc: ``docs/_design/m34-query-intent-routing.md`` §M35 note.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger("astrocyte.mem0_harness.token_cutoffs")

_ENV_VAR = "ASTROCYTE_MAX_TOKENS_CUTOFFS"


def _parse_env_cutoffs() -> list[int] | None:
    """Parse ``ASTROCYTE_MAX_TOKENS_CUTOFFS`` env var to a list of ints.

    Returns ``None`` when unset or empty (caller skips installation).
    Invalid integers are dropped with a warning rather than crashing —
    a typo'd value shouldn't take the bench down.
    """
    raw = (os.environ.get(_ENV_VAR) or "").strip()
    if not raw:
        return None
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            _logger.warning("ignoring non-integer cutoff %r in %s", token, _ENV_VAR)
    return out or None


def _pack_results_to_token_budget(
    formatted: list[dict[str, Any]],
    *,
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Trim formatted bench results to a token budget.

    ``formatted`` items are framework dicts shaped like
    ``{"id": ..., "memory": "<text>", "score": ...}``. We tokenize the
    ``"memory"`` field via the same encoding used by the answerer
    (cl100k_base for gpt-4o-mini).

    Always includes the first item even if oversize (mirrors the
    contract of :func:`astrocyte.pipeline.token_budget.pack_to_budget`).
    """
    from astrocyte.pipeline.token_budget import count_tokens  # noqa: PLC0415

    if max_tokens <= 0 or not formatted:
        return []

    out: list[dict[str, Any]] = []
    used = 0
    for i, item in enumerate(formatted):
        text = str(item.get("memory") or "")
        cost = count_tokens(text)
        if i == 0:
            out.append(item)
            used += cost
            continue
        if used + cost > max_tokens:
            continue
        out.append(item)
        used += cost
    return out


def _build_patched_process_question(
    upstream_run_mod,
    cutoffs: list[int],
):
    """Wrap upstream ``process_question`` so the cutoff loop uses
    token budgets instead of item-count slicing.

    Returns the new function — caller assigns it to
    ``upstream_run_mod.process_question``.
    """
    _orig = upstream_run_mod.process_question
    # The upstream loop pulls these symbols off the run module — we
    # don't redefine them, just rewire the slicer + label.
    from benchmarks.common.utils import cutoff_label as _orig_label  # noqa: PLC0415

    async def _patched(*args, **kwargs):
        # Replace the ``cutoffs`` kwarg with our budgets if upstream
        # was given the item-count default. This lets the rest of the
        # function tree (which still calls ``cutoff_label`` and slices
        # by integer) operate on max_tokens values transparently — the
        # only behaviour difference is the slicer (handled below) and
        # the label format (also below).
        kwargs["cutoffs"] = cutoffs
        # Monkey-patch the upstream module's symbols just for the
        # duration of this call. Restore afterwards so other callers
        # (e.g. ``apply_locomo_judge_to_saved_result``) see the
        # original slicing.
        _orig_slicer = getattr(upstream_run_mod, "_token_slicer", None)
        _orig_lbl = upstream_run_mod.cutoff_label

        def _token_label(c: int | None) -> str:
            return "all" if c is None else f"max_tokens_{c}"

        upstream_run_mod.cutoff_label = _token_label

        # Install a sentinel list-slice wrapper. The framework's loop is
        # ``sliced = formatted[:c]``; we can't intercept that without
        # rewriting the loop. Instead the cleanest hook is to wrap
        # ``formatted`` itself with a custom subclass whose ``__getitem__``
        # for a slice ``[:c]`` invokes our token-budget packer.
        try:
            return await _orig(*args, **kwargs)
        finally:
            upstream_run_mod.cutoff_label = _orig_lbl
            if _orig_slicer is None:
                upstream_run_mod.__dict__.pop("_token_slicer", None)
            else:
                upstream_run_mod._token_slicer = _orig_slicer

    return _patched


class _TokenBudgetSliceList(list):
    """List subclass whose ``[:c]`` returns a token-budgeted prefix
    instead of the first ``c`` items.

    Used to replace ``formatted`` inside upstream's ``process_question``
    so the ``sliced = formatted[:c]`` line in the cutoff loop produces
    a token-bounded prefix transparently. Other list operations are
    unchanged.
    """

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, slice) and key.start is None and key.step is None and isinstance(key.stop, int):
            return _pack_results_to_token_budget(
                list.__iter__(self),  # type: ignore[arg-type]
                max_tokens=key.stop,
            )
        return super().__getitem__(key)


def _coerce_formatted_to_token_slice_list(upstream_run_mod) -> None:
    """Patch upstream's ``format_search_results`` so it returns a
    ``_TokenBudgetSliceList`` instead of a plain list.

    Downstream code that does ``formatted[:c]`` then transparently gets
    a token-budgeted prefix when ``c`` is one of our max_tokens cutoffs.
    """
    _orig_format = upstream_run_mod.format_search_results

    def _patched_format(*args, **kwargs):
        result = _orig_format(*args, **kwargs)
        # ``format_search_results`` returns (list_of_dicts, debug_dict).
        if isinstance(result, tuple) and len(result) >= 1 and isinstance(result[0], list):
            wrapped = _TokenBudgetSliceList(result[0])
            return (wrapped, *result[1:])
        if isinstance(result, list):
            return _TokenBudgetSliceList(result)
        return result

    upstream_run_mod.format_search_results = _patched_format


def maybe_install_token_cutoffs_patch(bench: str) -> list[int] | None:
    """Install the token-budget cutoff patches on the named bench's
    upstream run module.

    ``bench`` is ``"locomo"`` or ``"lme"`` — selects which upstream
    module gets patched. When the env var is unset, returns ``None``
    and patches nothing (caller falls back to item-count cutoffs).

    Returns the parsed cutoff list when installed — caller can re-emit
    it onto the bench's argv so the rest of the framework sees the
    token budgets via the same ``--top-k-cutoffs`` plumbing.
    """
    cutoffs = _parse_env_cutoffs()
    if not cutoffs:
        return None

    if bench == "locomo":
        from benchmarks.locomo import run as upstream_mod  # noqa: PLC0415
    elif bench == "lme":
        from benchmarks.longmemeval import run as upstream_mod  # noqa: PLC0415
    else:
        raise ValueError(f"unknown bench {bench!r}; expected 'locomo' or 'lme'")

    # 1. cutoff_label → "max_tokens_N" instead of "top_N"
    def _token_label(c: int | None) -> str:
        return "all" if c is None else f"max_tokens_{c}"

    upstream_mod.cutoff_label = _token_label

    # Also patch the common.utils module — some imports go through
    # ``from benchmarks.common.utils import cutoff_label`` (locomo
    # runs do this) so we patch both sources.
    from benchmarks.common import utils as _utils_mod  # noqa: PLC0415

    _utils_mod.cutoff_label = _token_label

    # 2. format_search_results → returns TokenBudgetSliceList
    _coerce_formatted_to_token_slice_list(upstream_mod)
    # The astrocyte_client also exports format_search_results — patch
    # there too so any path that re-exports gets the wrapping behaviour.
    from scripts.mem0_harness import astrocyte_client as _client_mod  # noqa: PLC0415

    _client_orig = _client_mod.format_search_results

    def _client_patched(*args, **kwargs):
        result = _client_orig(*args, **kwargs)
        if isinstance(result, tuple) and len(result) >= 1 and isinstance(result[0], list):
            return (_TokenBudgetSliceList(result[0]), *result[1:])
        if isinstance(result, list):
            return _TokenBudgetSliceList(result)
        return result

    _client_mod.format_search_results = _client_patched

    _logger.info(
        "token_cutoffs_patch installed for %s with budgets=%s — bench reports max_tokens_N labels",
        bench,
        cutoffs,
    )
    return cutoffs


__all__ = ["maybe_install_token_cutoffs_patch"]
