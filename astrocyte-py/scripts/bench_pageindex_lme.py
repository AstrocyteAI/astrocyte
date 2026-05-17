"""PR1 commit D — LME smoke harness for the PageIndex POC.

Sibling to ``bench_pageindex_locomo.py`` but for LongMemEval. PR1's job
here is narrow: confirm the SPI-port and the conversation→markdown
renderer don't crash on LME's data shape (one user → many sessions →
mixed user/assistant turns). PR2 is where we actually optimise LME
accuracy via the entity layer + `supersedes` graph + speaker-tag
filtering; PR3 wires the wiki for `knowledge-update`.

What this script does:
- Loads ``longmemeval_s_cleaned.json``
- For each sample, renders the haystack_sessions as one markdown
  document (sessions become ``## Session N`` headers; turns carry
  ``role`` so PR2's speaker-tag pass has source data to extract from).
- Runs the same PageIndex agent loop as the LoCoMo bench (mode-aware
  picker + synth) — *no LME-specific tuning yet*.
- Scores via ``LongMemEvalJudge`` (same LLM-judge the in-tree LME
  bench uses).
- Reports overall + per-category accuracy.

What PR1 expects:
- LoCoMo target: ≥62% (parity with file-based v7).
- LME target: **runs to completion without crash**. Accuracy is a
  baseline reading; lift comes in PR2/PR3.

Usage:
  doppler run -- uv run python scripts/bench_pageindex_lme.py \\
      --backend memory \\
      --max-samples 50

Args:
  --backend file|memory|postgres   Same dispatch as bench_pageindex_locomo.
  --max-samples N                  How many LME samples to score (≤500).
  --workspace PATH                 File-cache root (only used for file backend).
  --bank-id ID                     Bank id used by store backends.
  --no-judge                       Skip LLM judging (smoke that just dumps responses).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timezone
from pathlib import Path

# PageIndex package is sibling to the astrocyte repo on the user's machine.
_PAGEINDEX_ROOT = Path("/Users/calvin/AstrocyteAI/PageIndex")
if str(_PAGEINDEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAGEINDEX_ROOT))

# Reuse the LoCoMo bench's helpers (agent loop, mode dispatch, picker,
# synth, conversion). They're file-format-agnostic; we only need a
# different markdown renderer.
_BENCH_LOCOMO = Path(__file__).resolve().parent / "bench_pageindex_locomo.py"
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("bench_pi_locomo", _BENCH_LOCOMO)
_BENCH = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_BENCH)

from astrocyte.eval.judges.longmemeval_judge import LongMemEvalJudge  # noqa: E402
from astrocyte.providers.openai import OpenAIProvider  # noqa: E402
from astrocyte.testing.in_memory import InMemoryPageIndexStore  # noqa: E402

# ── Markdown renderer (LME-specific) ─────────────────────────────────────


def lme_sample_to_markdown(sample: dict, sample_id: str) -> str:
    """Render an LME sample's haystack as PageIndex-friendly markdown.

    LME's haystack is a list of sessions; each session is a list of
    ``{role, content}`` turns. We mirror the LoCoMo renderer's shape:
    ``## Session N (date)`` headers so the tree-builder picks them up
    as nodes, and ``[role-N]`` prefixes on each turn so PR2's
    speaker-tag pass has source data to extract from.

    Dates come from ``haystack_dates`` (parallel array to
    haystack_session_ids). When missing, the header omits the date —
    the temporal strategy will fall back to the session's content for
    date discovery in PR2.
    """
    sessions = sample.get("haystack_sessions") or []
    session_ids = sample.get("haystack_session_ids") or []
    dates = sample.get("haystack_dates") or []
    lines = [
        f"# LongMemEval sample {sample_id}",
        "",
        f"Question type: {sample.get('question_type', 'unknown')}",
        "",
    ]
    for i, sess in enumerate(sessions):
        session_id = session_ids[i] if i < len(session_ids) else f"sess-{i}"
        date = dates[i] if i < len(dates) else ""
        # Header pattern matches LoCoMo so the date regex pulls dates
        # without a separate parser. The session_id stays in the title
        # for cite-back; PR2 commit C extracts speaker tags into the
        # section row.
        header = f"## Session {i + 1} ({date})" if date else f"## Session {i + 1}"
        lines.append(header)
        lines.append(f"_session_id: {session_id}_")
        lines.append("")
        for turn_idx, turn in enumerate(sess):
            role = turn.get("role", "?")
            content = (turn.get("content") or "").replace("\n", " ").strip()
            # Mark each turn with role + index so the picker's text-
            # slicer can attribute lines back. Format mimics LoCoMo's
            # [dia_id] **speaker**: pattern but uses [S{n}:T{m}] so the
            # synth doesn't cross-confuse with LoCoMo's dia_ids.
            tag = f"[S{i + 1}:T{turn_idx + 1}]"
            lines.append(f"{tag} **{role}**: {content}")
        lines.append("")
    return "\n".join(lines)


# ── LME-shaped wrapper around build_or_load_tree ─────────────────────────


async def build_lme_tree(
    sample: dict,
    sample_id: str,
    workspace: Path,
    model: str,
    *,
    store=None,
    bank_id: str = "bench-pageindex-lme",
    provider=None,
    entity_model: str | None = None,
    embedding_model: str | None = None,
    mental_model_store=None,
) -> dict:
    """LME-side build wrapper. Same SPI as the LoCoMo bench's
    ``build_or_load_tree`` but rendered from LME's haystack_sessions
    shape. Reuses the LoCoMo bench's caching + dispatch.

    PR2.6 Bug 3: LME samples carry ``question_date`` separately from
    haystack session dates. We override ``reference_date`` with
    ``question_date`` so the temporal-arithmetic short-circuit anchors
    "X weeks ago" to *when the user is asking*, not *when the last
    session ended* (those can differ by weeks). The judge expects
    answers framed against question_date.
    """
    md_path = workspace / f"{sample_id}.md"
    md_text = lme_sample_to_markdown(sample, sample_id)
    md_path.write_text(md_text)

    if store is not None:
        # Inline the store path to avoid needing to mock the LoCoMo-
        # specific ``conv["conversation"]`` shape.
        conv_tree = await _build_lme_tree_via_store(
            store=store,
            bank_id=bank_id,
            sample_id=sample_id,
            md_path=md_path,
            md_text=md_text,
            model=model,
            provider=provider,
            entity_model=entity_model,
            embedding_model=embedding_model,
            mental_model_store=mental_model_store,
        )
    else:
        # File backend
        tree_path = workspace / f"{sample_id}.tree.json"
        if tree_path.exists() and tree_path.stat().st_mtime >= md_path.stat().st_mtime:
            cached = json.loads(tree_path.read_text())
        else:
            print(f"  [pi-lme] Building tree for {sample_id} (file backend)...")
            cached = await _BENCH._build_raw_tree(md_path, model)
            tree_path.write_text(json.dumps(cached, indent=2))
        nodes = cached.get("structure", cached) if isinstance(cached, dict) else cached
        session_dates = _BENCH._enrich_nodes_with_dates(nodes)
        conv_tree = _BENCH._conv_tree_dict(sample_id, md_text, cached, session_dates)

    # PR2.6 Bug 3: anchor relative phrases to LME's question_date when
    # present. Format mirrors haystack_dates ("YYYY/MM/DD (Day) HH:MM")
    # so the existing _parse_session_date works on it.
    qd = sample.get("question_date")
    if qd:
        conv_tree["reference_date"] = qd
    return conv_tree


async def _build_lme_tree_via_store(
    *,
    store,
    bank_id: str,
    sample_id: str,
    md_path: Path,
    md_text: str,
    model: str,
    provider=None,
    entity_model: str | None = None,
    embedding_model: str | None = None,
    mental_model_store=None,
) -> dict:
    """LME equivalent of bench_pageindex_locomo._build_or_load_via_store."""
    cached_doc = await store.load_document(bank_id, sample_id)
    if cached_doc is not None and cached_doc.md_text == md_text:
        sections = await store.load_skeleton(cached_doc.id)
        compact = _BENCH._sections_to_compact_tree(sections)
        session_dates = [
            (s.line_num, _BENCH._format_session_date(s.session_date)) for s in sections if s.session_date is not None
        ]
        session_dates = [(ln, d) for ln, d in session_dates if d]
        return _BENCH._conv_tree_dict(
            sample_id,
            md_text,
            compact,
            session_dates,
            document_id=cached_doc.id,
        )

    print(f"  [pi-lme] Building tree for {sample_id} (store backend)...")
    raw_tree = await _BENCH._build_raw_tree(md_path, model)
    nodes = raw_tree.get("structure", raw_tree) if isinstance(raw_tree, dict) else raw_tree
    session_dates = _BENCH._enrich_nodes_with_dates(nodes)
    reference_date_dt = _BENCH._parse_session_date(session_dates[-1][1]) if session_dates else None

    # Local import to avoid hard dep when --backend is not store-shaped.
    from astrocyte.types import PageIndexDocument

    doc = PageIndexDocument(
        id="",
        bank_id=bank_id,
        source_id=sample_id,
        md_text=md_text,
        reference_date=reference_date_dt,
        built_at=datetime.now(tz=timezone.utc),
    )
    document_id = await store.save_document(doc)
    sections = _BENCH._flatten_tree_to_sections(raw_tree, document_id)
    await store.save_sections(document_id, sections)

    # PR2 commit A: populate entity + embedding indexes (same as LoCoMo).
    if provider is not None:
        await _BENCH._populate_section_index(
            provider=provider,
            store=store,
            document_id=document_id,
            sections=sections,
            md_text=md_text,
            entity_model=entity_model,
            embedding_model=embedding_model,
            bank_id=bank_id,
            mental_model_store=mental_model_store,
        )

    return _BENCH._conv_tree_dict(
        sample_id,
        md_text,
        raw_tree,
        session_dates,
        document_id=document_id,
    )


# ── Stratified sampler (PR2 prep) ────────────────────────────────────────


def _stratified_sample_lme(samples: list[dict], max_samples: int) -> list[dict]:
    """Round-robin pull across ``question_type`` so a 50-sample slice
    exercises every category instead of head-of-list (PR1's issue: the
    first 50 samples were all ``single-session-user``).

    LME's six question_types: single-session-user, single-session-assistant,
    single-session-preference, multi-session, temporal-reasoning,
    knowledge-update (any with ``_abs`` suffix is the abstention variant).

    Mirrors the LoCoMo bench's ``stratified_questions`` round-robin
    pull semantics so the 50Q slice has comparable per-category coverage.
    """
    if not samples or max_samples <= 0:
        return []
    if len(samples) <= max_samples:
        return samples

    by_type: dict[str, list[dict]] = {}
    for s in samples:
        by_type.setdefault(s.get("question_type", "unknown"), []).append(s)

    picks: list[dict] = []
    iters = {qt: iter(items) for qt, items in by_type.items()}
    while len(picks) < max_samples and iters:
        done: list[str] = []
        for qt, it in list(iters.items()):
            try:
                picks.append(next(it))
            except StopIteration:
                done.append(qt)
            if len(picks) >= max_samples:
                break
        for qt in done:
            iters.pop(qt, None)
    return picks


# ── Main ─────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lme-path",
        default="datasets/longmemeval/data/longmemeval_s_cleaned.json",
        help="Path to LongMemEval _s.json (small subset).",
    )
    parser.add_argument(
        "--workspace",
        default="benchmark-results/pageindex/lme",
        help="Where to cache markdown + tree JSON between runs (file backend only).",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="LLM model for tree-build, picker, synthesizer, and judge.",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=50,
        help="Score the first N samples from the dataset (PR1 smoke default = 50).",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM judging — useful for a smoke run that just dumps responses.",
    )
    parser.add_argument(
        "--backend",
        choices=("file", "memory", "postgres"),
        default="file",
        help="PageIndex tree cache backend (M9 / ADR-006).",
    )
    parser.add_argument(
        "--bank-id",
        default="bench-pageindex-lme",
        help="Bank id used when --backend in {memory,postgres}; ignored otherwise.",
    )
    parser.add_argument(
        "--reflect-model",
        default=None,
        help=(
            "Optional stronger model for the agentic-reflect loop only "
            "(e.g. 'gpt-4o'). Diagnostic for whether multi-session "
            "accuracy is model-limited. Falls back to --model when unset."
        ),
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY not set; run via 'doppler run --'.")

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    # ── Backend dispatch ──
    store = None
    mental_model_store = None
    if args.backend == "memory":
        store = InMemoryPageIndexStore()
        from astrocyte.testing.in_memory import InMemoryMentalModelStore  # noqa: PLC0415

        mental_model_store = InMemoryMentalModelStore()
        print(f"  [pi-lme] Backend: in-memory (bank_id={args.bank_id!r})")
    elif args.backend == "postgres":
        from astrocyte_postgres import (  # noqa: PLC0415
            PostgresMentalModelStore,
            PostgresPageIndexStore,
        )

        store = PostgresPageIndexStore(bootstrap_schema=True)
        mental_model_store = PostgresMentalModelStore(bootstrap_schema=True)
        print(f"  [pi-lme] Backend: postgres (bank_id={args.bank_id!r})")
    else:
        print(f"  [pi-lme] Backend: file ({workspace})")

    print(f"  [pi-lme] Loading LME from {args.lme_path}...")
    lme_data = json.loads(Path(args.lme_path).read_text())
    lme_data = _stratified_sample_lme(lme_data, args.max_samples)
    print(f"  [pi-lme] Scoring {len(lme_data)} samples (stratified across question_type)")

    provider = OpenAIProvider(api_key=api_key, model=args.model)
    # PR2.6 1b: optional stronger model for the agentic-reflect loop only.
    reflect_provider = (
        OpenAIProvider(api_key=api_key, model=args.reflect_model)
        if args.reflect_model and args.reflect_model != args.model
        else None
    )
    if reflect_provider is not None:
        print(
            f"  [pi-lme] Reflect model override: {args.reflect_model} (default: {args.model})",
        )
    judge = LongMemEvalJudge(provider, model=args.model) if not args.no_judge else None

    # ── Step 1: build trees ──
    trees: dict[str, dict] = {}
    t_build = time.monotonic()
    for sample in lme_data:
        sample_id = sample["question_id"]
        try:
            trees[sample_id] = await build_lme_tree(
                sample,
                sample_id,
                workspace,
                args.model,
                store=store,
                bank_id=args.bank_id,
                # PR2 commit A: entity + embedding pass at retain
                # (only fires for store backends on cache miss).
                provider=provider if store is not None else None,
                entity_model=args.model,
                embedding_model=None,
                mental_model_store=mental_model_store,
            )
        except Exception as exc:  # noqa: BLE001 — single-sample failure mustn't tank PR1 smoke
            print(f"  [pi-lme] Sample {sample_id}: tree build failed — {type(exc).__name__}: {exc}")
            trees[sample_id] = None  # mark for skip in scoring loop
    build_elapsed = time.monotonic() - t_build
    print(f"  [pi-lme] All trees ready in {build_elapsed:.1f}s")

    # ── Step 2: score ──
    results: list[dict] = []
    correct = 0
    by_category_correct: dict[str, int] = {}
    by_category_total: dict[str, int] = {}
    t_score = time.monotonic()
    for i, sample in enumerate(lme_data):
        sample_id = sample["question_id"]
        question = sample["question"]
        question_type = sample.get("question_type", "single-session-user")
        expected = sample.get("answer", "")

        conv_tree = trees.get(sample_id)
        if conv_tree is None:
            answer, line_nums = "(tree-build error)", []
        else:
            try:
                # PR2-D.1: LME's question_type is the canonical mode label
                # (single-session-user, multi-session, temporal-reasoning, etc.).
                # The new mode dispatch uses it directly — fixes the 0%
                # multi-session / temporal-reasoning / knowledge-update
                # categories from the PR2 LME gate.
                answer, line_nums = await _BENCH.answer_question(
                    provider,
                    conv_tree,
                    question,
                    args.model,
                    store=store,
                    bank_id=args.bank_id if store is not None else None,
                    cross_encoder=None,
                    category=question_type,
                    reflect_provider=reflect_provider,
                    mental_model_store=mental_model_store,
                )
            except Exception as exc:  # noqa: BLE001 — single-Q failure mustn't tank the run
                print(f"  [pi-lme] Q{i} ({sample_id}) failed: {exc}")
                answer, line_nums = "(error)", []

        is_correct: float = 0.0
        if judge is not None and answer not in {"(error)", "(tree-build error)"}:
            try:
                is_correct = await judge.score(
                    question_type=question_type,
                    question=question,
                    answer=str(expected),
                    response=answer,
                )
            except Exception as exc:  # noqa: BLE001 — judge failure shouldn't kill the run
                print(f"  [pi-lme] Q{i} judge failed: {exc}")
                is_correct = 0.0

        by_category_total[question_type] = by_category_total.get(question_type, 0) + 1
        if is_correct >= 1.0:
            correct += 1
            by_category_correct[question_type] = by_category_correct.get(question_type, 0) + 1

        results.append(
            {
                "question_id": sample_id,
                "question_type": question_type,
                "question": question,
                "expected": expected,
                "response": answer,
                "score": is_correct,
                "picked_lines": line_nums,
            }
        )

        if (i + 1) % 10 == 0:
            elapsed = time.monotonic() - t_score
            running = correct / (i + 1)
            print(
                f"  [pi-lme] {i + 1}/{len(lme_data)} — running accuracy {running:.3f} "
                f"({correct}/{i + 1}) — {elapsed:.0f}s elapsed",
            )

    # ── Step 3: report ──
    overall = correct / max(len(lme_data), 1)
    print()
    print(f"  [pi-lme] OVERALL ACCURACY: {overall:.4f} ({correct}/{len(lme_data)})")
    print()
    print("  [pi-lme] per-category:")
    for cat in sorted(by_category_total):
        n = by_category_total[cat]
        c = by_category_correct.get(cat, 0)
        print(f"    {cat:<26} {c}/{n}  ({c / max(n, 1):.4f})")

    # ── Step 4: dump results JSON for failure analysis ──
    out_path = workspace / f"results-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(
        json.dumps(
            {
                "overall_accuracy": overall,
                "evaluated_questions": len(lme_data),
                "correct": correct,
                "category_accuracy": {
                    cat: by_category_correct.get(cat, 0) / max(by_category_total[cat], 1) for cat in by_category_total
                },
                "model": args.model,
                "max_samples": args.max_samples,
                "backend": args.backend,
                "results": results,
            },
            indent=2,
            default=str,
        ),
    )
    print(f"  [pi-lme] Results written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
