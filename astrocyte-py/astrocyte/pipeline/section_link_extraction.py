"""PR2 D.7: per-section causal / supersedes / elaborates link extraction.

Why this exists: the ``graph_expand`` strategy (PR2 commit B) reads
``section_links`` to do 1-hop expansion from semantic / entity seeds.
Without this module, ``section_links`` is empty and graph_expand
returns no hits — which is why **LME multi-session sat at 11% (1/9)**
across PR2-D's iterations. PR2-D.7 populates the table at retain.

Three link types we extract:

| link_type     | Meaning                                          | Weight semantics |
|---------------|--------------------------------------------------|------------------|
| **causal**     | from_section caused (or enabled) to_section       | Higher = more direct |
| **supersedes** | to_section corrects / updates from_section       | Time-ordered (newer wins) — LME knowledge-update primitive |
| **elaborates** | to_section extends / adds detail to from_section | Topical continuity |

Weights default to 0.7 unless the LLM emits one. We don't tune weights
at PR2-D.7; the graph_expand strategy sums per-edge weights, so a flat
0.7 makes RRF behaviour predictable.

## Why per-section, not per-pair

Pair-wise extraction (look at every (i, j) section pair) is O(N²) at
retain and dominates LME cost. Per-section is O(N): for each section,
the LLM looks at the section's content + a window of prior section
summaries and emits any links it sees. Resulting graph is sparse but
covers the dominant retrieval cases.

## Cost shape

One LLM call per section. For LoCoMo ~30 sections × 10 convs = 300
calls (~$0.05 first-time, $0 on cache). For LME ~50 sections × 50
samples = 2500 calls (~$0.40 first-time, $0 on cache).

Failures degrade silently — link extraction is enrichment, not
correctness. The picker still works without it; graph_expand just
returns empty.

See:
- docs/_design/recall.md §5 (retain pipeline)
- docs/_design/adr/adr-006-three-layer-recall-stack.md (graph layer)
- ``astrocyte_pi_section_links`` schema in 015_tier2_recall.sql
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrocyte.provider import LLMProvider
    from astrocyte.types import PageIndexSection

from astrocyte.types import Message, PageIndexSectionLink

logger = logging.getLogger("astrocyte.pipeline.section_link_extraction")


_VALID_LINK_TYPES = frozenset({"causal", "supersedes", "elaborates"})

#: Cap how many prior-section summaries we show the LLM per call. Bounds
#: prompt size so retain wallclock stays bounded; LME's 50-section docs
#: would otherwise blow past gpt-4o-mini's context window.
_PRIOR_WINDOW = 8

#: Cap on emitted links per section. Pathological extractions (every
#: prior section linked) inflate the index without lifting recall —
#: graph_expand only needs the strongest few.
_MAX_LINKS_PER_SECTION = 5


_EXTRACT_PROMPT = """You are analysing a long conversation transcript. The transcript has been chunked into sessions. For the CURRENT session, identify links to PRIOR sessions in this conversation.

Three link types:

- "causal": the current session was caused or enabled by the prior session (e.g., prior session sets up an event that the current session reports the outcome of).
- "supersedes": the current session corrects or updates information from the prior session (e.g., user previously said X, now says actually Y).
- "elaborates": the current session adds detail or follow-up to a topic the prior session introduced.

CURRENT session (line {current_line}):
{current_text}

PRIOR sessions (you can link to any of these):
{prior_summary_block}

Return ONLY a JSON object: {{"links": [{{"to_line": <int>, "type": "causal|supersedes|elaborates", "weight": <0.0-1.0>, "reason": "<one short clause>"}}, ...]}}.

Emit at most {max_links} links. Return {{"links": []}} when no clear link exists.

Constraint: ``to_line`` MUST be one of the prior-session line numbers shown above.

Output (JSON only):
"""


async def extract_links_for_section(
    provider: "LLMProvider",
    document_id: str,
    current: "PageIndexSection",
    current_text: str,
    prior_sections: list["PageIndexSection"],
    *,
    model: str | None = None,
) -> list[PageIndexSectionLink]:
    """One LLM call → up to ``_MAX_LINKS_PER_SECTION`` ``PageIndexSectionLink`` rows.

    ``prior_sections`` is a window of the most-recent prior sections
    (caller passes them; usually the previous N session-grain
    sections in the same document). The LLM sees their summaries and
    line_nums; it can emit links to any subset.

    Returns an empty list on parse failure / no links found / no
    valid prior window.
    """
    if not current_text.strip() or not prior_sections:
        return []

    # Build the prior-summary block for the prompt.
    prior_lines: list[str] = []
    valid_to_lines: set[int] = set()
    for p in prior_sections[-_PRIOR_WINDOW:]:
        title = (p.title or "").strip()
        summary = (p.summary or "").strip()
        if not title and not summary:
            continue
        prior_lines.append(
            f"  - line {p.line_num}: {title}\n      summary: {summary[:240]}"
        )
        valid_to_lines.add(p.line_num)
    if not prior_lines:
        return []

    msg = _EXTRACT_PROMPT.format(
        current_line=current.line_num,
        current_text=current_text[:4000],  # cap; matches entity-extract sizing
        prior_summary_block="\n".join(prior_lines),
        max_links=_MAX_LINKS_PER_SECTION,
    )
    completion = await provider.complete(
        messages=[Message(role="user", content=msg)],
        model=model,
        max_tokens=400,
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    try:
        parsed = json.loads(completion.text)
        raw = parsed.get("links") or []
    except json.JSONDecodeError:
        logger.warning(
            "section_link_extraction: JSON parse failed for doc=%s line=%d",
            document_id, current.line_num,
        )
        return []

    out: list[PageIndexSectionLink] = []
    seen_to: set[tuple[int, str]] = set()  # (to_line, type) dedupe
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        to_line = entry.get("to_line")
        link_type = entry.get("type")
        if not isinstance(to_line, int) or to_line not in valid_to_lines:
            continue
        if link_type not in _VALID_LINK_TYPES:
            continue
        # Self-loops are nonsensical; skip.
        if to_line == current.line_num:
            continue
        key = (to_line, link_type)
        if key in seen_to:
            continue
        seen_to.add(key)
        weight_raw = entry.get("weight", 0.7)
        try:
            weight = float(weight_raw)
        except (TypeError, ValueError):
            weight = 0.7
        # Clamp to [0, 1] to match the schema's expected range.
        weight = max(0.0, min(1.0, weight))
        out.append(
            PageIndexSectionLink(
                from_doc=document_id,
                from_line=current.line_num,
                to_doc=document_id,
                to_line=to_line,
                link_type=link_type,
                weight=weight,
            )
        )
        if len(out) >= _MAX_LINKS_PER_SECTION:
            break
    return out
