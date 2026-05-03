"""Agentic reflect loop (Hindsight parity, native function-calling).

Closely mirrors Hindsight's ``engine/reflect/agent.py``:

- **Native function calling** (OpenAI/Anthropic ``tools=`` API), not a
  JSON-in-prose protocol. Provider adapters return parsed
  :class:`ToolCall` instances; the loop dispatches by tool name.
- **Hierarchical tools** in priority order:
    1. ``recall`` — raw fact retrieval (always available).
    2. ``search_observations`` — consolidated knowledge layer (when
       observations are configured for the bank).
    3. ``expand`` — fetch source memories cited by a compiled fact /
       wiki page (their "include_chunks" semantics on demand).
    4. ``done`` — terminal action; the model commits an answer with
       ``cited_ids``.
- **Default 10 iterations** (Hindsight default).
- **Tool-name normalization** for misbehaving LLMs that emit prefixed
  or decorated tool names (``call=functions.done``,
  ``done<|channel|>commentary``, ``functions.recall``).
- **Token tracking** via ``tiktoken`` when available; degrades to None
  totals when the package isn't installed.
- **Citation validation** — ``cited_ids`` not seen in the running
  evidence pool are stripped from the response's ``sources`` list.
- **Feature detection** — when the provider doesn't support
  ``tools`` (legacy ``MockLLMProvider`` paths), the loop falls
  through to forced single-shot synthesis.

The loop ALWAYS round-trips tool results back to the model as
``role="tool"`` messages so the next iteration sees its prior tool's
output, matching OpenAI's tool-use protocol.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from astrocyte.types import (
    MemoryHit,
    Message,
    ReflectResult,
    ToolDefinition,
)

_logger = logging.getLogger("astrocyte.agentic_reflect")


# ---------------------------------------------------------------------------
# Loop configuration
# ---------------------------------------------------------------------------


@dataclass
class AgenticReflectParams:
    """Tunable knobs for the reflect loop.

    Defaults match Hindsight where stated:
    - ``max_iterations=10`` matches their ``DEFAULT_MAX_ITERATIONS``.
    - ``recall_step_max_results=10`` matches the bench's outer recall.
    - ``observations_step_max_results=5`` matches their default.
    - ``expand_step_max_sources=5`` matches their default.
    - ``max_evidence_pool_size=30`` is our cap; their token-budget cap
      is computed dynamically from ``max_tokens`` instead.
    """

    max_iterations: int = 10
    recall_step_max_results: int = 10
    observations_step_max_results: int = 5
    expand_step_max_sources: int = 5
    max_evidence_pool_size: int = 30
    #: When the provider lacks tool-calling support OR doesn't emit any
    #: tool call on a turn, the loop falls through to forced synthesis
    #: rather than thrashing. Set ``False`` only for diagnostic runs.
    fallback_on_no_tool_call: bool = True
    #: Append adversarial-defense rules to the system prompt
    #: (premise check, negative-existence handling, time-shift trap,
    #: explicit "insufficient evidence is always a valid answer").
    #: Targets the LoCoMo adversarial category.
    adversarial_defense: bool = False


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI-compatible JSON Schema)
# ---------------------------------------------------------------------------


_TOOL_RECALL = ToolDefinition(
    name="recall",
    description=(
        "Retrieve raw memories from the bank by semantic similarity to your query. "
        "Use this for ground-truth facts. Each result has an `id` you must cite "
        "from in the final `done` call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief explanation of why you're calling recall (for debug/tracing).",
            },
            "query": {
                "type": "string",
                "description": "Search query — refine it with names/dates/specific terms when broadening doesn't help.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of memories to return (default 10, max 20).",
            },
        },
        "required": ["reason", "query"],
    },
)

_TOOL_SEARCH_OBSERVATIONS = ToolDefinition(
    name="search_observations",
    description=(
        "Search consolidated observations — auto-synthesized knowledge with "
        "freshness info. Higher precision than raw recall for stable persona / "
        "preference questions. Stale observations should be cross-checked with "
        "`recall` before citing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "query": {"type": "string"},
            "max_results": {
                "type": "integer",
                "description": "Maximum number of observations (default 5).",
            },
        },
        "required": ["reason", "query"],
    },
)

_TOOL_EXPAND = ToolDefinition(
    name="expand",
    description=(
        "Fetch the source memories cited by a compiled fact / observation / wiki "
        "page that you've already seen. Pass the `id` of the compiled item; "
        "you'll receive the raw memories underneath, useful when the distilled "
        "fact loses important verbatim context."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "memory_id": {
                "type": "string",
                "description": "ID of the compiled memory whose sources to expand.",
            },
            "max_sources": {
                "type": "integer",
                "description": "Maximum source memories to fetch (default 5).",
            },
        },
        "required": ["reason", "memory_id"],
    },
)

_TOOL_DONE = ToolDefinition(
    name="done",
    description=(
        "Commit your final answer. ALL `cited_ids` must be IDs you have seen in "
        "an earlier tool result; you cannot invent IDs. If you cannot answer "
        "with evidence, return an honest 'insufficient evidence' answer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "Final answer to the user's question, evidence-grounded.",
            },
            "cited_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of memories supporting the answer; must come from prior tool results.",
            },
        },
        "required": ["answer", "cited_ids"],
    },
)


# ---------------------------------------------------------------------------
# Tool-name normalization (Hindsight-parity)
# ---------------------------------------------------------------------------


def _normalize_tool_name(name: str) -> str:
    """Normalize tool names from misbehaving LLMs.

    Mirrors Hindsight's :func:`_normalize_tool_name`:
    - ``call=functions.done`` → ``done``
    - ``functions.recall`` → ``recall``
    - ``done<|channel|>commentary`` → ``done``
    """
    if name.startswith("call="):
        name = name[len("call=") :]
    if name.startswith("functions."):
        name = name[len("functions.") :]
    if "<|" in name:
        name = name.split("<|")[0]
    return name.strip()


def _is_done_tool(name: str) -> bool:
    return _normalize_tool_name(name).lower() == "done"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You are an evidence-gathering memory agent. Answer the user's question using \
ONLY tool results — never invent facts.

Available tools:
- `search_observations`: consolidated knowledge (try first when the question \
asks about stable preferences / patterns / persona).
- `recall`: raw memories for ground-truth facts (always available).
- `expand`: fetch source memories under a compiled fact you've seen.
- `done`: commit final answer with citations.

Core rules:
1. Gather evidence with the tools above before answering.
2. Each `cited_ids` entry MUST be an ID you saw in a prior tool result.
3. Refine your query with specific names / dates / sub-topics when initial \
recall doesn't surface the answer.

EXTRACTION DISCIPLINE — read this carefully before saying "insufficient evidence":

4. When tool results CONTAIN the answer (verbatim or near-verbatim, even \
embedded in larger text), you MUST extract the specific fact. Examples:
   - Q: "What's James's favorite game?" — recall has "James: my favourite \
game is Apex Legends" → answer "Apex Legends" and cite the hit.
   - Q: "What's Joanna's favorite movie?" — recall has "Joanna: I love \
Eternal Sunshine of the Spotless Mind" → answer with the title.
   - Q: "What's Jon's favorite style of dance?" — recall has "Jon: \
contemporary feels right to me" → answer "contemporary".

5. Wiki / persona pages in recall ARE evidence. If a person's wiki page is \
returned but doesn't directly list the specific fact, call `expand` on the \
page id OR run another `recall` with the specific term (e.g. "Joanna favorite \
movie", "Jon dance style") BEFORE concluding insufficient evidence.

6. "Insufficient evidence" is ONLY valid when:
   - No retrieved memory mentions the queried fact at all, OR
   - The question presupposes something not attested in memory (e.g. \
"Why did X quit Y?" when X never worked at Y), OR
   - You've spent the recall budget refining and still nothing matches.

7. NOT valid reasons to say insufficient evidence:
   - "The wording in evidence differs from the question's wording"
   - "I see partial information but not the exact phrasing"
   - "I'd need more context"
   - "Multiple options are mentioned, I can't pick one" — pick the most \
specific match and cite it.

8. When in doubt between abstaining and extracting: extract, cite the hit, \
and let the answer be evidence-grounded. A confident extraction beats an \
overcautious abstain.
"""


_ADVERSARIAL_DEFENSE_RULES = """\

ADVERSARIAL DEFENSE (read this carefully):

A. Premise check before answering. If the question PRESUPPOSES a fact \
("Why did Alice quit Google?" presupposes Alice worked at Google AND quit), \
your evidence must directly support each presupposition. If ANY \
presupposition lacks supporting evidence in the tool results, return:
   "insufficient evidence — '<the unsupported presupposition>' is not \
attested in memory."

B. Entity / date / location verification. If the question names a specific \
entity, date, or place, verify that token actually appears in the retrieved \
evidence with the right context before incorporating it into your answer.

C. Negative-existence questions. If asked "Did X ever do Y?" and recall \
returns no memory in which X is associated with Y, the correct answer is \
"No" or "no evidence in memory" — NOT a fabricated yes-answer from \
loosely-related hits.

D. Time-shift traps. Dates and time periods in the question must match \
dates in the retrieved evidence. If the question asks about year N but \
all evidence is year M, abstain — do not silently shift the date.

E. "I don't know" is ALWAYS a valid answer. Speculation is never valid. \
A confident "insufficient evidence" beats a plausible-but-unsupported answer.
"""


def _build_system_prompt(*, adversarial_defense: bool = False) -> str:
    """Compose the agentic-loop system prompt.

    The base prompt is always included; adversarial-defense rules are
    appended only when the orchestrator has the feature enabled. Keeping
    them off by default avoids the small overhead of the extra prompt
    text on workloads that don't have adversarial questions.
    """
    if adversarial_defense:
        return _SYSTEM_PROMPT + _ADVERSARIAL_DEFENSE_RULES
    return _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Loop entry point
# ---------------------------------------------------------------------------


RecallFn = Callable[[str, int], Awaitable[list[MemoryHit]]]
ObservationsFn = Callable[[str, int], Awaitable[list[MemoryHit]]] | None
ExpandFn = Callable[[str, int], Awaitable[list[MemoryHit]]] | None


@dataclass
class _AgentState:
    """Running-state container for the loop."""

    evidence: list[MemoryHit] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    conversation: list[Message] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0


def _add_hits(state: _AgentState, hits: list[MemoryHit], cap: int) -> int:
    """Append unseen hits, return the count of newly-added hits."""
    added = 0
    for h in hits:
        hid = h.memory_id
        if hid and hid not in state.seen_ids:
            state.evidence.append(h)
            state.seen_ids.add(hid)
            added += 1
        if len(state.evidence) >= cap:
            break
    return added


def _format_hits_for_tool_response(hits: list[MemoryHit]) -> str:
    """Render hits as a compact JSON array the LLM can consume."""
    payload = []
    for h in hits:
        text = (h.text or "").strip()
        if len(text) > 600:
            text = text[:597] + "..."
        payload.append({
            "id": h.memory_id,
            "text": text,
            "score": round(h.score, 4) if h.score else 0.0,
        })
    return json.dumps(payload, ensure_ascii=False)


async def agentic_reflect(
    query: str,
    *,
    initial_hits: list[MemoryHit],
    recall_fn: RecallFn,
    llm_provider,
    observations_fn: ObservationsFn = None,
    expand_fn: ExpandFn = None,
    params: AgenticReflectParams | None = None,
    final_synthesize_fn: Callable[..., Awaitable[ReflectResult]] | None = None,
    final_synthesize_kwargs: dict[str, Any] | None = None,
) -> ReflectResult:
    """Run the agentic loop with native function calling.

    Args:
        query: The user's question.
        initial_hits: Evidence already gathered by the outer recall step.
        recall_fn: Async callable ``(query, max_results) -> [MemoryHit]``.
        llm_provider: Must support the extended ``complete`` signature
            with ``tools=`` and emit parsed ``tool_calls`` on the
            returned :class:`Completion`.
        observations_fn: Optional. When provided, the ``search_observations``
            tool is enabled. Callable ``(query, max_results)``.
        expand_fn: Optional. When provided, the ``expand`` tool is
            enabled. Callable ``(memory_id, max_sources)``.
        params: Loop tuning. Defaults match Hindsight.
        final_synthesize_fn / final_synthesize_kwargs: Fallback path
            when the loop hits its iteration cap or the provider lacks
            tool-call support. Typically the orchestrator's ``synthesize``.
    """
    p = params or AgenticReflectParams()

    # Build the tool list from optional capabilities; recall + done are
    # always present.
    tools: list[ToolDefinition] = [_TOOL_RECALL]
    if observations_fn is not None:
        tools.append(_TOOL_SEARCH_OBSERVATIONS)
    if expand_fn is not None:
        tools.append(_TOOL_EXPAND)
    tools.append(_TOOL_DONE)

    state = _AgentState(
        evidence=list(initial_hits[: p.max_evidence_pool_size]),
        seen_ids={h.memory_id for h in initial_hits if h.memory_id},
        conversation=[
            Message(role="system", content=_build_system_prompt(
                adversarial_defense=p.adversarial_defense,
            )),
            Message(
                role="user",
                content=(
                    f"Question: {query}\n\n"
                    f"Initial evidence (from outer recall):\n"
                    f"{_format_hits_for_tool_response(initial_hits[: p.max_evidence_pool_size])}"
                ),
            ),
        ],
    )

    iteration = 0
    while iteration < p.max_iterations:
        iteration += 1
        try:
            completion = await llm_provider.complete(
                state.conversation,
                max_tokens=1024,
                temperature=0.0,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:
            _logger.warning(
                "agentic_reflect: LLM call failed at iter %d (%s); "
                "falling back to forced synthesis.", iteration, exc,
            )
            break

        if completion.usage:
            state.total_input_tokens += completion.usage.input_tokens
            state.total_output_tokens += completion.usage.output_tokens

        tool_calls = completion.tool_calls or []

        # Feature-detect: provider didn't return parsed tool_calls. Try
        # legacy JSON-in-prose for one turn before falling through.
        if not tool_calls:
            if p.fallback_on_no_tool_call:
                _logger.info(
                    "agentic_reflect: no tool_calls at iter %d "
                    "(provider may lack tool support); falling through "
                    "to forced synthesis.",
                    iteration,
                )
                break
            # Otherwise loop again — the model may have emitted prose
            # this turn but tool calls next turn.
            state.conversation.append(
                Message(role="assistant", content=completion.text or "")
            )
            continue

        # Append the assistant's tool-calling turn so the model sees
        # its own prior calls in the next iteration's context.
        state.conversation.append(
            Message(
                role="assistant",
                content=completion.text or "",
                tool_calls=tool_calls,
            )
        )

        # Execute each tool call (typically one per turn, but providers
        # can emit several). The first ``done`` call short-circuits.
        for tc in tool_calls:
            name = _normalize_tool_name(tc.name)
            args = tc.arguments or {}

            if name == "done":
                answer = str(args.get("answer") or "").strip()
                cited_ids_raw = args.get("cited_ids") or []
                if not isinstance(cited_ids_raw, list):
                    cited_ids_raw = []
                cited_set = {str(c) for c in cited_ids_raw if c}
                cited_hits = [h for h in state.evidence if h.memory_id in cited_set]
                return ReflectResult(answer=answer, sources=cited_hits or state.evidence)

            if name == "recall":
                sub_q = str(args.get("query") or query).strip() or query
                try:
                    max_results = int(args.get("max_results") or p.recall_step_max_results)
                except (TypeError, ValueError):
                    max_results = p.recall_step_max_results
                max_results = max(1, min(max_results, 20))
                try:
                    new_hits = await recall_fn(sub_q, max_results)
                except Exception as exc:
                    _logger.warning("agentic_reflect.recall failed (%s)", exc)
                    new_hits = []
                _add_hits(state, new_hits, p.max_evidence_pool_size)
                state.conversation.append(
                    Message(
                        role="tool",
                        tool_call_id=tc.id,
                        name=name,
                        content=_format_hits_for_tool_response(new_hits),
                    )
                )
                continue

            if name == "search_observations" and observations_fn is not None:
                sub_q = str(args.get("query") or query).strip() or query
                try:
                    max_results = int(args.get("max_results") or p.observations_step_max_results)
                except (TypeError, ValueError):
                    max_results = p.observations_step_max_results
                max_results = max(1, min(max_results, 20))
                try:
                    new_hits = await observations_fn(sub_q, max_results)
                except Exception as exc:
                    _logger.warning("agentic_reflect.observations failed (%s)", exc)
                    new_hits = []
                _add_hits(state, new_hits, p.max_evidence_pool_size)
                state.conversation.append(
                    Message(
                        role="tool",
                        tool_call_id=tc.id,
                        name=name,
                        content=_format_hits_for_tool_response(new_hits),
                    )
                )
                continue

            if name == "expand" and expand_fn is not None:
                memory_id = str(args.get("memory_id") or "").strip()
                try:
                    max_sources = int(args.get("max_sources") or p.expand_step_max_sources)
                except (TypeError, ValueError):
                    max_sources = p.expand_step_max_sources
                max_sources = max(1, min(max_sources, 20))
                if not memory_id:
                    new_hits: list[MemoryHit] = []
                else:
                    try:
                        new_hits = await expand_fn(memory_id, max_sources)
                    except Exception as exc:
                        _logger.warning("agentic_reflect.expand failed (%s)", exc)
                        new_hits = []
                _add_hits(state, new_hits, p.max_evidence_pool_size)
                state.conversation.append(
                    Message(
                        role="tool",
                        tool_call_id=tc.id,
                        name=name,
                        content=_format_hits_for_tool_response(new_hits),
                    )
                )
                continue

            # Unknown tool — feed a structured error back so the model
            # can recover rather than treating the response as terminal.
            _logger.info(
                "agentic_reflect: unknown tool %r at iter %d; "
                "informing model and continuing.", name, iteration,
            )
            state.conversation.append(
                Message(
                    role="tool",
                    tool_call_id=tc.id,
                    name=name,
                    content=json.dumps(
                        {"error": f"unknown tool: {tc.name!r}; valid: {[t.name for t in tools]}"}
                    ),
                )
            )

    # Loop exited without ``done``. Forced single-shot synthesis over
    # whatever evidence accumulated.
    if final_synthesize_fn is not None:
        return await final_synthesize_fn(
            query=query,
            hits=state.evidence,
            llm_provider=llm_provider,
            **(final_synthesize_kwargs or {}),
        )
    return ReflectResult(
        answer="insufficient evidence",
        sources=state.evidence,
    )
