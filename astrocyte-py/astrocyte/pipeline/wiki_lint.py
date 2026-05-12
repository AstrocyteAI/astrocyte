"""Wiki lint pass — Karpathy's periodic "lint" op (M12.5).

Karpathy's LLM Wiki spec includes a lint operation that checks compiled
pages for contradictions, stale claims, orphans, missing cross-links,
and data gaps. This module implements **contradiction detection** for
v1 — the highest-bench-leverage check.

Mechanism: per WikiPage, send (page content + top-K facts in same scope)
to an LLM judge with a yes/no contradiction prompt. If the judge flags a
contradiction, the page is marked unclean and callers can filter it out
of the synth context.

The lint is **stateless per call** — callers either (a) run once per
bank and cache the report, or (b) run inline per-question on the
small set of wiki hits actually surfaced by recall. The latter pays
~1-2 LLM calls per question instead of N upfront but only lints
pages that would have been seen.

Generic across benches — operates on WikiPage shape (title + content +
scope) without bench-specific assumptions.

See:
- ``docs/_design/llm-wiki-compile.md`` §7 (lint design)
- Karpathy gist: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

logger = logging.getLogger("astrocyte.pipeline.wiki_lint")


@dataclass
class WikiLintIssue:
    """An issue detected by the lint pass for one wiki page."""
    page_id: str
    kind: str  # 'contradicted' | 'stale' | 'orphan'
    detail: str = ""


@dataclass
class WikiLintReport:
    """Aggregate of lint issues for a set of pages."""
    bank_id: str
    issues: list[WikiLintIssue] = field(default_factory=list)

    def is_clean(self, page_id: str) -> bool:
        """True if no issues were flagged for this page_id."""
        return not any(i.page_id == page_id for i in self.issues)

    def kinds_for(self, page_id: str) -> set[str]:
        """Set of issue kinds flagged for this page."""
        return {i.kind for i in self.issues if i.page_id == page_id}


_CONTRADICTION_PROMPT = """You are auditing a knowledge-base entry against the source facts it summarises.

Knowledge entry (titled "{title}"):
{content}

Source facts (each independently true, most relevant first):
{facts_block}

Does the entry contain ANY claim that DISAGREES with one of the source facts in a way that matters for answering questions? A contradiction is when the entry directly asserts something the facts refute. Vagueness or omission is NOT contradiction.

Respond with JSON only:
{{"verdict": "OK" or "CONTRADICTED", "explanation": "one sentence; only when CONTRADICTED"}}"""


async def lint_one_wiki(
    *,
    page_id: str,
    title: str,
    content: str,
    facts: list[str],
    llm_provider: LLMProvider,
    model: str = "gpt-4o-mini",
) -> WikiLintIssue | None:
    """Lint a single wiki page against pre-fetched facts.

    Returns:
        ``WikiLintIssue`` with ``kind='contradicted'`` if the LLM judge
        flags a contradiction, ``None`` otherwise (clean / no facts /
        LLM failure — resilient).
    """
    if not facts or not content.strip():
        return None

    facts_block = "\n".join(f"{i + 1}. {fact}" for i, fact in enumerate(facts))
    prompt = _CONTRADICTION_PROMPT.format(
        title=title, content=content, facts_block=facts_block,
    )

    try:
        completion = await llm_provider.complete(
            prompt, model=model,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "wiki_lint llm call failed for page=%s: %s: %s",
            page_id, type(exc).__name__, exc,
        )
        return None

    try:
        parsed = json.loads(completion.text)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("wiki_lint json parse failed for page=%s", page_id)
        return None

    verdict = str(parsed.get("verdict", "")).strip().upper()
    if verdict == "CONTRADICTED":
        return WikiLintIssue(
            page_id=page_id,
            kind="contradicted",
            detail=str(parsed.get("explanation", "")).strip(),
        )
    return None


async def lint_wiki_pages(
    *,
    pages: list[tuple[str, str, str, list[str]]],
    llm_provider: LLMProvider,
    bank_id: str,
    model: str = "gpt-4o-mini",
) -> WikiLintReport:
    """Batch lint a set of pages.

    Args:
        pages: List of ``(page_id, title, content, facts)`` tuples. The
            tuple shape lets callers adapt from WikiPage / WikiPageHit
            without forcing a particular dataclass.
        llm_provider: Provider for the contradiction-detection LLM call.
        bank_id: Bank scope recorded on the report.
        model: LLM model for the judge.

    Returns:
        ``WikiLintReport`` with one issue per contradicted page. Pages
        with no facts or that fail the LLM call are silently skipped
        (treated as clean — fail-open).
    """
    report = WikiLintReport(bank_id=bank_id)
    for page_id, title, content, facts in pages:
        if not page_id:
            continue
        # Orphan: page has no source facts to anchor against. The
        # caller can still treat orphans as clean for filtering; we
        # flag for visibility.
        if not facts:
            report.issues.append(WikiLintIssue(
                page_id=page_id, kind="orphan",
                detail="no facts in scope",
            ))
            continue
        issue = await lint_one_wiki(
            page_id=page_id, title=title, content=content,
            facts=facts, llm_provider=llm_provider, model=model,
        )
        if issue is not None:
            report.issues.append(issue)
    return report
