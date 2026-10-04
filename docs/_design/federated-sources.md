# Federated sources

**Status:** Proposed 2026-10-04. Not scheduled before AML cycle 2, except that
the AML submission stays federation-free (§9). F0 (§8) is a standalone latency
fix and can land any time.

**Origin:** roadmap §9 items 13 and 14. Once agents read documents
([`anchored-documents.md`](anchored-documents.md)), the obvious next question is
where those documents live. For most teams it is not Astrocyte: it is GitHub,
Confluence, Notion, a wiki, or an existing RAG index. This design makes
Astrocyte the layer that governs knowledge wherever it lives (provenance,
freshness, trust, permissions, fusion) instead of a store that everything must
be migrated into.

**One line:** Astrocyte does not need to hold every document; it needs to know
where each one came from, whether it is still current, and who may see it.

---

## 1. What exists today

| Piece | Where | What it does |
|---|---|---|
| Proxy recall (M4.1) | `astrocyte/recall/proxy.py`, `sources.<id>.type: proxy` | GET or POST to a remote HTTP source; hits merged into recall through RRF. URL validation blocks private, loopback, and metadata addresses (SSRF guard) |
| Caller-supplied context | `recall(..., external_context=[...])` | Extra `MemoryHit`s fused with local retrieval |
| Ingestion adapters | `adapters-ingestion-py/`: document, github, s3, kafka, redis | Copy external content into a bank |
| Build-side pipelines | [`cocoindex-integration.md`](cocoindex-integration.md) (proposed) | CocoIndex as an incremental writer into the same Postgres |
| Team sharing of Astrocyte banks | [`team-memory.md`](team-memory.md) (accepted, not built) | Syncs project banks through the gateway |
| Proxied memory and erasure | [`dsar-and-proxied-memory.md`](dsar-and-proxied-memory.md) | What erasure means for content Astrocyte does not own |
| Identity and external policy | [`identity-and-external-policy.md`](identity-and-external-policy.md) | Policy home for acting on a user's behalf |

So federation is not new. What is missing is a design that keeps it fast,
accurate, and safe for teams.

### 1.1 A defect in today's proxy recall

`gather_proxy_hits_for_bank` (`recall/proxy.py:423`) queries matching sources
**one after another**, and `_make_recall_request` (`_astrocyte.py:662`) awaits it
**before local retrieval starts**. Each request uses a fixed 15 s timeout
(`_DEFAULT_TIMEOUT`), and no source can set its own. Recall latency is
therefore *local + the sum of every remote call*: three slow sources can add
45 s. For comparison, the prompt hook's budget is 1.5 s and warm local recall
takes about 10 ms ([`team-memory.md`](team-memory.md) §1). Proxy hits also all
enter fusion at a single weight (`intent_weights.semantic`,
`pipeline/recall_stage.py`), whatever their source's quality.

## 2. Three ways to plug in

| Mode | Mechanism | Strength | Weakness |
|---|---|---|---|
| **Ingest** | Copy, embed, store (today's adapters) | Fast, auditable, works offline | Stale the moment the source changes |
| **Federate** | Query the source live at recall (today's proxy) | Always fresh; no copy to govern | Slow; scores are not comparable across sources |
| **Anchor** (new) | Index locally, and keep a pointer plus the source's version for every item; check freshness against the source | Local speed with source freshness | Needs a version, etag, or content hash from the source |

**Default: anchor. Fallback: federate. Ingest only content Astrocyte owns**
(conversations, agent notes). Anchoring is the anchored-documents mechanism with
the git SHA generalized to any versioned source.

## 3. The source plugin interface

Proxy recall is one hard-coded HTTP shape. Sources become typed plugins,
discovered through entry points like LLM providers and vector stores:

```text
class RecallSource(Protocol):
    name: str
    capabilities: set[str]   # subset of {"search", "fetch", "version", "permissions", "list_changes"}

    async def search(query, *, principal, limit, deadline) -> list[SourceHit]
    async def fetch(item_id, *, principal) -> SourceDocument          # optional
    async def version(item_ids) -> dict[item_id, str]                 # optional: etag / revision / hash
    async def can_read(item_ids, *, principal) -> dict[item_id, bool] # optional
    async def list_changes(since) -> list[Change]                     # optional: drives index refresh

SourceHit:  item_id, text, url, source_score, version, author, updated_at
```

- **Capabilities are optional and declared.** A plain search API works with
  `search` alone. An anchored source needs `version`, and a team deployment
  needs `can_read` or a per-user `principal`.
- **First sources:** GitHub repository docs and ADRs, Notion, Confluence, a
  generic vector database (pgvector, Qdrant), **and MCP servers as sources**.
  MCP is already how agents reach tools, so any MCP search tool becomes a
  source without a bespoke adapter.
- **Today's `type: proxy` becomes one implementation** of this interface, so
  existing configs keep working.

## 4. Latency

**Rule 1: federation is never on the prompt hook's critical path.** The hook
reads only the local anchored index (§2). Live federation is for
agent-initiated deep search, where seconds are acceptable.

**Rule 2: fan out concurrently, under one deadline.** Remote sources run
*alongside* local retrieval, not before it. One deadline covers the whole
recall. Whatever has arrived by then is fused; late results are cached, so
the next turn gets them for free.

**Rule 3: a circuit breaker per source.** After repeated failures a source
is skipped for a cool-down. That is the same pattern that paused benchmark
ingest rather than storing degraded memories, and it stops one dead wiki
from taxing every recall.

**Rule 4: freshness checks are batched and cached.** For anchored items, one
`version()` call per source per recall covers every candidate from that
source. Results are cached per `(item, version)` for a short TTL.

**Expected effect:** hook-path recall stays at local cost plus a cached version
check, single-digit to tens of milliseconds, instead of the sum of remote
calls. Deep search is bounded by the deadline, not by the slowest source.
These are targets; F0 and F1 must measure them.

## 5. Accuracy

**The upside is coverage and authority.** Answers that live only in ADRs,
runbooks, or wikis become recallable. Human-written documents outrank
agent-inferred memories through the trust levels of
[`anchored-documents.md`](anchored-documents.md) §3.

**The risks, most of them already observed in our own data:**

1. **Scores are not comparable across sources.** RRF fuses by rank, so a weak
   source's first hit counts as much as a strong source's. Per-source fusion
   weights must be **measured** on an evaluation (§7), not guessed. Until
   then, external hits keep a conservative weight below local strategies.
2. **More candidates dilute the answer.** Our M30 evidence is that answer
   accuracy peaks near 50 candidates and degrades past it. Each source gets a
   slot budget inside the existing result cap, not an addition to it.
3. **Cross-source duplicates crowd the top-k.** The same fact in Confluence, a
   README, and a memory takes three slots. The 2026-10-04 benchmark showed
   what 22.5% duplicate rows do to a bank (roadmap §4e). Cross-source dedup
   must **merge provenance**: one hit, three sources. It must not just drop
   copies, because agreement across independent sources is a trust signal.
4. **Sources contradict each other.** A doc says X, and last week's
   conversation says not-X. Rank on currency and supersession first; surface
   a conflict rather than silently picking one
   ([`anchored-documents.md`](anchored-documents.md) §5.1).
5. **External documents go stale too,** which is the essay's own critique
   aimed at the systems we plug into. Anchoring plus version checks is the
   answer; a federated hit with no version is shown as unverified.

No accuracy claim is made for any of this until §7 measures it.

## 6. Team collaboration

**The gain:** teams keep their knowledge where it already is, and their agents
use it without a migration. This complements [`team-memory.md`](team-memory.md):
team memory shares Astrocyte's own banks, and federation leaves documents in
their system of record.

**Permissions are the crux.** Federated recall must enforce the *caller's*
rights in the source. A service account that can read everything turns
Astrocyte into a confused deputy, surfacing documents the asking user cannot
open. So:
- Searches run as the caller (`principal`), using per-user credentials. The
  per-user gateway token work (`feat/gateway-per-user-tokens`) is the
  prerequisite.
- When a source can only be searched with a shared credential, every hit is
  filtered through `can_read` for the caller before fusion. A source that
  supports neither is limited to banks whose members all hold equal access.
- Policy lives in [`identity-and-external-policy.md`](identity-and-external-policy.md).

**Provenance on every hit:** source, URL, author, version, and when it was
last verified. "Who said this, and is it still current?" is always
answerable, which is what the essay's "unauditable" critique asks for.

**Write-back as proposals.** An agent's end-of-task notes about shared
knowledge become a reviewable change in the system of record: a pull request
against the docs repository, or a draft page. They do not become private
memory. This is the essay's consult-then-update loop, scaled to a team, with
human review.

**Erasure.** Federated content is not Astrocyte's to erase. Cached copies,
index entries, and embeddings must honour deletion at the source: a
`list_changes` delete, or a failed `version` or `can_read` check, evicts them
([`dsar-and-proxied-memory.md`](dsar-and-proxied-memory.md)).

## 7. Evaluation

LongMemEval and AML exercise neither external documents nor permissions, so
federation needs its own evaluation:

1. **A docs-augmented coding eval.** A repository, its documentation and ADRs,
   and tasks whose answers live in the docs, in memory, or in both. Run paired
   arms: memory only, docs only (federated), and anchored fusion. AML's
   coding-memory track (repository history) is the closest public analogue.
2. **Latency under failure.** Recall latency with one source slow, one dead,
   and one healthy, which checks rules 2 and 3.
3. **Permission correctness.** Two principals with different source rights.
   The pass bar is zero cross-principal leakage, run in CI.
4. **Freshness.** Edit a source document after it is indexed, then measure how
   often the stale version is served without a flag.

The roadmap's measurement rules apply: paired items, replicate judge passes,
and error bars (roadmap §9 item 9).

## 8. Phasing

| Phase | Scope | Exit criterion |
|---|---|---|
| **F0** | Proxy recall concurrent with local retrieval: one deadline, per-source timeout and circuit breaker, partial results | Latency-under-failure test passes; a slow source no longer adds its full timeout |
| **F1** | `RecallSource` interface; `type: proxy` ported onto it; first sources: GitHub docs, one wiki (Notion or Confluence), generic vector DB, MCP | Existing proxy configs unchanged; each source passes a conformance suite |
| **F2** | Calibration: measured per-source weights, slot budgets, cross-source dedup that merges provenance | Docs-augmented eval shows fusion no worse than the best single arm |
| **F3** | Anchored external documents: anchors are URL plus version; batched freshness checks | Freshness eval: stale items served unflagged trend to zero |
| **F4** | Team: per-principal search, `can_read` filtering, provenance in hits, write-back as reviewed proposals | Permission eval: zero leakage |

F0 is independent and small. F1 can run alongside the anchored-documents P1.
F4 depends on per-user gateway tokens and team memory G1.

## 9. Non-goals and boundaries

- **Not in the AML submission.** AML's Add/Search contract assumes everything
  arrives through Add; configuring sources for a submission would be outside
  the contract. The submission config stays federation-free.
- **Not a search engine.** Astrocyte governs and fuses; it does not crawl,
  rank the web, or replace a team's existing search
  ([`cocoindex-integration.md`](cocoindex-integration.md) §2 makes the same
  boundary argument for build-side pipelines).
- **Not cross-organization federation** in v1, matching team memory's
  non-goals.

## 10. Open questions

- **Calibration without labels.** Per-source weights need relevance judgments.
  Click-through from agent use (which injected hits were cited in the answer)
  may be enough, but it must be validated against the paired evaluation.
- **Sources without versions.** A content hash at index time detects change
  only on re-fetch. Decide how often unversioned sources are re-checked.
- **Rate limits and cost on hosted APIs.** Notion and Confluence throttle.
  Freshness batching (rule 4) helps; budgets per source may also be needed.
- **MCP as a source.** MCP tools return free text with no versions or
  permissions model. They may be useful only in federate mode, never
  anchored.
