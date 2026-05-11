# In-conversation document upload — PageIndex as a chat primitive

**Status:** Design sketch. Not implemented. Targets the Synapse chat surface; Astrocyte provides the substrate.

**Companion docs:** [recall.md](recall.md), [llm-wiki-compile.md](llm-wiki-compile.md), [hindsight-informed-capabilities.md](hindsight-informed-capabilities.md), [memory-lifecycle.md](memory-lifecycle.md).

---

## 1. Why this exists

Today, "chat with a document" is solved by ad-hoc RAG: vector-index the doc, retrieve passages, stuff into context. The conversation history is in one world; the doc is in another. They don't cross-join, so the agent can't answer questions like *"is what this contract says consistent with what we discussed last month?"* without the human doing the bridging.

Astrocyte's PageIndex + Hindsight + Wiki architecture (M9–M12) is structurally the right shape for this. A chat conversation and an uploaded document are both `PageIndexDocument` rows in the same bank. The moment they coexist, every layer above the document layer cross-joins for free: section retrieval, fact search, entity index, mental models, wiki compile.

This document specifies how Synapse uses that substrate. The Astrocyte side is mostly already built; the work is mostly hot-path ingestion, scope plumbing, and UX surfacing.

---

## 2. Goals and non-goals

**Goals**

- Uploaded document becomes queryable within ≤10 seconds (section tree) and fully ingested within ≤2 minutes (facts + entities + wiki + mental models).
- Single retrieval call returns hits from conversation history *and* uploaded doc(s), ranked together.
- Cross-grain joins: entities in the doc bridge to conversation history; doc facts can `supersede`/`contradict` prior conversation facts (M12.2).
- Per-question scope control: agent (or user) can scope a query to a specific doc, all docs, the conversation, or the entire bank.
- Citations preserve provenance: every synth answer can name the source document, section line_num, and contributing fact ids.

**Non-goals**

- Whole-file embedding ("upload PDF, get one vector"). The whole point of PageIndex is structure-first navigation; we tree-build, not flatten.
- Per-doc bank isolation. Docs live in the same bank as the conversation by default; cross-doc retrieval is the value.
- Auto-deletion. Uploaded docs persist as `PageIndexDocument` rows until the user removes them; recompile-on-forget applies (see [memory-lifecycle.md](memory-lifecycle.md)).
- Real-time collaborative editing of uploaded docs. The doc is treated as a snapshot at upload time.

---

## 3. Architecture: doc + conversation share the same bank

```
Bank: synapse-thread-<id>
│
├── PageIndexDocument(id=conv,  source_id="thread-42")    ← chat history
│     ├── PageIndexSection      (one per chat session/turn-window)
│     ├── PageIndexFact         (atomic facts from chat)
│     ├── PageIndexSectionEntity (entities mentioned)
│     └── PageIndexSectionLink   (semantic_knn / supersedes / …)
│
├── PageIndexDocument(id=doc-A, source_id="upload-2026-05-11-contract.pdf")
│     ├── PageIndexSection      (tree built from PDF structure)
│     ├── PageIndexFact         (atomic facts from doc-A)
│     ├── PageIndexSectionEntity (entities mentioned in doc-A)
│     └── PageIndexSectionLink   (intra-doc + CROSS to conv sections)
│
├── PageIndexDocument(id=doc-B, source_id="upload-2026-05-11-meeting-notes.md")
│     └── … (same shape)
│
├── WikiPage(scope="document:doc-A")    ← per-doc DBSCAN compile
├── WikiPage(scope="document:conv")     ← per-conv compile
├── WikiPage(scope="bank")              ← cross-source compile (optional, expensive)
│
├── MentalModel(scope="document:doc-A") ← doc summary as 12 sentences
└── MentalModel(scope="document:conv")  ← user profile from chat
```

Three invariants matter:

1. **Same bank, multiple PageIndexDocuments.** Cross-doc joins (entity index, fact semantic search) are free because they're bank-scoped, not doc-scoped.
2. **Conversation history is itself a PageIndexDocument.** We don't bolt RAG onto chat — chat *is* a PageIndex tree, refreshed as the conversation grows.
3. **Scope is explicit at query time.** Every recall call takes an optional `document_id` (single doc) or `document_ids` (subset); omit → bank-wide.

---

## 4. Ingestion pipeline

### 4.1 Hot path (≤10s — must complete before user can query)

Stages 1–2 are blocking. The chat UI shows a "doc ready" chip when stage 2 completes.

1. **Upload + content-type sniff** (≤1s)
   - File hits Synapse backend, MIME-sniffed.
   - Routed to one of: PDF→markdown, DOCX→markdown, raw markdown, plain text. (Astrocyte side already does this for the bench scripts.)
   - Compute `content_hash`; check `SourceStore.find_document_by_hash` for dedup (existing M10 capability).

2. **Tree build** (3–8s for a typical 20-page doc)
   - `pageindex.md_to_tree(md_text)` → hierarchical sections with LLM-generated summaries.
   - Insert `PageIndexDocument` + `PageIndexSection` rows. `reference_date = upload_time`.
   - Emit `astrocyte.document.ready` event on Synapse's event bus → UI chip turns green.

### 4.2 Warm path (10s–2min — runs async, results appear progressively)

Stages 3–6 run in parallel after stage 2 returns. Each emits a progress event; the UI can show "facts ready / entities ready / wiki ready / mental models ready" sub-chips.

3. **Section embeddings** (~5–15s, batch one OpenAI embed call)
   - Embeds `title + summary` per section; populates `summary_embedding`.
   - Unblocks section-grain semantic recall.

4. **Fact extraction** (~10–60s; one LLM call per section, parallel-batched)
   - Per-section, extract up to 12 atomic facts with `fact_type`, `speaker`, `occurred_start/end`, `entities`.
   - Embed facts in a second batch call; populates `PageIndexFact.embedding`.
   - Unblocks fact-grain precision search.

5. **Section entity + link extraction** (~10–30s)
   - Per-section entity extraction → `PageIndexSectionEntity` rows.
   - Section-link KNN computes intra-doc `semantic_knn` edges.
   - **Cross-doc join:** for each new section, compute KNN against existing sections in the same bank → emit cross-doc `semantic_knn` edges between doc and conversation.

6. **Compile layers** (~30–90s)
   - DBSCAN-cluster the doc's sections → per-cluster wiki page (`scope="document:<id>"`).
   - One LLM call → up to 12 mental models per doc (`scope="document:<id>"`).
   - Bank-wide wiki recompile is *not* triggered by default (expensive); user can opt-in via "rebuild knowledge base" action.

### 4.3 Failure modes

- Stage 2 fails (LLM down, tree-build crash): document_id is still inserted, but with `built_at=NULL`. UI shows "tree build failed — retry?" chip. Stages 3–6 are skipped.
- Stage 4–6 fail: doc is still queryable at section grain (stage 2 output). Fact / wiki / mental_model retrieval falls back to skip-and-log. The bench script already follows this pattern (try/except with print + fall-through).
- Stage 5 cross-doc KNN failure doesn't block intra-doc retrieval — degrades to "no cross-doc bridging" but the doc itself works.

---

## 5. Retrieval surface

### 5.1 Scoping

Every Synapse query has a scope:

| Scope | Document_id filter | Use case |
|---|---|---|
| `@conversation` | `=conv` | "What did we discuss earlier?" |
| `@document:<id>` | `=<id>` | "What does this contract say about X?" |
| `@documents` | `IN(doc-A, doc-B, …)` | "Across all the docs I shared, find Y" |
| `@all` (default) | no filter | Bank-wide, cross-source |

Scope is set explicitly in the chat input (`/doc <id> what does X say`) or inferred by the agent based on question intent (MIP routing — see [memory-intent-protocol.md](memory-intent-protocol.md)).

### 5.2 Per-scope retrieval composition

The synth context for `@all` (the common case) layers:

```
[USER PROFILE]                          ← MentalModel.list(scope=conv)
- ...

[DOCUMENT SUMMARY: contract.pdf]        ← MentalModel.list(scope=document:doc-A)
- ...

[FACTS]                                 ← fact_rerank over PageIndexFact.search_semantic
                                          (bank-scoped, no document_id filter)
- [src=doc-A, type=experience, ...] ...
- [src=conv,  type=preference, ...] ...

[OBSERVATIONS]                          ← wiki_recall.search_wiki_pages_semantic
- [OBSERVATION: doc-A clustering result]
- [OBSERVATION: conv clustering result]

---

<section excerpts from picker output, each tagged with src + line_num>
```

The synth prompt instructs the LLM to: (a) prefer doc-sourced facts for doc-specific questions, (b) surface contradictions when conv and doc facts disagree, (c) cite source + line_num for every claim.

### 5.3 Cross-grain joins worth calling out

These are the queries that no naive RAG-over-doc design can answer:

- **Entity bridging.** "Have we discussed this person before?" → `search_facts_by_entity(bank, entity)` returns hits from doc and conv ranked together.
- **Temporal contradiction.** "Is the doc consistent with what I told you in March?" → fact temporal search returns conv-facts (March) + doc-facts (now); synth surfaces both with timestamps.
- **Supersession.** "Does this contract change anything we agreed verbally?" → M12.2 consolidation engine writes `PageIndexSectionLink(link_type='supersedes')` between conv and doc sections at upload time; query walks the supersedes edges.
- **Aggregation across sources.** "Summarize obligations across all docs and our discussion." → mental_model.list(scope=bank), or one-shot compile.

---

## 6. Synapse-side UX surface

### 6.1 Upload affordance

- Drag-and-drop or paperclip in the chat input.
- During upload: chip shows "tree-building doc-A …" with a spinner. Sections searchable as soon as the chip flips to "ready" (≤10s).
- Sub-chips appear progressively: "facts ready" / "wiki ready" / "mental model ready". User can hover for ingest timing.
- Errors surface as a red chip with a one-line reason and a "retry" button.

### 6.2 Scope toggle

In the chat input, a small dropdown (or `/` slash command) lets the user pick scope. Default `@all` (bank-wide). Examples:

- `/doc contract.pdf what's the termination clause?` → scoped to doc-A
- `/conv when did I first mention Dr. Patel?` → scoped to conv
- `what should I expect from this deal?` → @all (default)

### 6.3 Citation rendering

The agent's answer renders citation chips: `[contract.pdf §3.2 line 142]`, `[conv 2026-04-15 line 5]`. Clicking opens the source pane to that section with the contributing facts highlighted.

### 6.4 Doc management

- Doc list view shows: title, upload time, ingestion state, fact count, entity count, last accessed.
- Per-doc actions: rename, re-extract (re-run stages 4–6), delete (cascades to sections, facts, entities, links, scoped wikis and mental models — existing `memory-lifecycle.md` behaviour).

---

## 7. What we already have vs what's new

| Capability | Status | Module |
|---|---|---|
| `PageIndexDocument` table + insert | Exists | migrations 015–020, `astrocyte_postgres/pageindex_store.py` |
| `PageIndexSection` + tree build | Exists | `pageindex` upstream + `bench_pageindex_*.py` ingestion |
| Section semantic + entity + link recall | Exists | `astrocyte.pipeline.section_recall` |
| Cross-encoder section + fact rerank | Exists (M9, M12.3) | `section_rerank.py`, `fact_rerank.py` |
| Fact extraction + storage | Exists (M12.1) | `section_fact_extraction.py`, migration 020 |
| Per-doc wiki compile + scoped retrieval | Exists (M10.1) | `section_compile.py`, `wiki_recall.py` |
| Per-doc mental models | Exists (M11.2) | `mental_model_compile.py` |
| `PageIndexSectionLink.supersedes` writer | **New (M12.2 on roadmap)** | consolidation engine |
| Cross-doc `semantic_knn` edge computation | **New** | small extension to existing link extraction |
| Hot ingestion path + event bus | **New on Synapse side** | wraps existing Astrocyte calls |
| Scope toggle + citation UI | **New on Synapse side** | Synapse web/Flutter frontend |
| `astrocyte.document.ready` event | **New** | small additions to outbound transport |

Astrocyte side: ~1 week of integration work (events, M12.2 cross-doc supersedes). Synapse side: ~2 weeks for UX surface, scope plumbing, citation rendering.

---

## 8. Open design questions

1. **When is the conversation "PageIndex-tree-built" vs streamed?** Chat history is incremental — every new turn extends the markdown. Options: (a) full rebuild every N turns (simple, costly), (b) incremental section append (cheap, needs PageIndex extension), (c) rolling-window rebuild (compromise). Recommend (c) for v1.
2. **How aggressively to compute cross-doc supersedes?** All pairs is O(N²) sections; with N=50 docs × 30 sections each, that's 2.25M pairs. Practical: only compute supersedes for facts that share at least one entity AND fact_type. Drops the cost ~100×.
3. **Default scope for the agent.** If a user uploads a doc and immediately asks a question, should `@all` or `@document:<just-uploaded>` be the default? Recommend `@document:<just-uploaded>` for the next 3 turns after upload, then revert to `@all`. (UX heuristic, not a hard rule.)
4. **Cost ceiling per doc.** A 100-page doc could trigger ~300 fact-extraction LLM calls + ~30 wiki compile calls + 1 mental-model call = ~$3-5 in gpt-4o-mini at current pricing. Need a per-doc budget cap with surfacing in the UI.
5. **Multi-modal docs.** Images, tables, scanned PDFs. Out of scope for v1; rely on Synapse's existing multimodal middleware ([presentation-layer-and-multimodal-services.md](presentation-layer-and-multimodal-services.md)) to convert to markdown before handing off to Astrocyte.

---

## 9. Bench coverage

The bench scripts already exercise the substrate this design relies on: LoCoMo is "10 conversations × 200 questions over Q&A turns," structurally similar to a chat session with one uploaded doc per conversation. LongMemEval is "a user's haystack of sessions" — closer to a multi-doc chat.

If we ship this product surface, the natural bench addition is **a doc-upload eval set** where each question is anchored to (conversation context, uploaded doc, ground-truth answer). Authoring this is the right validation gate; we don't ship until our LME/LoCoMo numbers don't regress on the bench harness + the doc-upload eval passes ≥75%.

---

## 10. Next concrete steps

1. **Astrocyte:** add `astrocyte.document.ready` event + sub-events (`facts.ready`, `wiki.ready`, `mental_model.ready`). [outbound-transport.md](../_plugins/outbound-transport.md) is the surface.
2. **Astrocyte:** finalize M12.2 consolidation engine and extend it to cross-doc supersedes (not just intra-doc).
3. **Synapse:** plumb upload → Astrocyte ingestion call; render the progressive chip UX.
4. **Synapse:** add the scope toggle to chat input; wire MIP routing for implicit scope inference.
5. **Synapse:** citation chips in answer rendering, click-to-open source pane.
6. **Both:** author the doc-upload eval set (50 question×doc×conv triples) before shipping externally.

The Astrocyte data structures are ready. The work ahead is wiring, UX, and consolidation polish — not new primitives.
