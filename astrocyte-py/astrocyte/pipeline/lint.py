"""M8 W6: Wiki page lint pass.

LintEngine runs periodic checks over compiled wiki pages, detecting:

- **Stale**: one or more ``source_ids`` no longer exist in the VectorStore
  (the underlying raw memory was forgotten).  Action: flag for recompile.

- **Orphan**: *all* ``source_ids`` are gone — the page has no evidence left.
  Action: candidate for archival per memory-lifecycle.md §2.

- **Contradiction**: two pages in the same bank make conflicting claims.
  Detected via lightweight LLM call (opt-in; requires an LLMProvider).
  Action: flag both pages; surface in audit log.

The lint pass is additive — it only annotates pages with issues; it never
deletes or modifies page content.  Stale pages are marked in their metadata
and queued for the next compile cycle.  All issues are returned in
``LintResult`` for the caller to act on.

Design reference: docs/_design/llm-wiki-compile.md §7.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider, VectorStore, WikiStore
    from astrocyte.types import WikiPage

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

LintIssueKind = Literal["stale", "orphan", "contradiction"]


@dataclass
class LintIssue:
    """A single lint finding on a wiki page."""

    kind: LintIssueKind
    page_id: str
    bank_id: str
    detail: str  # Human-readable description of the issue
    action: Literal["recompile", "archive", "review"]  # Recommended action

    #: For ``contradiction`` issues: the other page involved (if known).
    peer_page_id: str | None = None


@dataclass
class LintResult:
    """Aggregate result of a lint pass over one bank."""

    bank_id: str
    pages_checked: int
    stale_count: int  # Pages with ≥1 missing source but ≥1 remaining
    orphan_count: int  # Pages with 0 remaining sources
    contradiction_count: int  # Page-pairs flagged by contradiction detection
    issues: list[LintIssue]
    elapsed_ms: int
    error: str | None = None


# ---------------------------------------------------------------------------
# LintEngine
# ---------------------------------------------------------------------------

_CONTRADICTION_SYSTEM_PROMPT = """\
You are a fact-checker. Given two wiki page excerpts, determine whether they \
make contradictory factual claims.

Respond with exactly one of:
- "CONTRADICTION: <brief explanation>" — if they conflict
- "OK" — if they do not conflict or the comparison is inconclusive

Output only the single-line verdict. No preamble.
"""


class LintEngine:
    """Runs lint checks over compiled wiki pages in a bank.

    Usage::

        engine = LintEngine(vector_store, wiki_store)
        result = await engine.run("user-alice")
        for issue in result.issues:
            print(issue.kind, issue.page_id, issue.action)

    Contradiction detection is opt-in (requires an LLM provider and makes
    one LLM call per page-pair)::

        engine = LintEngine(vector_store, wiki_store, llm_provider,
                            detect_contradictions=True)
    """

    def __init__(
        self,
        vector_store: VectorStore,
        wiki_store: WikiStore,
        llm_provider: LLMProvider | None = None,
        *,
        detect_contradictions: bool = False,
        contradiction_model: str | None = None,
        max_contradiction_pairs: int = 50,
    ) -> None:
        self._vs = vector_store
        self._ws = wiki_store
        self._llm = llm_provider
        self._detect_contradictions = detect_contradictions and llm_provider is not None
        self._contradiction_model = contradiction_model
        self._max_pairs = max_contradiction_pairs

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, bank_id: str) -> LintResult:
        """Run all lint checks over the wiki pages in *bank_id*.

        Args:
            bank_id: The bank to lint.

        Returns:
            :class:`LintResult` with issue lists and counts.  On unexpected
            error, ``error`` is set and ``issues`` reflects partial progress.
        """
        start = time.monotonic()
        issues: list[LintIssue] = []
        stale_count = 0
        orphan_count = 0
        contradiction_count = 0

        try:
            pages = await self._ws.list_pages(bank_id)
            pages_checked = len(pages)

            # Build the set of live raw-memory IDs once (scan the VectorStore
            # once per lint run rather than once per page).
            live_ids = await self._fetch_live_ids(bank_id)

            # ── Staleness / orphan checks ─────────────────────────────────
            for page in pages:
                issue = self._check_stale_or_orphan(page, bank_id, live_ids)
                if issue is not None:
                    issues.append(issue)
                    if issue.kind == "orphan":
                        orphan_count += 1
                    else:
                        stale_count += 1

            # ── Contradiction detection (opt-in) ──────────────────────────
            if self._detect_contradictions:
                contradiction_issues = await self._detect_page_contradictions(pages, bank_id)
                issues.extend(contradiction_issues)
                contradiction_count = len(contradiction_issues)

        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return LintResult(
                bank_id=bank_id,
                pages_checked=0,
                stale_count=stale_count,
                orphan_count=orphan_count,
                contradiction_count=contradiction_count,
                issues=issues,
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return LintResult(
            bank_id=bank_id,
            pages_checked=pages_checked,
            stale_count=stale_count,
            orphan_count=orphan_count,
            contradiction_count=contradiction_count,
            issues=issues,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Staleness / orphan
    # ------------------------------------------------------------------

    def _check_stale_or_orphan(
        self,
        page: WikiPage,
        bank_id: str,
        live_ids: set[str],
    ) -> LintIssue | None:
        """Return a stale or orphan issue if any source_ids are missing, else None."""
        if not page.source_ids:
            # No source_ids recorded — cannot determine staleness; skip
            return None

        missing = [sid for sid in page.source_ids if sid not in live_ids]
        if not missing:
            return None  # All sources still live

        if len(missing) == len(page.source_ids):
            # Every source is gone — orphan
            return LintIssue(
                kind="orphan",
                page_id=page.page_id,
                bank_id=bank_id,
                detail=(
                    f"All {len(page.source_ids)} source memories have been deleted "
                    f"or forgotten. Page has no remaining evidence."
                ),
                action="archive",
            )

        # Partial loss — stale
        return LintIssue(
            kind="stale",
            page_id=page.page_id,
            bank_id=bank_id,
            detail=(
                f"{len(missing)}/{len(page.source_ids)} source memories missing: "
                + ", ".join(missing[:5])
                + ("…" if len(missing) > 5 else "")
            ),
            action="recompile",
        )

    # ------------------------------------------------------------------
    # Contradiction detection
    # ------------------------------------------------------------------

    async def _detect_page_contradictions(
        self,
        pages: list[WikiPage],
        bank_id: str,
    ) -> list[LintIssue]:
        """Check page pairs for contradictory claims via LLM.

        Checks up to ``max_contradiction_pairs`` pairs (upper-triangular) to
        bound LLM cost.  Returns one ``LintIssue`` per contradicting pair
        (both pages referenced via ``peer_page_id``).
        """
        from astrocyte.types import Message

        issues: list[LintIssue] = []
        pairs_checked = 0

        for i in range(len(pages)):
            for j in range(i + 1, len(pages)):
                if pairs_checked >= self._max_pairs:
                    break

                pa, pb = pages[i], pages[j]
                pairs_checked += 1

                excerpt_a = f"## {pa.title}\n{pa.content[:400]}"
                excerpt_b = f"## {pb.title}\n{pb.content[:400]}"
                user_prompt = f"Page A:\n{excerpt_a}\n\nPage B:\n{excerpt_b}"

                try:
                    completion = await self._llm.complete(  # type: ignore[union-attr]
                        [
                            Message(role="system", content=_CONTRADICTION_SYSTEM_PROMPT),
                            Message(role="user", content=user_prompt),
                        ],
                        model=self._contradiction_model,
                        max_tokens=80,
                    )
                    verdict = completion.text.strip()
                except Exception:
                    continue  # LLM failure is non-fatal; skip pair

                if verdict.upper().startswith("CONTRADICTION"):
                    explanation = verdict[len("CONTRADICTION:"):].strip() if ":" in verdict else verdict
                    issues.append(
                        LintIssue(
                            kind="contradiction",
                            page_id=pa.page_id,
                            bank_id=bank_id,
                            detail=f"Contradiction with {pb.page_id!r}: {explanation}",
                            action="review",
                            peer_page_id=pb.page_id,
                        )
                    )
                    # Also flag the peer page
                    issues.append(
                        LintIssue(
                            kind="contradiction",
                            page_id=pb.page_id,
                            bank_id=bank_id,
                            detail=f"Contradiction with {pa.page_id!r}: {explanation}",
                            action="review",
                            peer_page_id=pa.page_id,
                        )
                    )

            if pairs_checked >= self._max_pairs:
                break

        return issues

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_live_ids(self, bank_id: str) -> set[str]:
        """Return the set of all raw-memory IDs currently in the VectorStore."""
        live: set[str] = set()
        offset = 0
        batch = 200
        while True:
            chunk = await self._vs.list_vectors(bank_id, offset=offset, limit=batch)
            if not chunk:
                break
            for item in chunk:
                # Only count raw memories — exclude compiled wiki pages
                if item.memory_layer != "compiled" and item.fact_type != "wiki":
                    live.add(item.id)
            if len(chunk) < batch:
                break
            offset += batch
        return live
