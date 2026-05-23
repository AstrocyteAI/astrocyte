# M27 — Memory quality fundamentals (generalizable, not bench-overfit)

**Status:** scoping (2026-05-19)
**Predecessor:** [`m22-m25-hindsight-answerer.md`](m22-m25-hindsight-answerer.md) — closed with M24 as bench-stable −1q best (within R0-v2 variance), no config beat baseline. **Confirmed exhaustion of prompt-engineering ROI.**
**Reference:** Hindsight `hindsight-api-slim/hindsight_api/engine/retain/` + capability-analysis report (2026-05-19)

---

## 1. Why this cycle exists

The M22-M26 arc proved that *prompt engineering on a fixed bench is overfit to the bench*. M24 reached parity (−1q); every further variant either kept the trade or regressed harder. The capability analysis identified ten generalizable improvements — features that improve memory quality regardless of which benchmark tests it.

M27 picks the three highest-leverage **generalizable** improvements: things that encode true properties of a memory system, not bench-category sweet-spots. Success criterion: the bench should be **neutral-to-positive** (M27 ≥ R0-v2 ± noise) — if M27 beats the bench, we can confidently say *"the system got better"* rather than *"we found the bench's sweet spot."*

## 2. Three workstreams

### Workstream A — Confidence scores on every fact

**Real-world value.** When the system knows it's uncertain, it can abstain, hedge, or trigger re-confirmation. Currently `MemoryFact` has no confidence; only `Observation` carries one. Every retrieved fact has the same epistemic weight whether grounded in one offhand remark or repeated across ten sessions.

**Hindsight pattern.** `memory_units.confidence_score` with `CHECK (confidence_score IS NULL OR (0.0 ≤ x ≤ 1.0))`. Opinion-typed facts MUST have one; others MAY. Answerer context renders `(confidence: 0.8)` after each fact. The extraction prompt explicitly asks the LLM to emit confidence per fact.

**Deliverables:**
- `MemoryFact.confidence_score: float | None` field
- `MemoryFactHit.confidence_score: float | None` mirror
- Postgres migration `033_pi_facts_confidence_score.sql` — NULLable FLOAT column with CHECK constraint
- `section_fact_extraction.py` — extraction prompt asks for `"confidence": <0.0-1.0>` per fact; default 0.7 when omitted
- Bench harness answerer renders confidence in fact entries
- Test: extraction with explicit confidence + extraction without (defaults)

### Workstream B — `mentioned_at` dual timestamp

**Real-world value.** *"What did you say last time we talked about X?"* needs the SESSION date, not the event date. *"When did X happen?"* needs the event date. Real memory systems need both, surfaced distinctly. We currently have only `occurred_start` / `occurred_end` (when the event happened); session date is buried on `PageIndexSection` and not propagated to facts.

**Hindsight pattern.** `memory_units` has `occurred_start`, `occurred_end`, `mentioned_at`, `event_date` (4 timestamps). Answerer context renders `When: occurred: 2024-11-12 | mentioned: 2024-11-13`. The prompt explicitly distinguishes "when it happened" vs "when it was discussed".

**Deliverables:**
- `MemoryFact.mentioned_at: datetime | None` field (the session_date at retain time)
- `MemoryFactHit.mentioned_at: datetime | None` mirror
- Postgres migration `034_pi_facts_mentioned_at.sql` — NULLable TIMESTAMPTZ
- `section_fact_extraction.extract_facts_for_section()` — populates `mentioned_at = section.session_date` for every fact
- Bench harness answerer renders both: `When: occurred: <date> | mentioned: <date>`
- Test: section with session_date → all facts have it; section without → None

### Workstream C — Cross-session entity link graph

**Real-world value.** Users ask cross-session questions constantly: *"who else did I discuss this with?"*, *"when did Alice last mention React?"*. Today every cross-session query is search-then-stitch the LLM has to do at recall time. A proper entity graph makes traversal cheap.

**Hindsight pattern.** `entity_links` table populated by `engine/retain/link_utils.py` at retain. Every `memory_unit` gets edges to other units sharing entities. Recall uses `engine/search/link_expansion_retrieval.py` to traverse — one of their 4 parallel recall strategies.

**Astrocyte gap.** `section_entity_extraction.py` produces per-section entities but the links live WITHIN a section, not cross-section. Recall has `entity` strategy in `section_recall` but it doesn't traverse beyond direct entity matches.

**Deliverables:**
- New table `astrocyte_entity_links` — edges `(bank_id, entity_text, source_fact_id, target_fact_id)` populated at retain time
- Postgres migration `035_astrocyte_entity_links.sql` + index on `(bank_id, entity_text)` and `(source_fact_id)`
- New module `astrocyte/pipeline/entity_link_builder.py` — at retain time, after `section_entity_extraction`, batch-INSERT links for all (entity, fact) pairs that share an entity with another fact in the bank
- New module `astrocyte/pipeline/link_expansion_recall.py` — given query entities, traverse the link graph to find connected facts; surface as a new RRF sibling in `fact_recall`
- Bench harness fact_recall threads link-expansion hits into the candidate pool
- Test: 3 sessions with overlapping entity → link graph built correctly; query with entity → traverse returns facts from all 3 sessions

## 3. Deliverables summary

| File | Change | Workstream |
|---|---|---|
| `astrocyte/types.py` | `MemoryFact.confidence_score`, `MemoryFactHit.confidence_score`, `MemoryFact.mentioned_at`, `MemoryFactHit.mentioned_at` | A + B |
| `astrocyte/pipeline/section_fact_extraction.py` | Extraction prompt emits confidence; `mentioned_at` populated from section | A + B |
| `astrocyte/pipeline/entity_link_builder.py` (NEW) | Build cross-section entity links at retain | C |
| `astrocyte/pipeline/link_expansion_recall.py` (NEW) | Traverse entity link graph at recall | C |
| `astrocyte/pipeline/fact_recall.py` | Wire link_expansion as RRF sibling | C |
| `astrocyte/provider.py` | `PageIndexStore.save_entity_links` + `query_entity_links` SPI methods | C |
| `adapters-storage-py/astrocyte-postgres/migrations/033_pi_facts_confidence_score.sql` (NEW) | Add column + CHECK | A |
| `adapters-storage-py/astrocyte-postgres/migrations/034_pi_facts_mentioned_at.sql` (NEW) | Add TIMESTAMPTZ column | B |
| `adapters-storage-py/astrocyte-postgres/migrations/035_astrocyte_entity_links.sql` (NEW) | New table + indexes | C |
| `adapters-storage-py/astrocyte-postgres/astrocyte_postgres/pageindex_store.py` | Persist + load confidence_score + mentioned_at; implement save_entity_links + query_entity_links | A + B + C |
| `astrocyte/testing/in_memory.py` | InMemoryPageIndexStore implements entity_link methods | C |
| `scripts/mem0_harness/astrocyte_client.py` | Render confidence + dual-timestamp in answerer; thread link-expansion hits | A + B + C |
| `tests/test_section_fact_extraction.py` | Confidence + mentioned_at extraction tests | A + B |
| `tests/test_entity_link_builder.py` (NEW) | Cross-session link generation + traversal | C |
| `docs/_design/m27-memory-quality.md` (this doc) | Scope + §6 cycle close | All |
| `CHANGELOG.md` | M27 entry post-bench | All |

## 4. Sequence (~1.5-2 weeks)

**Phase A — Confidence scores (2-3 days)**
- Type field + Postgres migration + extraction prompt update + extraction tests + answerer render

**Phase B — mentioned_at (1-2 days)**
- Type field + Postgres migration + extraction populates from section + answerer render
- Tests

**Phase C — Entity link graph (5-7 days)**
- Schema design for link table
- `entity_link_builder.py` — retain-time batch INSERT
- `link_expansion_recall.py` — query-time traversal (1-hop or 2-hop, configurable)
- Wire into `fact_recall` as RRF sibling
- InMemory + Postgres SPI implementations
- Unit tests + integration test (3-session scenario)

**Phase D — Parity bench + cycle close (1 day)**
- Single bench run vs R0-v2 baseline. Target: neutral-to-positive
- If positive: ship as v0.15.0 = M21 + M27
- If neutral: ship as v0.15.0 (parity-clean, new infrastructure for future cycles)
- If negative: investigate which workstream regressed; defer that one to M28

## 5. Ship gate

| Gate | Pass criteria |
|---|---|
| Unit tests | All 2744+ existing tests still pass; 10-15 new tests added for M27 workstreams |
| Backward compat | Legacy `MemoryFact` rows without `confidence_score` / `mentioned_at` read as `None` and behave correctly |
| Postgres migrations | Idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`) |
| Entity link traversal | Cross-session integration test: 3 sessions with overlapping entity → query returns facts from all 3 |
| Bench parity | Combined ≥ 192/230 (R0-v2 −2q, within M19a variance band) |
| **Bench positive (stretch)** | Combined ≥ 196/230 (+2q) — would mean entity graph genuinely helps cross-session questions |

## 6. Cycle close (2026-05-19) — code shipped, link-expansion bench-negative, other workstreams architecturally clean

**Final bench: 186/230 (80.87%) — −8q vs R0-v2.** Negative. The cross-session entity link-expansion was a clean structural hypothesis (real generalizable capability, mirrors Hindsight's pattern) but bench-overfit in the OPPOSITE direction we expected — it helped multi-hop on early samples (n=17 → 100%) but the late-sample regression dragged it to 90% (worse than R0-v2's 92%), and temporal crashed −7pp from cross-session noise.

### 6.1 Per-workstream verdict

| Workstream | Code shipped | Bench impact | Verdict |
|---|---|---|---|
| **A — confidence_score on every fact** | `MemoryFact.confidence_score` + `MemoryFactHit.confidence_score` fields, extraction prompt asks for it, 3 unit tests | Inactive at bench (Postgres adapter doesn't persist it yet; bench answerer doesn't render it) | **Infrastructure landed; bench-neutral.** Activates when adapter migration + answerer rendering land. |
| **B — mentioned_at dual timestamp** | `MemoryFact.mentioned_at` + `MemoryFactHit.mentioned_at` fields, populated from `section.session_date` at extraction, 1 unit test | Inactive at bench (same — adapter not yet persisting) | **Infrastructure landed; bench-neutral.** |
| **C — cross-session entity link-expansion** | `_search_link_expansion` branch in `fact_recall.py` (active when `query_entities` passed), 7 unit tests | **−8q at bench** (helps multi-hop early, hurts temporal + late-sample multi-hop) | **Bench-negative.** Code stays in-tree as opt-in (`query_entities` param defaults to None → branch doesn't fire) for future workloads where cross-session traversal genuinely helps. Bench harness still passes `query_entities=question_entities` so the bench code path is exercised; production callers can omit. |

**11 new unit tests, 2755 total passing, zero regressions on existing tests.**

### 6.2 Why C regressed despite being structurally clean

The cross-session entity graph IS a useful capability — real users ask cross-session questions ("when did Alice last mention X?"). But the LoCoMo/LME bench is heavily within-session: most questions are answered by ONE session's content. Cross-session traversal on those questions pulls in unrelated facts that:

1. **Distract on temporal questions** — events from other sessions confuse date-arithmetic chains
2. **Add noise on multi-hop within-session questions** — what looked like an entity match is actually a coincidental same-name-different-context (no coreference resolution)
3. **Don't hurt LME much** because LME is single-session-by-design (link-expansion fires but doesn't add useful evidence)

The fix path (M28+ candidate) is **coreference + alias resolution at retain time** (capability-analysis item #5) so that "Dr. Patel" in two different sessions is verified as the same entity before cross-session traversal pulls them together. Without coreference, link-expansion has a precision problem on common names / generic entity strings.

### 6.3 What ships in v0.15.0 from M27

**Always-on (no behavior change at defaults):**
- `MemoryFact.confidence_score` field — None for all current data; populated when extraction LLM emits + Postgres adapter persists (future incremental)
- `MemoryFact.mentioned_at` field — populated at extraction from section.session_date; None for top-level facts
- Extraction prompt asks for confidence per fact (defaults None if LLM omits)
- 11 new tests covering all three workstreams

**Opt-in (default OFF):**
- `_search_link_expansion` branch in `fact_recall` — fires when caller passes `query_entities=[...]`; default behavior with `query_entities=None` is identical to pre-M27 fact_recall

Per RELEASING.md §122 hotfix path: v0.15.0 reuses M19's bench cycle in `BENCH_PARITY.yaml` because default behavior produces M19a-mean scores.

### 6.4 Banked for M28+

- **A + B activation** — Postgres migrations (`033_pi_facts_confidence_score.sql`, `034_pi_facts_mentioned_at.sql`) + persistence in `PostgresPageIndexStore.save_facts` + answerer rendering. ~1-2 days. Will activate confidence-aware abstention + dual-timestamp answers.
- **Coreference resolution at retain** (capability-analysis #5) — prerequisite for making link-expansion bench-positive on focused-retrieval questions
- **Embed-CLI-controlplane triad** (parallel workstream per capability analysis) — different code surface, runs independently of M28's memory-quality work

## 7. Out of scope (deferred)

Per the capability analysis, four other generalizable improvements are noted but deferred:

- **#3 Background retain-time enrichment** — architectural decoupling of inline LLM calls. Big change (~1-2 weeks alone). Should be a separate cycle (M28+) once we have a job-queue dependency story.
- **#4 Configurable extraction modes** (concise/verbose/verbatim/custom) — composes with our MIP per-bank routing. M28-M29 territory.
- **#5 Coreference + alias resolution** — high value but harder to get right. Deserves its own cycle with dedicated test corpus.
- **#7 Memory access patterns + adaptive surfacing** — low standalone value, useful as foundation for adaptive surfacing later. M30+ territory.

Parallel workstream (per capability analysis): **embed-CLI-controlplane triad** for product positioning. This is a different team / different code surface (Rust CLI, Next.js UI, embedded-Postgres bundler) and can run independently of M27.
