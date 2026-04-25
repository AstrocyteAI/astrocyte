"""M8 W7: Wiki compile A/B evaluation harness.

Measures the recall quality improvement produced by the wiki compile tier,
comparing two conditions head-to-head on a controlled set of cases:

- **Baseline**: plain ``brain.recall()`` over raw memories — no compile, no
  wiki tier.
- **Wiki**: ``CompileEngine.run()`` followed by ``brain.recall()`` with the
  wiki tier enabled (``PipelineOrchestrator.wiki_store`` wired up).

The harness is designed to run without the real LongMemEval dataset.  Callers
supply ``WikiEvalCase`` objects; the built-in fixture covers the two categories
most expected to benefit (``knowledge-update`` and ``multi-session``).

Design reference: docs/_design/llm-wiki-compile.md §9.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrocyte.pipeline.compile import CompileEngine
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, InMemoryWikiStore
from astrocyte.types import RecallRequest, RetainRequest

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

_logger = logging.getLogger("astrocyte.eval.wiki")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WikiEvalCase:
    """One A/B eval case.

    A case retains a set of memories, compiles them under ``scope``, then
    issues ``query`` and checks whether the top hits contain at least one of
    ``expected_keywords``.
    """

    case_id: str
    category: str  # "knowledge-update" | "multi-session" | "extraction"
    retains: list[str]  # Memories stored before eval (ordered; later facts supersede earlier)
    scope: str  # compile scope label (tag or explicit)
    query: str  # Recall question
    expected_keywords: list[str]  # Hit is correct if its text contains any keyword (case-insensitive)
    tags: list[str] | None = None  # Tags applied to all retained memories


@dataclass
class WikiABCaseResult:
    """Per-case A/B result."""

    case_id: str
    category: str
    baseline_correct: bool
    wiki_correct: bool
    baseline_top_text: str  # Text of highest-scoring baseline hit (for debugging)
    wiki_top_text: str  # Text of highest-scoring wiki hit (for debugging)
    wiki_tier_used: bool  # Whether wiki tier fired for this case


@dataclass
class WikiABResult:
    """Aggregate result from :meth:`WikiCompileEvalHarness.run_ab`.

    The two key metrics for the shipping gate (§9.2):
    - ``lift >= 0.10``:     ≥10 percentage-point absolute improvement.
    - ``meets_gate``:       wiki never worse than baseline (no regression).
    """

    baseline_accuracy: float
    wiki_accuracy: float
    lift: float  # wiki_accuracy - baseline_accuracy
    total_cases: int
    baseline_correct: int
    wiki_correct: int
    per_case: list[WikiABCaseResult] = field(default_factory=list)
    category_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def meets_gate(self) -> bool:
        """Regression gate: wiki accuracy must be >= baseline on this run."""
        return self.wiki_accuracy >= self.baseline_accuracy

    @property
    def meets_lift_target(self) -> bool:
        """v0.9 shipping target: ≥10pp absolute lift on combined cases."""
        return self.lift >= 0.10


# ---------------------------------------------------------------------------
# Accuracy scoring
# ---------------------------------------------------------------------------


def _is_correct(hits_text: list[str], expected_keywords: list[str]) -> bool:
    """Return True if any hit contains any expected keyword (case-insensitive)."""
    lower_hits = [t.lower() for t in hits_text]
    for kw in expected_keywords:
        kw_lower = kw.lower()
        if any(kw_lower in t for t in lower_hits):
            return True
    return False


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class WikiCompileEvalHarness:
    """A/B harness: recall quality with vs without wiki compile.

    Shares a single ``InMemoryVectorStore`` across both conditions so
    the retained memories are identical.  The wiki condition additionally
    runs ``CompileEngine`` and enables the wiki tier on its orchestrator.

    Args:
        llm_provider: The LLM provider used for both embedding and synthesis.
            Use a mock provider in tests; a real provider when measuring
            against real benchmarks.
        wiki_confidence_threshold: Passed through to
            :class:`~astrocyte.pipeline.orchestrator.PipelineOrchestrator`
            for the wiki-tier condition.
        top_k: Number of recall hits to examine for scoring.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        *,
        wiki_confidence_threshold: float = 0.7,
        top_k: int = 5,
    ) -> None:
        self._llm = llm_provider
        self._wiki_threshold = wiki_confidence_threshold
        self._top_k = top_k

    async def run_ab(
        self,
        cases: list[WikiEvalCase],
        bank_id: str = "eval-wiki-ab",
    ) -> WikiABResult:
        """Run both recall conditions on all cases and return the A/B result.

        The method is stateless across calls — each invocation creates fresh
        in-memory stores.

        Args:
            cases: List of :class:`WikiEvalCase` objects to evaluate.
            bank_id: Bank identifier used for all retained memories.

        Returns:
            :class:`WikiABResult` with accuracy, lift, and per-case detail.
        """
        if not cases:
            return WikiABResult(
                baseline_accuracy=0.0,
                wiki_accuracy=0.0,
                lift=0.0,
                total_cases=0,
                baseline_correct=0,
                wiki_correct=0,
            )

        vs = InMemoryVectorStore()
        ws = InMemoryWikiStore()

        # Two orchestrators share the same VectorStore so retained memories
        # are identical; only the wiki tier wiring differs.
        baseline_orch = PipelineOrchestrator(vs, self._llm)
        wiki_orch = PipelineOrchestrator(
            vs,
            self._llm,
            wiki_store=ws,
            wiki_confidence_threshold=self._wiki_threshold,
        )

        # ── Phase 1: Retain all memories ──────────────────────────────────
        for case in cases:
            tags = list(case.tags or [])
            # Tag with scope so CompileEngine's tag-grouping pass can
            # discover it without needing DBSCAN.
            if case.scope not in tags:
                tags.append(case.scope)

            for memory_text in case.retains:
                await baseline_orch.retain(
                    RetainRequest(
                        content=memory_text,
                        bank_id=bank_id,
                        tags=tags,
                    )
                )

        # ── Phase 2: Compile (wiki condition only) ─────────────────────────
        engine = CompileEngine(
            vector_store=vs,
            llm_provider=self._llm,
            wiki_store=ws,
        )
        compile_result = await engine.run(bank_id)
        _logger.debug(
            "Compile: %d pages created, %d updated, %d noise",
            compile_result.pages_created,
            compile_result.pages_updated,
            compile_result.noise_memories,
        )

        # ── Phase 3: Recall A/B ───────────────────────────────────────────
        case_results: list[WikiABCaseResult] = []
        baseline_correct_total = 0
        wiki_correct_total = 0

        for case in cases:
            # Baseline recall
            b_result = await baseline_orch.recall(
                RecallRequest(query=case.query, bank_id=bank_id, max_results=self._top_k)
            )
            b_texts = [h.text for h in b_result.hits]
            b_correct = _is_correct(b_texts, case.expected_keywords)

            # Wiki recall
            w_result = await wiki_orch.recall(
                RecallRequest(query=case.query, bank_id=bank_id, max_results=self._top_k)
            )
            w_texts = [h.text for h in w_result.hits]
            w_correct = _is_correct(w_texts, case.expected_keywords)

            wiki_tier_fired = bool(
                w_result.trace and w_result.trace.wiki_tier_used
            )

            if b_correct:
                baseline_correct_total += 1
            if w_correct:
                wiki_correct_total += 1

            case_results.append(
                WikiABCaseResult(
                    case_id=case.case_id,
                    category=case.category,
                    baseline_correct=b_correct,
                    wiki_correct=w_correct,
                    baseline_top_text=b_texts[0] if b_texts else "",
                    wiki_top_text=w_texts[0] if w_texts else "",
                    wiki_tier_used=wiki_tier_fired,
                )
            )

        # ── Phase 4: Aggregate ────────────────────────────────────────────
        n = len(cases)
        baseline_acc = baseline_correct_total / n
        wiki_acc = wiki_correct_total / n
        lift = wiki_acc - baseline_acc

        # Per-category breakdown (mirrors LongMemEval category_accuracy)
        cat_baseline: dict[str, int] = {}
        cat_wiki: dict[str, int] = {}
        cat_total: dict[str, int] = {}
        for cr in case_results:
            c = cr.category
            cat_total[c] = cat_total.get(c, 0) + 1
            if cr.baseline_correct:
                cat_baseline[c] = cat_baseline.get(c, 0) + 1
            if cr.wiki_correct:
                cat_wiki[c] = cat_wiki.get(c, 0) + 1

        category_breakdown: dict[str, dict[str, Any]] = {}
        for cat in cat_total:
            total_cat = cat_total[cat]
            category_breakdown[cat] = {
                "baseline_accuracy": cat_baseline.get(cat, 0) / total_cat,
                "wiki_accuracy": cat_wiki.get(cat, 0) / total_cat,
                "lift": (cat_wiki.get(cat, 0) - cat_baseline.get(cat, 0)) / total_cat,
                "total": total_cat,
            }

        return WikiABResult(
            baseline_accuracy=baseline_acc,
            wiki_accuracy=wiki_acc,
            lift=lift,
            total_cases=n,
            baseline_correct=baseline_correct_total,
            wiki_correct=wiki_correct_total,
            per_case=case_results,
            category_breakdown=category_breakdown,
        )


# ---------------------------------------------------------------------------
# Regression gate
# ---------------------------------------------------------------------------


def assert_wiki_regression_gate(
    result: WikiABResult,
    *,
    categories: list[str] | None = None,
) -> None:
    """Assert the wiki tier meets the regression gate.

    Raises:
        AssertionError: When wiki accuracy is below baseline accuracy for any
            of the monitored categories.  The error message names the failing
            categories and their per-category accuracy figures so CI output is
            actionable.

    Args:
        result: Output of :meth:`WikiCompileEvalHarness.run_ab`.
        categories: Subset of categories to gate on.  Defaults to
            ``["knowledge-update", "multi-session"]`` per §9.2.
    """
    gate_cats = categories or ["knowledge-update", "multi-session"]
    failures: list[str] = []

    for cat in gate_cats:
        if cat not in result.category_breakdown:
            continue  # Category not represented in this run — skip
        bd = result.category_breakdown[cat]
        if bd["wiki_accuracy"] < bd["baseline_accuracy"]:
            failures.append(
                f"  {cat}: wiki={bd['wiki_accuracy']:.1%} < baseline={bd['baseline_accuracy']:.1%}"
                f" (lift={bd['lift']:+.1%})"
            )

    if failures:
        raise AssertionError(
            "Wiki tier regression gate failed — wiki accuracy is below baseline:\n"
            + "\n".join(failures)
        )


# ---------------------------------------------------------------------------
# Built-in synthetic fixture
# ---------------------------------------------------------------------------


def make_synthetic_cases() -> list[WikiEvalCase]:
    """Return a small synthetic fixture exercising knowledge-update and multi-session.

    Designed so that:
    - ``knowledge-update`` cases have an *old* fact and a *new* (superseding)
      fact.  Baseline recall may surface the old fact; the compiled wiki page
      synthesises the new fact as primary.
    - ``multi-session`` cases scatter related facts across separate retain
      calls.  The compiled wiki page fuses them; baseline must retrieve each
      fragment independently.

    No external data files are required — suitable for unit tests and CI.
    """
    return [
        # ── Knowledge-update: employer change ─────────────────────────────
        WikiEvalCase(
            case_id="ku-employer-alice",
            category="knowledge-update",
            retains=[
                "Alice previously worked at Google as a software engineer.",
                "Alice recently joined Meta as a senior ML engineer.",
            ],
            scope="alice",
            query="Where does Alice currently work?",
            expected_keywords=["Meta"],
        ),
        # ── Knowledge-update: project status ──────────────────────────────
        WikiEvalCase(
            case_id="ku-project-status",
            category="knowledge-update",
            retains=[
                "Project Phoenix was paused in Q3 due to budget constraints.",
                "Project Phoenix has resumed and is now targeting Q2 launch.",
            ],
            scope="project-phoenix",
            query="What is the current status of Project Phoenix?",
            expected_keywords=["resumed", "Q2"],
        ),
        # ── Multi-session: person profile ─────────────────────────────────
        WikiEvalCase(
            case_id="ms-person-bob",
            category="multi-session",
            retains=[
                "Bob is a data scientist at Acme Corp.",
                "Bob specialises in time-series forecasting and anomaly detection.",
                "Bob presented at NeurIPS 2023 on energy forecasting.",
            ],
            scope="bob",
            query="What is Bob's area of expertise?",
            expected_keywords=["forecasting", "anomaly"],
        ),
        # ── Multi-session: incident timeline ──────────────────────────────
        WikiEvalCase(
            case_id="ms-incident-db",
            category="multi-session",
            retains=[
                "Database latency spiked at 14:00 UTC on Monday.",
                "Root cause identified: runaway query from analytics job.",
                "Fix deployed: added query timeout; latency returned to normal by 16:30.",
            ],
            scope="incident-db",
            query="What caused the database incident on Monday?",
            expected_keywords=["query", "analytics"],
        ),
        # ── Knowledge-update: location ────────────────────────────────────
        WikiEvalCase(
            case_id="ku-location-hq",
            category="knowledge-update",
            retains=[
                "Acme Corp headquarters was located in San Francisco.",
                "Acme Corp has moved its headquarters to Austin, Texas.",
            ],
            scope="acme-corp",
            query="Where is Acme Corp headquartered?",
            expected_keywords=["Austin", "Texas"],
        ),
    ]
