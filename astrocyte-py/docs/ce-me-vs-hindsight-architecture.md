# CE + ME vs Hindsight: Architectural Reality

**Date:** 2026-05-16
**Status:** Active reference doc for ongoing path-to-≥-Hindsight work
**Companion:** [ce-me-gap-analysis.md](../../../ce-me-gap-analysis.md) — the six concrete gaps

---

## What Hindsight Actually Is

Hindsight is a **product**, not a research benchmark architecture. Specifically:

- **Vendor:** Vectorize AI (vectorize.io). Open-source slim API under
  `hindsight/hindsight-api-slim/`; control plane + integrations live in
  the parent monorepo.
- **API surface:** three verbs on a single bank — `retain()`, `recall()`,
  `reflect()`. That is it.
- **Storage shape:** banks → memory_units → documents + entities +
  entity_links, all in Postgres + pgvector. There is **no Document Engine
  layer** — `retain()` accepts opaque text and an LLM extracts
  facts/entities/links during ingest. Discourse structure (sessions,
  speaker turns, conversational scaffolding) is not modeled — it is
  inferred from text post-hoc, if at all.
- **Memory model:** three tiers — world facts, experience facts, mental
  models (auto-consolidated). Hindsight calls these *facts*, *observations*,
  *mental models* in code. Plus directives compiled from user-stated
  preferences.
- **Recall pipeline:** four parallel strategies (semantic / BM25 / graph /
  temporal) fused with RRF (k=60), cross-encoder rerank, multiplicative
  post-rerank boosts (recency × temporal-band × proof-count).
- **Reflect pipeline:** agentic tool-calling loop (≤5 iterations) with
  five tools — `search_mental_models`, `search_observations`, `recall`,
  `expand`, `done`. Hierarchical precedence: directives → mental_models →
  observations → raw recalls.

**What Hindsight is NOT:** Hindsight has **no Document Engine**. There is
no notion of *document → session → turn* with structural metadata. The
retain API is text-in, fact-out. Session boundaries, speaker roles, and
per-turn temporal anchors do not exist as first-class concepts in the
schema. Everything Hindsight knows about discourse it learned by re-reading
the same text the user sent it.

---

## What Belongs to Astrocyte

Astrocyte is a **gateway**, not a memory store. It owns three engines
that compose to produce memory behavior — Document Engine (DE),
Conversation Engine (CE), and Memory Engine (ME). Hindsight is one
possible **upstream backend** Astrocyte can speak to, alongside Mem0,
Zep, the local PageIndex pipeline, etc.

### Document Engine (DE)

**Role:** transform raw inputs into a navigable structural tree before
anything semantic is computed.

**Inputs:** arbitrary text — conversation transcripts, PDFs, markdown,
HTML, code repos.

**Outputs:** a `PageIndexDocument` — title + nested-dict tree
(`{title, line_num, summary, nodes: [...]}`) — anchored to original
line numbers in the source. Each leaf is a "section" with its own
`section_id`, summary, embedding, and entity index.

**Why this matters vs Hindsight:**
- Discourse structure is **kept**, not re-inferred. The picker (LLM
  reasoning over the tree) can address "session 5" or "line 42"
  deterministically; Hindsight's reflect agent has to navigate
  text-by-text via tool calls.
- Per-section temporal anchors are populated at retain time from header
  parsing (`## Session 5 (8 May, 2023)`) and per-turn date stamps. The
  reranker has real `session_date`, `event_date`, `occurred_start`
  signals to work with — Hindsight has to extract dates from text on
  every recall.
- Cross-modal: the same tree shape accommodates non-text modalities
  (B4 in the gap doc). Hindsight is text-only.

**Implementation files:**
- `astrocyte/pipeline/chunking.py` — tree extraction
- `astrocyte/pipeline/section_*.py` — per-section enrichment
  (embedding, entity extraction, event extraction, link extraction,
  facts)

### Conversation Engine (CE)

**Role:** turn-level reasoning over conversation transcripts. Owns
speaker identity, turn ordering, session boundaries, and the picker
loop that selects which sections feed the synth.

**Inputs:** a `PageIndexDocument` from DE, a user question, optional
category metadata.

**Outputs:** a `(answer, picked_line_nums)` tuple. The line_nums are
the structural citations — used by the bench harness to compute
recall@k against ground-truth dia_ids.

**Why this matters vs Hindsight:**
- The picker sees a **constrained skeleton** (top-25 reranked sections,
  full tree as fallback allowlist) rather than a flat candidate list.
  An LLM reading a small structured tree picks 5–10 evidence sections
  reliably; the same LLM reading 30 flat candidates degenerates to
  `[1]` (Phase A failure analysis).
- Mode dispatch (`temporal` / `listing` / `inference` / `counting` /
  default) picks both the right pick-prompt AND the right synth-prompt.
  Hindsight's reflect loop applies the same prompt regardless of
  question shape.
- Wiki / fact / mental-model context blocks are injected as structured
  prefixes (`[OBSERVATION]`, `[FACTS]`, `[USER PROFILE]`) so the synth
  sees layered evidence with explicit provenance.

**Implementation files:**
- `scripts/bench_pageindex_locomo.py:answer_question` — main CE loop
- `astrocyte/pipeline/section_recall.py` — parallel retrieval +
  RRF fusion (Hindsight-parity layer)
- `astrocyte/pipeline/section_rerank.py` — cross-encoder rerank +
  constrained skeleton
- `astrocyte/pipeline/agentic_reflect.py` — iterative tool-call loop
  for multi-session / multi-hop categories

### Memory Engine (ME)

**Role:** long-lived storage of distilled artifacts (atomic facts, mental
models, wiki pages, directives) with semantic + entity + temporal +
graph-link indexes.

**Inputs:** sections from DE, signals from CE.

**Outputs:** queryable stores — `astrocyte_pi_facts`,
`astrocyte_pi_section_links`, `astrocyte_mental_models`,
`astrocyte_wiki_pages`.

**Why this matters vs Hindsight:**
- Distilled artifacts (mental models, wiki pages) are **structurally
  scoped** to their source document(s) and live alongside raw sections —
  not in a separate "facts" bag. Recall can compose `[USER PROFILE]` +
  `[FACTS]` + section excerpts in one synth pass with explicit layer
  attribution.
- Section links carry typed edges (`semantic_knn`, `causal`,
  `elaborates`, `supersedes`) — Hindsight has only `semantic_knn`
  links. This is the substrate for causal-chain retrieval (B1 in the
  gap doc).
- Wiki tier is per-document and lint-checked at recall time
  (`pipeline/wiki_lint.py`) — stale or contradicted wikis are filtered
  before they reach the synth. Hindsight has no equivalent.

**Implementation files:**
- `astrocyte/pipeline/section_fact_extraction.py`,
  `fact_extraction.py`, `fact_rerank.py`
- `astrocyte/pipeline/mental_model_compile.py`,
  `directive_compile.py`, `preference_compile.py`
- `astrocyte/pipeline/wiki_lint.py`

---

## CE Path's Structural Advantages Over Hindsight's Opaque `retain()`

Hindsight's `retain(text)` is a one-way valve. Once text goes in, the
discourse structure is gone — the engine sees only the residue (extracted
facts + entity links). Three concrete consequences, each of which becomes
an Astrocyte advantage once the agentic loop (Gap 1) is in:

### 1. Session boundaries are first-class

In Astrocyte, a session is a `PageIndexSection` with explicit
`session_date`, `line_num` start/end, and a parent in the section tree.
The picker can answer "across sessions" questions with a
session-scoped aggregation query rather than a retrieve-then-synth
pass.

In Hindsight, "session" is a heuristic — the engine has to either
re-parse headers from raw text or infer session changes from temporal
gaps. Multi-session counting (LME's bread-and-butter category) becomes
a retrieval problem rather than an aggregation problem, which is why
Hindsight's reflect agent has to iterate 3–5 times for a question that
Astrocyte can answer in one structured query (once B2 lands).

### 2. Speaker roles are first-class

In Astrocyte, every section carries explicit `speaker={'user' |
'assistant' | <name>}`. The "assistant-recall" strategy (LME single-
session-assistant) directly filters on `speaker='assistant'` — no
text-mining required.

In Hindsight, speaker has to be inferred from prose ("you said…", "I
told you…"). Cross-encoder reranking on speaker-attributed questions
in Hindsight is doing implicit speaker disambiguation as a byproduct
of relevance scoring — which works but is noisier than a direct filter.

### 3. Per-turn temporal anchors are first-class

In Astrocyte, every section has `session_date` (when the turn
happened) and `occurred_start` / `occurred_end` (the in-dialogue event
date — separable from the session date). The synth gets a structured
header per excerpt: `[line=42, session=2023-05-08, event=2023-05-03]`.

In Hindsight, the cross-encoder gets the section text and a generic
recency boost. There is no distinction between "when the user told me
about the event" and "when the event happened" — both collapse into a
single retrieval-time recency score.

### Why these become *advantages* once Gap 1 lands

Single-pass retrieval over a flat candidate list can only express ONE
of the above signals at a time. The cross-encoder either ranks by
relevance, OR the temporal strategy ranks by recency, OR the speaker
filter narrows the pool — but each pass is its own pipeline.

The agentic reflect loop (Gap 1) lets the answerer compose them: "look
in `speaker='assistant'` first, then expand to surrounding turns, then
follow causal links to the conclusion." Hindsight's reflect agent can
do the same dance, but its tools operate on a flat memory bag — every
expansion is text-mining. Astrocyte's tools can navigate the structural
tree directly, which is cheaper, more precise, and lets the agent
verify "I have evidence from 3 sessions" deterministically instead of
hoping the reranker surfaced enough variety.

The path to **exceeding** Hindsight (not just matching it) runs
through the structural tree the CE/DE layers preserve. Gaps 1–6 close
the matching part; the B-series advantages (causal chains, session-
scoped aggregation, user-curated directives, cross-modal) open the
gap going the other way.

---

## Six Gaps (Summary; See Gap Doc for Detail)

See [ce-me-gap-analysis.md](../../../ce-me-gap-analysis.md) for full
descriptions, file references, and impact estimates.

| # | Gap | Tier | Est. impact | Status |
|---|-----|------|-------------|--------|
| 1 | Agentic reflect loop (multi-iteration tool-call) | T1 | +5–15pp LME, +2–5pp LoCoMo | Partial — `agentic_reflect.py` exists, gated to multi-session/multi-hop |
| 2 | Link-expansion graph retrieval at recall | T1 | +3–7pp LoCoMo, +1–2pp LME | Pending — table populated, no reader |
| 3 | Post-rerank multiplicative boosts | T2 | +2–5pp temporal | **Closed (2026-05-16)** |
| 4 | Per-document entity canonicalization | T2 | +2–5pp multi-session counting | Pending |
| 5 | LLM temporal fallback enabled in bench | T2 | +1–2pp temporal | **Closed (2026-05-16)** |
| 6 | LME per-category answer-prompt routing | T2 | +1–3pp open-domain | **Closed (2026-05-16)** |

The three closed gaps (this commit) are pure-additive quick wins with
no impact on the running conv-run-3 benchmark — they ship behind module
imports / config flags that the bench script picks up on next start.

---

## Why the CE Path Will Exceed Hindsight (Once Gaps 1–6 Close)

Hindsight's ceiling is the **information it threw away at retain time**.
Once the answerer needs structural facts (which session a claim came
from, which speaker said it, when in the timeline the event happened)
that weren't preserved, the reflect agent has to reconstruct them from
text via more tool calls. That works but it caps out around 91–93%
accuracy because reconstruction is lossy.

Astrocyte's ceiling is the **agency we haven't wired in yet**. The
structural information is already there — gaps 1, 2, 4, 6 are about
teaching the answerer to use it. Once the agentic loop has the
structural tools (B1 causal chain walk, B2 session-scoped aggregation,
plus the existing recall/expand pair), single-pass retrieval bottlenecks
disappear and the system answers via composition rather than retrieval.

The replication policy (≥2σ gate, 3-run minimum) keeps us honest about
which gains are real; the M14 phase-4 retraction (single-run +13.3pp
dissolved to noise) is the cautionary tale we measure against.
