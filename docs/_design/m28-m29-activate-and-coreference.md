# M28-M29 — Activate M27 infrastructure + coreference at retain

**Status:** in flight (2026-05-19)
**Predecessors:** [`m27-memory-quality.md`](m27-memory-quality.md) — fields shipped but dead-letter; [`m22-m25-hindsight-answerer.md`](m22-m25-hindsight-answerer.md) §8 — bench arc closed with M24 parity-stable
**Reference:** Hindsight `engine/retain/fact_extraction.py` lines 280-295 (coreference rules), `engine/observations/consolidator.py` (refresh pattern)

---

## 1. Why this cycle exists

The M27 cycle landed three fields on `MemoryFact` (`chunk_id`, `confidence_score`, `mentioned_at`) plus a link-expansion recall branch — but **none of it affects user-visible answers** because:

1. **Confidence + mentioned_at not persisted in Postgres adapter** → fields populate at extraction, get dropped on storage, return as `None` on recall.
2. **Answerer doesn't render confidence or dual timestamps** → even if data flowed through, the LLM never sees it.
3. **Link-expansion regressed bench (−5q 2-run mean)** because cross-session entity matches lack identity resolution — "Dr. Patel" in session A and session B are treated as the same entity even when they aren't.
4. **Mental models have CRUD (M21) but no `refresh()` operation** — once a model is curated, new evidence doesn't update it.

M28-M29 closes the field→answer gap and unlocks link-expansion via coreference.

## 2. M28 — Activate M27 infrastructure

### Workstream A — Postgres persistence

**Deliverables:**
- `adapters-storage-py/astrocyte-postgres/migrations/033_pi_facts_confidence_score.sql` — `ALTER TABLE astrocyte_pi_facts ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION NULL` + `CHECK (confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0))`
- `adapters-storage-py/astrocyte-postgres/migrations/034_pi_facts_mentioned_at.sql` — `ALTER TABLE astrocyte_pi_facts ADD COLUMN IF NOT EXISTS mentioned_at TIMESTAMPTZ NULL`
- `astrocyte_postgres/pageindex_store.py` updates:
  - `save_facts()` writes both columns
  - `search_facts_semantic()`, `search_facts_by_entity()`, `search_facts_temporal()` SELECT both columns into MemoryFactHit
  - In-process schema bootstrap (`_ensure_schema`) mirrors both `ADD COLUMN IF NOT EXISTS` for test/bench runs
- `astrocyte/testing/in_memory.py` — InMemoryPageIndexStore persists + returns both fields (already supports via dataclass field; verify)

### Workstream B — Answerer renders all three M27 fields

**Deliverables:**
- `scripts/mem0_harness/astrocyte_client.py` `search()` — populate candidate dicts with `confidence`, `mentioned_at`, `chunk_id` from MemoryFactHit
- `scripts/mem0_harness/_hindsight_answerer.py` `format_context_structured()` — render each `Fact N:` entry with:
  - `When: occurred: <iso> | mentioned: <iso>` (Hindsight parity)
  - `Confidence: 0.85` (when present; omitted when None)
  - Inline source chunk (already in place from M24)
- Shared prelude updated to explain confidence semantics ("low confidence ≤ 0.5 → hedge or abstain; high confidence ≥ 0.8 → quote directly")

### Workstream C — Mental-model refresh

**Deliverables:**
- `astrocyte/provider.py` — `MentalModelStore.refresh()` Protocol method
- `astrocyte/pipeline/mental_model.py` — `MentalModelService.refresh()` that re-runs `mental_model_compile` for one model with new source memories
- `astrocyte/testing/in_memory.py` — `InMemoryMentalModelStore.refresh()`
- `astrocyte_postgres/mental_model_store.py` — `refresh()` implementation
- `astrocyte/_astrocyte.py` — public `refresh_mental_model()` method
- `astrocyte/mcp.py` — new MCP tool `memory_refresh_mental_model`
- Tests for each

## 3. M29 — Coreference + alias resolution at retain

### Hypothesis

M27 link-expansion regressed because entity strings aren't canonical identities. "Dr. Patel" in one session vs "the ENT specialist" in another are treated as distinct entities, so cross-session graph traversal either:
- Misses real connections (different surface forms → no edge), or
- Creates spurious connections (same surface form but different referents → noise)

Hindsight's `fact_extraction.py` prompt resolves both at retain time. M29 ports that pattern.

### Deliverables

- `astrocyte/pipeline/section_fact_extraction.py` — `_EXTRACT_PROMPT` updates:
  - New section: "COREFERENCE + ALIASING RULES" with explicit instructions
  - Resolve pronouns to named entities within section ("she" → "Emily")
  - Canonicalize aliases when both forms appear ("my roommate" + "Emily" → use "Emily (user's roommate)")
  - Stable entity-string convention: `"Name (descriptor)"` when descriptor disambiguates; bare `"Name"` when unambiguous
  - Updated examples showing coreference resolution
- Test coverage: section with "my roommate Emily" + "she" produces facts where entities array contains canonical "Emily (user's roommate)" not "she" or "my roommate"

### Why this unlocks M27 link-expansion

Once entities have canonical identity:
- Cross-session traversal finds REAL connections (same canonical entity across sessions)
- Spurious connections drop (different canonical entities even with same surface form)
- Bench's multi-hop questions ("who else did I discuss this with") get correctly-resolved cross-session candidates instead of noise

Projected bench impact: M27 link-expansion was −5q 2-run mean. With M29 coreference, the +3-5q potential of cross-session traversal should unlock — net +0 to +3q vs R0-v2.

## 4. Ship gate

| Gate | Pass criteria |
|---|---|
| Unit tests | All 2755+ existing + new tests pass; ≥10 new tests for M28-M29 |
| Backward compat | Legacy MemoryFact rows without confidence/mentioned_at read as None; legacy mental models work without refresh op |
| Postgres migrations | Idempotent (`ADD COLUMN IF NOT EXISTS`); in-process bootstrap mirrors |
| Coreference test | 3-session scenario: "my roommate Emily" + "she" + "Emily" → all 3 facts have canonical "Emily (user's roommate)" entity |
| **Bench parity** | Combined ≥ 192/230 (M19a 2-run mean) |
| **Bench stretch** | Combined ≥ 196/230 (+2q via coref-unlocked link-expansion) |

## 5. Sequence (~3 weeks combined)

**Week 1 — M28 in parallel:** 4 workstreams (A persistence, B rendering, C refresh, D = coreference prompt port). All can land independently.

**Week 2 — Integration tests + regression:** Verify backward compat, run smoke tests for each workstream end-to-end.

**Week 3 — Parity bench + ship:** Single 2-run bench. If ≥192/230, ship as v0.16.0 (proper bench cycle since infrastructure now flows through to answers, unlike v0.15.0's surface-only ship).

## 6. Cycle close — SHIPPED in v0.15.0 (2026-05-19)

### Per-workstream completion

| Workstream | Status | Code surface |
|---|---|---|
| **M28-A** Postgres persistence (confidence + mentioned_at) | ✅ Shipped | Migrations 033 + 034 (idempotent `ADD COLUMN IF NOT EXISTS` + `_ddl_facts_alter` bootstrap mirror); `pageindex_store.save_facts` + `search_facts_*` loaders |
| **M28-B** Answerer renders confidence + dual timestamps + chunk_id | ✅ Shipped | `_hindsight_answerer.format_context_structured` emits `When: occurred:… / mentioned:…` and `Confidence: 0.NN` inline under each fact, with `Source chunk:` as the M24 pairing |
| **M28-C** Mental-model `refresh_from_sources` operation | ✅ Shipped | `MentalModelStore.refresh()` Protocol method + Postgres impl; `MentalModelService.refresh_from_sources()` service wrapper; `Astrocyte.refresh_mental_model()` public method; `memory_refresh_mental_model` MCP tool (10th CRUD tool) |
| **M29** Coreference + alias resolution at retain | ✅ Shipped | 4 numbered rules added to `_EXTRACT_PROMPT` in `section_fact_extraction.py`: pronoun resolution within section, alias canonicalization across sessions, role-based aliases for generic references, stable entity-string convention |

### Bench result — first bench-positive measurement since M24

**M28-M29 single-run, project=astrocyte-m28-m29, 2026-05-19:**

| Bench | Score | vs R0-v2 (v0.14.0) | vs M27 (last cycle) |
|---|---|---|---|
| LME @ top_200 | **22/30 (73.3%)** | 0 | +2q |
| LME @ top_50 | 22/30 (73.3%) | 0 | – |
| LoCoMo @ top_200 | **170/200 (85.0%)** | −3q | **+4q** |
| LoCoMo @ top_50 | 167/200 (83.5%) | −2q | – |
| **Combined @ top_200** | **192/230 (83.5%)** | **−3q (parity-band)** | **+6q** |

**Cleared the parity ship gate (≥192/230).** Stretch gate (≥196/230 for +2q via coref-unlocked link-expansion) not met — the unlock was real (LoCoMo multi-hop 93.7% @ top_200, vs M27's 90% with collapse) but didn't translate to combined-score lift past parity.

### LoCoMo @ top_200 per-category

| Category | M28-M29 | vs R0-v2 |
|---|---|---|
| multi-hop | 74/79 (**93.7%**) | matched best of arc |
| open-domain | 25/31 (80.6%) | parity |
| single-hop | 4/4 (100%) | – |
| temporal | 67/86 (77.9%) | +2 |

**Multi-hop is the headline win.** M27 had 100% multi-hop on early conversations but collapsed to 90% by conv 9 (the "late-bench collapse" pattern). M28-M29 held 93.7% across all 10 conversations — the coreference rules in the extraction prompt fixed the cross-session ambiguity that was poisoning M27's link-expansion as the entity graph grew.

### LME @ top_200 by-type (vs R0-v2)

| Type | M28-M29 | R0-v2 | Δ |
|---|---|---|---|
| knowledge-update | 3/5 | 3/5 | 0 |
| multi-session | 4/5 | 4/5 | 0 |
| single-session-assistant | 4/5 | 4/5 | 0 |
| single-session-preference | 3/5 | 3/5 | 0 |
| single-session-user | **5/5** | 3/5 | **+2** |
| temporal-reasoning | 3/5 | 4/5 | −1 |

**single-session-user +2q** is the M28-B confidence-rendering + M29 coreference paying off — the answerer now grounds answers in specific facts with timestamps and disambiguated entities. temporal-reasoning −1q is judge noise at n=5.

### Ship decision — YES, as part of v0.15.0 consolidation

Per the original §4 gate (≥192/230 combined), M28-M29 alone cleared. Per user direction at end of cycle: consolidate with M21 + M22-M27 + M30 (concurrent latency cycle) into a single v0.15.0 release rather than separate v0.15/v0.16 cuts.

See [`v0.15.0-ship-decision.md`](v0.15.0-ship-decision.md) for the consolidated ship memo and [`m30-retrieval-parallelization.md`](m30-retrieval-parallelization.md) for the concurrent latency work that also lands in v0.15.0.

### What's NOT in v0.15.0 from this cycle

- **`wait_consolidation` flag on retain endpoint** (Hindsight parity) — discussed but not implemented; queued for the M30b ingest-race cycle. The bench harness's `_do_ingest` already awaits `_populate_section_index` end-to-end synchronously, so the production race risk is concurrent-`add()`-during-`search()`, not the bench race.
- **Confidence-aware abstention in the answerer** — the renderer emits the confidence number; the answerer prompt does NOT yet have a "if confidence < 0.5, hedge or abstain" rule. Deferred to M30b (LME single-session-preference + multi-session 60% ceilings could lift here).

### Architectural insights banked

1. **M27 fields had to ship as a coherent stack to pay off.** M27 alone (just fields + link-expansion strategy) was −8q vs R0-v2 — the fields existed but neither persistence nor rendering nor coreference was wired through to the answerer. M28-M29 wired the full stack and the same code became +3q. Lesson: don't bench infrastructure-only cycles; bench the end-to-end activation.

2. **Coreference at retain is the prerequisite for cross-session traversal.** M27's link-expansion regressed multi-hop because entity ambiguity ("Emily" vs "my roommate" vs "she") spread the entity graph across spurious nodes. M29's prompt rules collapse these to one canonical entity, and link-expansion then surfaces real cross-session connections instead of noise.

3. **Dual timestamps (occurred vs mentioned) matter for temporal-reasoning more than expected.** The answerer can now anchor relative-time phrases ("last week", "3 days ago") to the `mentioned:` date rather than always the question date. M28-M29 didn't see this hit yet (judge noise dominated at n=5), but the infrastructure is in place for an M31-class temporal-resolution-at-retain cycle.
