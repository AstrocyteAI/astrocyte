"""Premise extraction and verification for adversarial defense.

Adversarial questions in evaluation benchmarks (LoCoMo, etc.) follow
predictable shapes:

- **False premise**: "Why did Alice quit her job at Google?" — Alice
  never worked at Google. The LLM left to its own devices rationalizes
  the false premise instead of refusing.
- **Negative existence**: "Did Caroline ever go skiing?" — she didn't.
  The LLM invents a yes-answer from weakly-related hits.
- **Time-shift**: "What happened in 2024?" — the conversation was 2023.
  The LLM silently adopts the wrong date.
- **Cross-entity confusion**: "Did they eat there?" with multiple
  referents — the LLM picks the wrong referent.

The shared failure mode is that the LLM produces a confident answer
when the correct answer is "I don't know" or "the question presupposes
something I have no evidence for."

This module adds a **pre-loop verification step**: the question is
decomposed by the LLM into atomic claims, and each claim is verified
against memory via a focused recall. The results are returned as a
structured verdict that the agentic-reflect loop (or single-shot
synthesis) can incorporate into its evidence context. Crucially, when
ANY presupposition lacks supporting evidence with high confidence, the
caller can short-circuit to "insufficient evidence: <unsupported
claim>" without even running the main reflect loop.

Cost: 1 LLM call (premise extraction) + N focused recalls (one per
claim, typically 1–3). Cheap relative to a 10-iter agentic loop;
expensive relative to single-shot synth. Opt-in via config.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from astrocyte.types import MemoryHit, Message

_logger = logging.getLogger("astrocyte.premise_verification")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Premise:
    """A single atomic claim presupposed by a question."""

    claim: str
    #: Why the LLM extracted this premise — short reasoning. Useful for
    #: debugging false-positive presupposition detection.
    rationale: str = ""


@dataclass
class PremiseVerdict:
    """Result of verifying a single premise against memory.

    ``confidence`` reflects how strongly the retrieved evidence supports
    the claim:

    - ``>= min_confidence``: claim is supported by retrieved evidence
    - ``< min_confidence``: claim is unsupported; the question's
      presupposition fails and the caller should abstain
    - ``None``: verification was inconclusive (no recall ran, error)
    """

    premise: Premise
    supported: bool
    confidence: float
    evidence_ids: list[str]
    rationale: str = ""


@dataclass
class QuestionVerification:
    """End-to-end verification result for a question."""

    premises: list[Premise]
    verdicts: list[PremiseVerdict]
    #: True when EVERY premise is supported with sufficient confidence.
    #: Callers use this to decide whether to short-circuit before the
    #: main reflect loop runs.
    all_premises_supported: bool

    def unsupported_premises(self) -> list[PremiseVerdict]:
        return [v for v in self.verdicts if not v.supported]

    def short_circuit_message(self) -> str | None:
        """Return a "insufficient evidence" message when verification
        failed — or ``None`` when the question is safe to proceed."""
        if self.all_premises_supported:
            return None
        unsupported = self.unsupported_premises()
        if not unsupported:
            return None
        # Quote the first unsupported claim — its absence usually
        # explains the others (e.g. "Alice worked at Google" failing
        # makes "Alice quit Google" moot).
        first = unsupported[0]
        return (
            f"insufficient evidence: the question presupposes "
            f"'{first.premise.claim}' which is not supported by memory."
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_EXTRACTION_SYSTEM_PROMPT = """\
You decompose questions into the FACTUAL PRESUPPOSITIONS they assume \
to be true. The downstream system will verify each presupposition \
against a memory bank before the question is answered, so it can \
abstain when a presupposition is false.

Return a JSON array of {claim, rationale} objects. Each ``claim`` is \
ONE short atomic statement (≤ 15 words) that the question takes for \
granted. ``rationale`` is one sentence explaining why the question \
implies the claim.

Rules:
1. Decompose only PRESUPPOSITIONS — facts the question takes as given. \
For "Why did Alice quit Google?" the presuppositions are:
   - "Alice worked at Google"
   - "Alice quit"
   The "why" is the question's actual content; don't include it.
2. For yes/no questions ("Did X happen?") the presupposition is the \
participants/setting, NOT the event itself. The event IS what's being \
asked. Example: "Did Alice play tennis at the club?" presupposes \
"Alice was at the club" but NOT "Alice played tennis".
3. For pure-fact lookups with no embedded assumption ("What is X?", \
"When did Y happen?"), return [].
4. Maximum 3 presuppositions per question — pick the most central.
5. Output JSON only, no prose.
"""


def _build_extraction_user_prompt(question: str) -> str:
    return (
        f"Question: {question.strip()}\n\n"
        f"Presuppositions (JSON array):"
    )


_VERIFICATION_SYSTEM_PROMPT = """\
You judge whether retrieved memories support a specific factual claim.

Output a JSON object: {"supported": bool, "confidence": float, \
"evidence_ids": [...], "rationale": "<1 sentence>"}

Rules:
1. ``supported`` is True only when at least one memory directly \
attests the claim. Adjacent / topical relevance is NOT support.
2. ``confidence`` ∈ [0, 1]. ≥ 0.8 only when explicit. 0.5-0.8 for \
strongly implied. Below 0.5: not supported.
3. ``evidence_ids`` lists the memory IDs that attest the claim. Empty \
list when not supported.
4. Rationale is one sentence explaining the verdict.

Output JSON only.
"""


def _build_verification_user_prompt(claim: str, hits: list[MemoryHit]) -> str:
    if not hits:
        return (
            f"Claim: {claim}\n\n"
            f"Retrieved memories: (none)\n\n"
            f"Verdict (JSON):"
        )
    lines = [f"Claim: {claim}", "", "Retrieved memories:"]
    for hit in hits:
        text = (hit.text or "").strip()
        if len(text) > 400:
            text = text[:397] + "..."
        lines.append(f"[{hit.memory_id}] {text}")
    lines.extend(["", "Verdict (JSON):"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _parse_json_array(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match is None:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [p for p in payload if isinstance(p, dict)]


def _parse_json_object(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Extraction + verification
# ---------------------------------------------------------------------------


async def extract_premises(question: str, llm_provider) -> list[Premise]:
    """Decompose a question into the factual claims it presupposes.

    Returns ``[]`` for pure fact-lookup questions (no embedded
    assumption) and on any LLM/parse failure — caller treats empty list
    as "no presupposition to verify, proceed normally."
    """
    if not question or not question.strip():
        return []
    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                Message(role="user", content=_build_extraction_user_prompt(question)),
            ],
            max_tokens=512,
            temperature=0.0,
        )
    except Exception as exc:
        _logger.warning("premise extraction LLM call failed (%s)", exc)
        return []

    parsed = _parse_json_array(completion.text)
    out: list[Premise] = []
    for item in parsed[:3]:
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        rationale = str(item.get("rationale") or "").strip()
        out.append(Premise(claim=claim, rationale=rationale))
    return out


RecallFn = Callable[[str, int], Awaitable[list[MemoryHit]]]


async def verify_premise(
    premise: Premise,
    recall_fn: RecallFn,
    llm_provider,
    *,
    recall_max_results: int = 5,
    min_confidence: float = 0.6,
) -> PremiseVerdict:
    """Verify one premise against memory via focused recall + LLM judge.

    The recall_fn is the orchestrator's existing recall (RRF + spread
    + cross-encoder rerank + tag scope), parameterized by the claim
    text. The judge LLM call is structured-JSON to keep parsing robust.
    """
    try:
        hits = await recall_fn(premise.claim, recall_max_results)
    except Exception as exc:
        _logger.warning("premise verification recall failed (%s)", exc)
        return PremiseVerdict(
            premise=premise, supported=False, confidence=0.0,
            evidence_ids=[], rationale=f"recall failed: {exc}",
        )

    try:
        completion = await llm_provider.complete(
            [
                Message(role="system", content=_VERIFICATION_SYSTEM_PROMPT),
                Message(role="user", content=_build_verification_user_prompt(premise.claim, hits)),
            ],
            max_tokens=256,
            temperature=0.0,
        )
    except Exception as exc:
        _logger.warning("premise verification LLM call failed (%s)", exc)
        return PremiseVerdict(
            premise=premise, supported=False, confidence=0.0,
            evidence_ids=[], rationale=f"judge LLM failed: {exc}",
        )

    parsed = _parse_json_object(completion.text) or {}
    supported_raw = parsed.get("supported", False)
    supported = bool(supported_raw) if not isinstance(supported_raw, str) else \
        supported_raw.lower() == "true"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence_ids_raw = parsed.get("evidence_ids") or []
    evidence_ids = [str(e) for e in evidence_ids_raw if e] if isinstance(evidence_ids_raw, list) else []
    rationale = str(parsed.get("rationale") or "").strip()

    # Final supported flag combines the LLM's verdict with the
    # confidence threshold — defensive against the LLM saying
    # "supported: true" with low confidence.
    final_supported = supported and confidence >= min_confidence

    return PremiseVerdict(
        premise=premise,
        supported=final_supported,
        confidence=confidence,
        evidence_ids=evidence_ids,
        rationale=rationale,
    )


async def verify_question(
    question: str,
    *,
    recall_fn: RecallFn,
    llm_provider,
    recall_max_results: int = 5,
    min_confidence: float = 0.6,
) -> QuestionVerification:
    """End-to-end verification: extract premises, verify each.

    Returns a :class:`QuestionVerification` whose
    ``short_circuit_message()`` is non-None when the question's
    presuppositions fail.

    No-presupposition questions return ``all_premises_supported=True``
    so the caller proceeds normally.
    """
    premises = await extract_premises(question, llm_provider)
    if not premises:
        return QuestionVerification(
            premises=[], verdicts=[], all_premises_supported=True,
        )

    # Run verifications in parallel — each premise's recall + judge
    # are independent, so latency is bounded by the slowest.
    import asyncio
    verdicts = await asyncio.gather(*[
        verify_premise(
            p, recall_fn, llm_provider,
            recall_max_results=recall_max_results,
            min_confidence=min_confidence,
        )
        for p in premises
    ])

    all_supported = all(v.supported for v in verdicts)
    return QuestionVerification(
        premises=list(premises),
        verdicts=list(verdicts),
        all_premises_supported=all_supported,
    )
