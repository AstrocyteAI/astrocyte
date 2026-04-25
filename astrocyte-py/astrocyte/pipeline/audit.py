"""M10: Gap-analysis audit pipeline.

``run_audit()`` takes a set of memories already retrieved for a scope,
calls the LLM audit judge, and returns a structured ``AuditResult``.

The judge is a single-shot LLM call that:
1. Receives all retrieved memories as context.
2. Identifies topics that are absent or under-covered.
3. Returns a JSON object with ``gaps`` (list) and ``coverage_score`` (float).

The module is intentionally narrow: memory retrieval and token budgeting
happen in the orchestrator; this module owns only the prompt + parse logic
so it can be unit-tested against a ``MockLLMProvider`` without any store.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Literal

from astrocyte.types import AuditResult, GapItem, MemoryHit, Message, RecallTrace

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider

_logger = logging.getLogger("astrocyte.audit")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a memory-gap analyst.  You will be given:
- A SCOPE: the topic or question the user cares about.
- MEMORIES: a numbered list of facts the agent currently knows.

Your task is to identify what is MISSING or UNDER-COVERED — knowledge that
would be needed to answer questions about the scope reliably but is absent
or too thin in the provided memories.

Return ONLY valid JSON in this exact shape (no markdown, no preamble):

{
  "coverage_score": <float 0.0–1.0>,
  "gaps": [
    {"topic": "<short label>", "severity": "<high|medium|low>", "reason": "<one sentence>"},
    ...
  ]
}

Scoring guide for coverage_score:
  1.0 — comprehensive; the scope is well covered from multiple angles.
  0.7 — good; most key facts are present, minor gaps only.
  0.5 — partial; useful but material gaps exist.
  0.3 — sparse; only surface-level coverage.
  0.0 — no relevant memories at all.

Gap severity guide:
  high   — absence would likely produce a wrong or confidently-wrong answer.
  medium — partial coverage; answer would be incomplete.
  low    — nuance or context is missing but a reasonable answer is still possible.

If there are no gaps, return an empty list for "gaps".
Do not fabricate memories. Only identify gaps relative to what was provided.\
"""


def _render_memories(memories: list[MemoryHit]) -> str:
    if not memories:
        return "(no memories retrieved)"
    lines: list[str] = []
    for i, m in enumerate(memories, 1):
        ts = f" [{m.retained_at:%Y-%m-%d}]" if m.retained_at else ""
        lines.append(f"[{i}]{ts} {m.text}")
    return "\n".join(lines)


def _parse_response(raw: str, scope: str, bank_id: str, memories_scanned: int, trace: RecallTrace | None) -> AuditResult:
    """Parse LLM JSON response into AuditResult, with graceful fallback."""
    raw = raw.strip()
    # Strip markdown fences if the model wrapped the JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("audit judge returned non-JSON: %r", raw[:200])
        return AuditResult(
            scope=scope,
            bank_id=bank_id,
            gaps=[GapItem(topic="(parse error)", severity="low", reason="Audit judge returned non-JSON output.")],
            coverage_score=0.0 if not memories_scanned else 0.5,
            memories_scanned=memories_scanned,
            trace=trace,
        )

    raw_score = data.get("coverage_score", 0.5)
    try:
        coverage_score = max(0.0, min(1.0, float(raw_score)))
    except (TypeError, ValueError):
        coverage_score = 0.5

    gaps: list[GapItem] = []
    for g in data.get("gaps", []):
        if not isinstance(g, dict):
            continue
        topic = str(g.get("topic", "unknown"))
        severity_raw = str(g.get("severity", "low")).lower()
        severity: Literal["high", "medium", "low"] = (
            severity_raw if severity_raw in ("high", "medium", "low") else "low"
        )
        reason = str(g.get("reason", ""))
        gaps.append(GapItem(topic=topic, severity=severity, reason=reason))

    return AuditResult(
        scope=scope,
        bank_id=bank_id,
        gaps=gaps,
        coverage_score=coverage_score,
        memories_scanned=memories_scanned,
        trace=trace,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_audit(
    scope: str,
    bank_id: str,
    memories: list[MemoryHit],
    llm_provider: LLMProvider,
    *,
    trace: RecallTrace | None = None,
) -> AuditResult:
    """Call the LLM audit judge and return a structured ``AuditResult``.

    Args:
        scope: The topic or question to audit coverage for.
        bank_id: The bank that was searched (echoed into result).
        memories: Retrieved memories to pass as context to the judge.
            The caller is responsible for retrieving and budget-trimming them.
        llm_provider: LLM to use for the audit judge call.
        trace: Optional recall trace to embed in the result.

    Returns:
        :class:`~astrocyte.types.AuditResult` with gaps and coverage score.
    """
    if not memories:
        # No memories at all → zero coverage, one high-severity gap
        return AuditResult(
            scope=scope,
            bank_id=bank_id,
            gaps=[GapItem(
                topic=scope,
                severity="high",
                reason="No memories were found in this bank for the given scope.",
            )],
            coverage_score=0.0,
            memories_scanned=0,
            trace=trace,
        )

    memory_block = _render_memories(memories)
    user_content = f"SCOPE: {scope}\n\nMEMORIES:\n{memory_block}"

    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=user_content),
    ]

    try:
        completion = await llm_provider.complete(messages, max_tokens=1024, temperature=0.0)
        raw = completion.text or ""
    except Exception as exc:
        _logger.warning("audit judge LLM call failed: %s", exc)
        raw = ""

    return _parse_response(raw, scope, bank_id, len(memories), trace)
