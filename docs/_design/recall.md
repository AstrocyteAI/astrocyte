# Recall — three-layer stack synthesising PageIndex, Hindsight, and LLM Wiki

**Status:** Draft (M9 design). See [product-roadmap.md (§ Release numbering)](product-roadmap.md) for milestone status.

**See also:**
- [llm-wiki-compile.md](llm-wiki-compile.md) — Layer 1 (compounding wiki pages, Karpathy pattern)
- [hindsight-informed-capabilities.md](hindsight-informed-capabilities.md) — Layer 2 graph techniques (link expansion, RRF, rerank)
- [storage-and-data-planes.md](storage-and-data-planes.md) — DDL for Layer 2 tables
- [adr/adr-006-three-layer-recall-stack.md](adr/adr-006-three-layer-recall-stack.md) — decision record
- [adr/adr-007-pageindex-tree-as-section-primitive.md](adr/adr-007-pageindex-tree-as-section-primitive.md) — section-grain decision
- [adr/adr-008-section-graph-replaces-age.md](adr/adr-008-section-graph-replaces-age.md) — AGE removal
- [benchmark-roadmap.md](benchmark-roadmap.md) — phased validation gates

---

## 1. Why this exists

Astrocyte's M1–M8 recall path is RAG over atomized memory units with optional AGE-backed graph traversal. Two structural problems surfaced during 2026-04 → 2026-05 benchmark work:

1. **A 33pp regression on LoCoMo** between v0.13.0 (83.28%) and HEAD (~50%) that a 6-layer config-knob bisect could not isolate. The regression is in code, not config — and the more knobs the recall path exposes, the harder it is to keep stable.
2. **17h LME retain wallclock** dominated by per-session structured fact extraction (SFE) calls, with downstream recall accuracy that does not justify the cost.

A vectorless POC ([PageIndex](https://github.com/VectifyAI/PageIndex)) bypassed the entire vector pipeline and reached 62% on LoCoMo at M9 Phase A with zero retain-time vector cost — proving the recall surface, not the storage layer, is what limits accuracy.

This is the formal recall architecture, built as a three-layer stack:

| Layer | Source | Solves |
|---|---|---|
| **L1: Wiki pages** | Karpathy's [LLM wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | "What does the system know about X overall?" |
| **L2: Tree + graph** | [PageIndex](https://github.com/VectifyAI/PageIndex) (tree) + [Hindsight](https://github.com/Hindsight-AI/hindsight) (graph) | "Where in the transcript is X mentioned?" |
| **L3: Raw memories** | Existing Astrocyte memory_units | "What were the exact words?" |

Each layer can answer alone; recall precedence + RRF fusion picks the best signal per question.

---

## 2. Where each technique came from

| Component | Source | What it gives us |
|---|---|---|
| Section as recall primitive (line_num anchors, sliceable markdown) | PageIndex | Preserves conversation/document structure; small synth context |
| Tree skeleton + per-node summaries | PageIndex | Picker has navigable map; mode-specific reasoning loop |
| Mode classifier + mode-specific picker/synth prompts | PageIndex (M9 Phase A) | Categories need different reasoning shapes |
| `<reference_date>` block in synth | PageIndex (M9 Phase A) | Resolves relative time phrases (LME temporal-reasoning) |
| `section_entities` table + `(entity_name)` btree | Hindsight | Entity-bridging multi-hop in <100ms via SQL CTE |
| `section_links` (semantic_knn / causal / supersedes) | Hindsight | Pre-computed graph; no query-time chains |
| RRF fusion (k=60) | Hindsight | Robust signal combination across incomparable rankings |
| Cross-encoder reranker | Hindsight (already in `cross_encoder_rerank` config) | Bridges retrieval and reasoning |
| LATERAL fanout caps (per-entity limit) | Hindsight | Celebrity-name explosions don't break SQL |
| Per-bank partial indexes | Hindsight | Scale to many banks without query degradation |
| `wiki_pages` (compounding topic/entity synthesis) | Karpathy / Astrocyte M8 | LME multi-session and knowledge-update |
| `wiki_contradictions` + lint pass | Karpathy / Astrocyte M8 | LME knowledge-update specifically |
| `wiki_page_revisions` + supersession chain | Karpathy / Astrocyte M8 | "Latest revision wins" semantics |
| `wiki_section_provenance` (cross-layer glue) | NEW | Wiki claims verifiable against source sections |
| Picker as final reranker (not primary retrieval) | NEW (Phase A v6 failure analysis) | Fixes picker non-compliance — small candidate set keeps gpt-4o-mini reliable |
| Three-tier precedence (wiki > sections > raw) | NEW | Highest-quality compiled answer wins when available; falls through to source when not |

---

## 3. Architecture

```
                  ┌────────────────────────────────────────────────┐
            ┌─────►   LAYER 1: WIKI PAGES (Karpathy)               │
            │     │   Compounding topic/entity pages, contradiction│
            │     │   resolution, supersedes chain                 │
            │     │   "What does the system know about X overall?" │
            │     └────────────────────────────────────────────────┘
            │                          │ provenance
recall      │                          ▼
fusion ─────┤     ┌────────────────────────────────────────────────┐
(RRF +      │     │   LAYER 2: TREE + GRAPH (PageIndex + Hindsight)│
 rerank)    ├─────►   Sections (tree nodes) + entity/semantic/    │
            │     │   causal/supersedes links between sections     │
            │     │   "Where in the transcript is X mentioned?"    │
            │     └────────────────────────────────────────────────┘
            │                          │ provenance
            │                          ▼
            │     ┌────────────────────────────────────────────────┐
            └─────►   LAYER 3: RAW MEMORIES (current Astrocyte)    │
                  │   Memory units, chunks                         │
                  │   "What were the exact words?"                 │
                  └────────────────────────────────────────────────┘
```

The tree provides **navigation structure** (PageIndex contribution); the graph provides **cross-references** (Hindsight contribution); fusion + reranker is Hindsight's proven multi-strategy pattern; picker + synth is PageIndex's two-step reasoning loop, but operating on a pre-filtered candidate set instead of the raw skeleton (this fixes the picker non-compliance observed in Phase A v6 — see §8.3).

---

## 4. Data structures

### 4.1 Layer 1 — Wiki pages (extends M8 schema)

The M8 `WikiPage` model already exists. M9 extends it with:

```sql
-- Existing M8 tables (unchanged):
--   wiki_pages, wiki_page_revisions

-- NEW in M9:
CREATE TABLE wiki_contradictions (
    id           UUID PRIMARY KEY,
    page_a       UUID NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    page_b       UUID NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    reason       TEXT NOT NULL,           -- LLM-extracted explanation
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMPTZ,
    resolution   UUID REFERENCES wiki_page_revisions(id)
);
CREATE INDEX ix_wc_unresolved ON wiki_contradictions (page_a, page_b)
    WHERE resolved_at IS NULL;
```

The `wiki_pages.status` column gains values `'stale'` and `'contradicted'` (in addition to the existing `'current'` and `'archived'`). The `WHERE status = 'current'` partial index on `wiki_pages.current_embedding` ensures recall sees only the latest revisions.

### 4.2 Layer 2 — Tree (PageIndex) + graph (Hindsight)

```sql
CREATE TABLE pageindex_documents (
    id              UUID PRIMARY KEY,
    bank_id         UUID NOT NULL,
    source_id       TEXT NOT NULL,        -- e.g. conv-26, lme-user-42
    md_text         TEXT NOT NULL,        -- canonical markdown for slicing
    reference_date  TIMESTAMPTZ,          -- "today" anchor for synth
    built_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bank_id, source_id)
);

CREATE TABLE pageindex_sections (
    document_id        UUID NOT NULL REFERENCES pageindex_documents(id) ON DELETE CASCADE,
    line_num           INT NOT NULL,
    node_id            TEXT NOT NULL,        -- "0001"-style PageIndex node id
    title              TEXT NOT NULL,
    summary            TEXT,
    summary_embedding  VECTOR(1536),
    speaker            TEXT,                 -- 'user' | 'assistant' | NULL — LME assistant-recall
    session_date       TIMESTAMPTZ,
    parent_node        TEXT,
    depth              INT NOT NULL,
    PRIMARY KEY (document_id, line_num)
);
CREATE INDEX ix_pis_skeleton ON pageindex_sections (document_id, depth, line_num);
CREATE INDEX ix_pis_date ON pageindex_sections (document_id, session_date)
    WHERE session_date IS NOT NULL;
CREATE INDEX ix_pis_embed ON pageindex_sections USING vchordrq (summary_embedding);
CREATE INDEX ix_pis_speaker ON pageindex_sections (document_id, speaker)
    WHERE speaker IS NOT NULL;

CREATE TABLE section_entities (              -- Hindsight's unit_entities, at section grain
    document_id  UUID NOT NULL,
    line_num     INT NOT NULL,
    entity_name  TEXT NOT NULL,
    PRIMARY KEY (document_id, line_num, entity_name),
    FOREIGN KEY (document_id, line_num)
        REFERENCES pageindex_sections(document_id, line_num) ON DELETE CASCADE
);
CREATE INDEX ix_se_entity ON section_entities (entity_name);

CREATE TABLE section_links (                 -- Hindsight's memory_links, at section grain
    from_doc    UUID NOT NULL,
    from_line   INT NOT NULL,
    to_doc      UUID NOT NULL,
    to_line     INT NOT NULL,
    link_type   TEXT NOT NULL,                -- 'semantic_knn' | 'causal' | 'supersedes' | 'elaborates'
    weight      FLOAT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (from_doc, from_line, to_doc, to_line, link_type)
);
CREATE INDEX ix_sl_from ON section_links (from_doc, from_line, link_type);
CREATE INDEX ix_sl_to ON section_links (to_doc, to_line, link_type);
```

Per-bank partial-index pattern (§8.4) shards by `bank_id` to keep query plans constant under bank growth.

### 4.3 Cross-layer glue

```sql
CREATE TABLE wiki_section_provenance (
    wiki_page_id UUID NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    document_id  UUID NOT NULL,
    line_num     INT NOT NULL,
    PRIMARY KEY (wiki_page_id, document_id, line_num),
    FOREIGN KEY (document_id, line_num)
        REFERENCES pageindex_sections(document_id, line_num)
);
```

Recall uses this to: take a wiki page hit → fetch underlying L2 sections → slice raw L3 markdown for synth verification context. Lint uses the same path in reverse: a contradiction triggers walk-back from page → sections → raw, where the LLM resolution call sees verbatim source.

### 4.4 Layer 3 (raw memory_units) is unchanged

Existing M1–M8 schema continues to work. The recall path does not read from L3 on the hot path; L3 stays for proof-count, lifecycle, and the optional verbatim verification step.

---

## 5. Retain pipeline

```
session ingested (Tier-1 retain)
        │
        ├─► tree update (PageIndex)
        │   ├─ append to md_text
        │   ├─ md_to_tree (incremental on new branch)               ── 1 LLM call/session (summary)
        │   ├─ section embedding                                     ── 1 embedding
        │   ├─ entity extraction → section_entities                  ── 1 LLM call
        │   ├─ semantic kNN: top-5 similar sections in bank         ── 1 SQL `<=>`
        │   └─ causal/supersedes detection (recent sections)        ── 1 LLM call (gated by bank profile)
        │
        ├─► wiki compile threshold check (M8 CompileEngine)
        │   └─ if (≥50 new memories) OR (≥10 new + 7 days stale):
        │       enqueue async compile job
        │       │
        │       └─► CompileEngine.run(bank_id):
        │           ├─ scope_discover (tags + DBSCAN)               ── ~1 LLM call/cluster
        │           ├─ for each scope:
        │           │   ├─ fetch raw memories + provenance sections
        │           │   ├─ synth wiki page revision                 ── 1 LLM call
        │           │   ├─ embed page                                ── 1 embedding
        │           │   └─ write wiki_pages + wiki_page_revisions
        │           │       + wiki_section_provenance
        │           └─ cross-link pages (entity → entity)
        │
        └─► raw memory_units write (Layer 3, unchanged)

Async cron (daily):
  └─► lint pass over wiki_pages (M8 §7)
      ├─ contradiction detection (page-pair LLM check)              ── 1 LLM call/pair
      ├─ stale check (last_linted_at older than threshold)
      ├─ orphan check (no incoming cross_links, no recall hits)
      └─ supersession resolution (wiki_contradictions → resolution)
```

Cost shape per session: ~3-4 LLM calls + 1 embedding. Same order as M8's SFE+entity+links retain; the saving is that we extract once per **section** (not once per atomized fact). At LoCoMo scale (~10 sessions/conv × 10 convs) that's ~30-40 LLM calls/conv at retain — measured at ~1 minute end-to-end during Phase A.

---

## 6. Recall pipeline

```python
async def tier2_recall(question: str, bank_id: UUID) -> Answer:
    mode = await classify(question)               # 1-token LLM call

    # Strategy fan-out (parallel SQL queries; no LLM calls)
    tasks = [
        wiki_semantic(question_emb, bank_id),                          # L1
        section_semantic(question_emb, bank_id, top=20),               # L2 vector
        section_keyword(question, bank_id, top=20),                    # L2 BM25
        section_entity_lookup(extract_entities(question), bank_id),    # L2 entity (Hindsight CTE)
    ]
    if mode in {"temporal", "temporal-reasoning", "knowledge-update"}:
        tasks.append(section_temporal(parse_dates(question), bank_id)) # L2 temporal
    if mode in {"multi-hop", "multi-session", "knowledge-update"}:
        seeds = await asyncio.gather(tasks[1], tasks[3])
        tasks.append(section_graph_expand(seeds, hops=1))              # L2 graph (Hindsight)
    if mode in {"single-session-assistant", "assistant-recall"}:
        tasks[2] = section_keyword(question, bank_id, speaker='assistant')

    candidates = rrf_fuse(await asyncio.gather(*tasks), k=60)          # Hindsight RRF
    top_15 = await cross_encoder_rerank(question, candidates, top=15)

    picks = await pageindex_picker(question, top_15, mode)             # PageIndex picker as RERANKER
    excerpts = await fetch_excerpts(picks)                             # slice md_text per line_num
    wiki_context = await fetch_wiki_pages(picks)                       # via wiki_section_provenance
    answer = await mode_synth(
        question=question,
        wiki=wiki_context,        # high-priority context
        excerpts=excerpts,        # source-of-truth context
        reference_date=...,
    )
    return answer
```

Cost shape per question: 1 mode classifier + parallel SQL (no LLMs) + 1 picker + 1 synth ≈ **3 LLM calls per question**, comparable to the Phase A POC. Wiki and graph strategies cost zero LLM calls at recall (all SQL).

---

## 7. Per-category mapping

| Bench | Category | Primary layer | Strategies that fire |
|---|---|---|---|
| LoCoMo | single-hop | L2 | semantic + keyword |
| LoCoMo | multi-hop | L2 | **entity + graph 1-hop** |
| LoCoMo | temporal | L2 | **temporal + entity** |
| LoCoMo | open-domain | L1 + L2 | wiki page (if compiled) + semantic + entity |
| LoCoMo | adversarial | — | all strategies weak → picker abstains |
| LME | single-session-user | L2 | semantic + keyword |
| LME | single-session-assistant | L2 | keyword **with `speaker='assistant'` filter** |
| LME | multi-session | L1 + L2 | **wiki page primary** + entity-graph backstop |
| LME | **knowledge-update** | **L1** | **wiki revision chain** (lint resolved supersession) |
| LME | temporal-reasoning | L2 | temporal + reference_date |
| LME | abstention | — | all strategies weak → picker abstains |

The wiki layer is the differentiator for LME. LoCoMo conversations are too short for compounding; the L2 tree+graph is sufficient. LME's long histories give the wiki time to compile, lint, resolve contradictions — that's where Hindsight's atomized graph runs out of steam and Karpathy's compiled-page model wins.

---

## 8. Design notes

### 8.1 Why sections, not memory_units, are the recall primitive

`memory_units` were designed as atomized facts for proof-count and lifecycle operations. Recall over atomized facts loses two things that matter for accuracy:

1. **Conversation structure**: a multi-hop question over Jon and Gina needs to know which session each was discussed in; atomized facts strip that.
2. **Synth context efficiency**: 12 memory_units = 12 disjoint snippets (~3000 tokens with overhead); 12 sections = 12 contiguous excerpts with speaker turns and date headers (~1500 tokens). The synth makes better use of token budget when context preserves discourse.

L3 raw memory_units stay first-class for proof_count, lifecycle (forget / archive), and the optional verbatim verification fetch. They're just off the hot recall path.

See [adr-007-pageindex-tree-as-section-primitive.md](adr/adr-007-pageindex-tree-as-section-primitive.md).

### 8.2 Why AGE is removed (not deprecated)

Hindsight runs the same multi-hop / entity-graph workloads we want, without AGE. Their `memory_links` flat table + LATERAL-capped CTEs measured at sub-100ms for entity expansion. `section_links` is the same pattern at section grain. AGE adds a Postgres extension dep, version-compatibility friction, and a query-planner boundary that prevents cross-source optimization — all for capabilities (recursive Cypher, graph analytics) that Astrocyte does not currently expose.

See [adr-008-section-graph-replaces-age.md](adr/adr-008-section-graph-replaces-age.md).

### 8.3 Why the picker becomes a reranker

Phase A v6 (50Q) collapsed to 50% accuracy because gpt-4o-mini, when asked to pick from 30 raw skeleton nodes under a multi-hop prompt, returned a degenerate single-pick (`[1]` = title page) and the synth confabulated against the title-page excerpt. Two fixes were tried in v7:

1. Min-pick floor (pad to ≥4 with first-N session anchors) — recovered single-hop and adversarial.
2. Anti-hallucination rules in synth — over-applied to inference questions; open-domain dropped to 11%.

The picker-as-reranker pattern fixes the root cause: the picker operates on a **15-section pre-filtered candidate set** (output of RRF + cross-encoder), not the raw 30-node skeleton. With a small, relevance-ranked input the model reliably picks 5-10 — the failure mode that produced `[1]` only appeared at higher candidate counts. The mode-specific prompts (default / temporal / listing) and `<reference_date>` synth block carry forward unchanged from Phase A v7.

### 8.4 Per-bank partial indexes (Hindsight pattern, AGE pattern at flat-table grain)

Per the M5 AGE work, each bank previously got its own per-(bank, label) partial index on AGE entity tables. The flat-table equivalent is straightforward:

```sql
-- Example: per-bank partial index on section_entities for high-traffic banks
CREATE INDEX ix_se_entity_bank_alice
    ON section_entities (entity_name)
    INCLUDE (line_num)
    WHERE document_id IN (
        SELECT id FROM pageindex_documents WHERE bank_id = '<alice-uuid>'
    );
```

In practice we won't materialize these by hand — a maintenance worker creates them when a bank crosses a row threshold (e.g. >10k sections), mirroring the M5 `_ensure_label` pattern. The maintenance worker is separate from the recall path; recall just queries `section_entities` with `bank_id` in the predicate and Postgres picks the right index.

### 8.5 What this design deliberately does NOT include

| Excluded | Why |
|---|---|
| AGE graph store | Removed entirely (ADR-008); no migration window |
| `memory_units` on the read path | Stays for proof_count/lifecycle; sections become the recall primitive |
| `agentic_reflect` as primary recall | Becomes optional refinement after the picker, not a retrieval surface |
| `spreading_activation` as a strategy | Replaced by `section_graph_expand` (Hindsight's link-expansion at retain time) |
| Recursive multi-hop (3+ hops) | Out of scope; 1-hop expansion + iterated retrieval covers our actual workloads |
| Graph analytics (PageRank, communities) | Out of scope; not on any roadmap |

---

## 9. Phased implementation (PR1 → PR2 → PR3)

Each phase is one PR (or a handful of well-checkpointed commits) with its own bench gate. Subsequent PRs do not start until the prior gate clears.

### PR1 — Foundation (target: 1 week)

**Scope**: schema + migrations + AGE removal + tree-build to Postgres + port v7 PageIndex picker (no behavioral change vs file-based).

| Commit | Content |
|---|---|
| A | Postgres migration: create L2 tables + DROP EXTENSION age |
| B | Remove AGE code paths, configs, tests, deps |
| C | Tree-build pipeline writes to Postgres (replaces `/tmp/pageindex-*` cache) |
| D | Port v7 picker/synth to read sections from Postgres |
| E | Tests: schema, retain, recall, regression-vs-file-based |

**Bench gates**:
- LoCoMo 50Q smoke at parity with file-based v7 (≥62% overall, ≥100% adversarial, ≥90% single-hop)
- LME 50Q smoke runs without crash; baseline accuracy recorded for PR2 comparison

### PR2 — Recall stack (target: 2-3 weeks)

**Scope**: section embedding + entity extraction + 5 parallel strategies + RRF + cross-encoder rerank + picker-as-reranker + mode classifier + mode-specific synth + causal/supersedes extraction.

| Commit | Content |
|---|---|
| A | Section embedding + entity extraction at retain; `section_entities` populated |
| B | Five parallel recall strategies (semantic, keyword, entity, temporal, graph); RRF fusion |
| C | Cross-encoder rerank wired in |
| D | Mode classifier (1-tok LLM); picker-as-reranker over top-15; mode-specific synth prompts |
| E | Causal/supersedes link extraction at retain; `section_links` populated |
| F | Tests: per-strategy, integration, bench harness updates |

**Bench gates**:
- LoCoMo overall ≥75%, multi-hop ≥70%, adversarial ≥95%, temporal ≥60%
- LME multi-session improvement ≥+15pp over PR1 baseline
- LME knowledge-update measurable (no regression vs PR1)

### PR3 — Wiki integration + LME validation (target: 2 weeks)

**Scope**: wire M8 CompileEngine into L2 via `wiki_section_provenance`; lint cron; LME-specific levers (speaker filter, supersedes path).

| Commit | Content |
|---|---|
| A | `wiki_section_provenance` populated by CompileEngine; recall fetches wiki context for picked sections |
| B | Wiki-tier-first recall precedence (M8 §6 two-tier search adapted to L1+L2+L3) |
| C | Lint cron: contradictions, stale, orphans; supersession resolution writes to `wiki_contradictions` |
| D | LME-specific: speaker tag in retain; assistant-recall keyword filter |
| E | Tests: wiki integration, lint, LME bench harness updates |

**Bench gates**:
- LME `knowledge-update` ≥+10pp over no-compile baseline (M8 W7 success criterion)
- LME full bench at parity or above Hindsight on `multi-session` and `temporal-reasoning`
- LoCoMo no regression

---

## 10. Open questions

| # | Question | Owner | Resolution by |
|---|---|---|---|
| 1 | Cross-encoder model choice for rerank — keep `ms-marco-MiniLM-L-6-v2` or upgrade? | M9 lead | PR2 commit C |
| 2 | Mode classifier output schema — single label or distribution? | M9 lead | PR2 commit D |
| 3 | LME tree shape — one document per user history, or sliced by time period? | M9 lead | PR1 commit C |
| 4 | Wiki compile threshold tuning for LME (default 50 may be wrong for chat-history shape) | M9 lead | PR3 commit A |
| 5 | Picker model — gpt-4o-mini or gpt-4o for the reranker step? | M9 lead | PR2 commit D |

---

## 11. Provenance — Phase A POC artefacts

Phase A POC (M9 prep work) lives at:
- `astrocyte-py/scripts/bench_pageindex_locomo.py` — file-based PageIndex POC, v7
- `/tmp/pageindex-smoke/` — smoke run cache + results (LoCoMo 50Q)
- `/tmp/pageindex-full/` — full run cache (LoCoMo 200Q, partial)

The Phase A picker prompts (`PICK_PROMPT_DEFAULT`, `PICK_PROMPT_TEMPORAL`, `PICK_PROMPT_LISTING`) and synth prompts (`SYNTHESIZE_PROMPT_DEFAULT`, `SYNTHESIZE_PROMPT_LISTING`) port verbatim into PR2 commit D. The mode-detection regex (`_detect_question_mode`) is replaced by an LLM classifier in PR2 but kept as a fallback when the classifier returns low confidence.

Phase A v7 results (50Q):
| Category | Score |
|---|---|
| Overall | 62% |
| adversarial | 100% |
| single-hop | 90% |
| multi-hop | 50% |
| open-domain | 22% |
| temporal | 45% |

These are PR1's regression floor: the migration to Postgres + AGE removal must not regress below v7.
