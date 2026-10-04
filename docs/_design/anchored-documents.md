# Anchored documents

**Status:** Proposed 2026-10-04. Not scheduled before AML cycle 2. This changes
the coding-agent product surface; it is not a benchmark lever, and it does not
touch the AML submission.

**Origin:** the critique recorded in
[`sota-roadmap-m45-m48.md`](sota-roadmap-m45-m48.md) §9 item 13. Kevin Liao's
["Agents Don't Need Memory. They Need Documentation."](https://liao.gg/blog/agents-dont-need-memory)
(2026-10-03) argues that memory plugins are "just RAG" and proposes a Markdown
brain the agent reads before a task and updates after it. This document keeps
his best point, that documents are the right interface, and keeps what his
approach discards: evidence, time, and scale.

**One line:** the agent reads a brain, and every claim in the brain can show
where it came from and whether it is still true.

---

## 0. Decisions (2026-10-04)

Committed publicly in Calvin's reply to the essay,
["Agents Need Documents They Can Check"](https://calvinx.com/blog/2026-Oct-04/agents-need-documents-they-can-check) (2026-10-04), whose "What this means for Astrocyte"
section states the direction below. Nothing in it is built yet; it follows the
AML cycle 2 work.

1. **Astrocyte is the evidence layer behind documents, not a rival to them.**
2. **The every-prompt similarity hook stops being the default.** It is replaced
   by a table of contents at session start plus the claims anchored to the file
   the agent opens; similarity stays as a fallback (§4).
3. **Capture stays automatic.** Turns and work are retained as evidence (§7).
4. **What the agent knows is readable pages,** with no database query needed
   (§5.5).
5. **Agents draft, people approve, in tools the team already uses.** Repository
   Markdown pull requests first, then suggestions in documentation tools
   (Confluence, Notion). **Astrocyte does not build its own review UI** (§7).
6. **Work with existing documents, including operator-memory's Markdown,** and
   add sources and staleness checks to them rather than asking teams to move.
7. **Draw on search and RAG systems teams already run,** treating their results
   as evidence with sources, never as raw text passed to the agent
   ([`federated-sources.md`](federated-sources.md)).
8. **Results before claims.** Build the essay's documents-only system as the
   baseline and publish the comparison with error bars, whichever way it goes
   (§9).

**Positioning.** Memory is low effort and low control: it is trusted by its
results. Documentation is high effort and high control: it is trusted by
inspection. Astrocyte aims at the empty corner, captured as easily as memory
and checked as easily as a document. Integrations deliver the control half
only if they write back for human review and carry provenance in both
directions.

**Prerequisite for every phase:** retain must persist each chunk's extracted
metadata. On 2026-10-04 we found that `retain()` discarded it (dates, fact
type, when/where/who), keeping only chunk text and entities; a fix is in
progress. Anchors, staleness, and trust all depend on dates and sources
surviving storage.

## 1. The critiques this must answer

From the essay, against memory systems:

1. Similarity is not correctness or currency.
2. Snippets are stored without their context.
3. The past is treated as truth, though the world (the code) keeps changing.
4. Agents cannot search for what they do not know exists.
5. The store cannot be audited.

From our reading (§9 item 13), against documentation-only memory:

6. Documents written by an agent drift.
7. An LLM rewrite loses information silently and leaves no provenance.
8. Concurrent agents conflict when they edit the same file.
9. Once the brain outgrows the context window, "consult the index" becomes
   retrieval again.
10. Updating documents after every task costs tokens in the foreground loop.

A design that answers 1–5 by becoming 6–10 has not improved anything. Both
lists are requirements.

## 2. Shape: two layers and a contract

```
              reads (default)                 cites / expands on demand
   agent ─────────────────────▶ DOCUMENT LAYER ─────────────────────▶ EVIDENCE LAYER
     │                          pages → sections → claims                verbatim chunks
     │                          each claim: citations, anchors,          occurred_at / retained_at
     │                          trust, validity, supersession            speaker, session, provenance
     │                                   ▲                               append-only
     └── end of task: proposes patches ──┘                                    ▲
                                                                              │
                         every turn is retained as evidence ──────────────────┘
```

**Evidence layer.** What Astrocyte already is: verbatim chunks with
bitemporal stamps, session, speaker, and provenance. It is append-only: a
memory is superseded, never rewritten. It is the source of truth, and the
agent does not see it raw by default.

**Document layer.** Markdown pages for decisions, specs, constraints, how-tos,
and preferences. It grows out of the existing wiki tier
([`llm-wiki-compile.md`](llm-wiki-compile.md)) and the OKF export (roadmap
§6.1). The unit of truth is the **claim**, a statement inside a section that
carries its own metadata (§3).

**The contract.**
- The agent reads documents. Evidence is fetched only to justify or expand a
  claim.
- Every claim cites evidence. A claim without evidence is an *orphan* and is
  linted (§5.5).
- The database is an index and cache over the document and evidence layers. It
  can be rebuilt from them, so auditing the documents audits the system.

## 3. The claim model

```text
Claim
  id              stable across edits
  page, section   where it lives
  text            the statement the agent reads
  citations[]     evidence ids (chunk ids) supporting it
  anchors[]       what it describes in the world (below)
  trust           human > agent_verified > agent_inferred > extracted
  valid_from      when it became true (domain time)
  valid_to        when it stopped being true; null while current
  superseded_by   claim id, when replaced
  status          current | suspect | contradicted | superseded
  last_verified   time and anchor state at the last verification
```

**Anchor kinds**
- **code**: `{repo, path, commit_sha, span_hash}`. The span hash covers the
  lines the claim describes, so a rename or move is detectable and a no-op
  commit is not.
- **entity**: `{entity_id}`, for people, services, or libraries resolved by
  the existing entity resolution.
- **time**: the validity interval alone, for facts with no external referent
  (plans, preferences).

Granularity is the claim, not the page. A page-level timestamp says the page
changed; a claim-level anchor says *which statement* may now be wrong.

## 4. Read path: event-driven recall plus a pushed map

Today the `prompt` hook injects similarity-ranked memories on every
non-trivial prompt, which is exactly the "lottery" the essay describes. The
read path replaces it with three mechanisms, in order of precedence.

1. **Session start: the map.** Inject a compact table of contents of the
   brain: page titles, one-line summaries, and a count of suspect claims, in a
   few hundred tokens. The `session-start` hook already injects "a short
   summary of this project's memory"; this upgrades it to a real index. It
   answers critique 4: the agent cannot ask about what it does not know
   exists, but it can see a map.
2. **On file touch: anchored injection.** When the agent reads or edits
   `src/auth/session.py`, a tool hook injects the claims anchored to that
   path, with their status. The match is exact, not by similarity, and it is
   triggered by *what the agent is doing*, not by what it typed. **This is the
   central move.** For most coding work it replaces similarity ranking with a
   deterministic lookup. It needs a new hook event; our harness hooks only
   session start, prompt, and stop today.
3. **Fallback: query-driven retrieval.** Similarity retrieval remains for the
   long tail, but it searches documents first and evidence second. The current
   `prompt` hook becomes this fallback.

Ranking within any of the three is **currency and authority first,
similarity second**: current before suspect, human before inferred, and a
superseded claim only when explicitly asked for history.

## 5. Mechanisms, critique by critique

### 5.1 "Similarity is not correctness or currency"
- Validity intervals and supersession links on every claim; ranking as in §4.
- **Write-time conflict check.** A new claim is compared with the current
  claims on the same anchor and classified as *same / supersedes / contradicts
  / unrelated*. Cosine similarity picks the candidates; a decision model makes
  the call (roadmap §9 item 11 ranks this kind of call site). *Same* is a
  no-op. *Supersedes* closes the old claim's validity interval. *Contradicts*
  is never merged silently: both claims become `contradicted` and the page
  gets an open item.

### 5.2 "Snippets lose their context"
- The page *is* the context: motivation, constraints, and history sit together
  in one section.
- Any claim expands to its cited verbatim evidence on demand. This is the
  reconstruction-at-recall stage (roadmap §9 item 4) aimed at documents
  rather than chunks.

### 5.3 "The past is treated as truth" — anchoring to the world
- At recall, every code-anchored claim is checked cheaply: did any anchored
  path change between `commit_sha` and `HEAD`, and does the span hash still
  match?
  - No change: shown as `current`.
  - Changed: shown flagged, for example *"may be stale: `auth/session.py`
    changed since a1b2c3d"*, status `suspect`, and a re-verification task is
    queued.
- **Re-verification** reads the current code and the claim. The outcome is
  confirm (refresh the anchor, set `last_verified`), supersede (write the
  corrected claim, close the old one), or escalate (a contradiction for a
  human).
- Claims with no external referent decay by type: a plan goes suspect in
  days, a preference in months, a decision never; it stays current until
  superseded.
- Cost control: results are cached per `(path, sha)`, so one `git diff` per
  path per HEAD serves every claim anchored there.

This turns "how accurate are the 500 snippets about authentication?" into a
question the system answers mechanically.

### 5.4 "Agents cannot search for what they do not know"
Answered by the read path (§4): the pushed map plus anchored injection on file
touch. Query-driven retrieval is no longer the primary mechanism.

### 5.5 "The store cannot be audited"
- Documents are Markdown in the repository, reviewed like code. Roadmap §9
  item 8 (git-native audit) already plans this.
- The existing wiki lint (`astrocyte_wiki_lint_issues`) gains four checks:
  - **stale**: an anchor changed and the claim has not been re-verified;
  - **orphan**: no citations;
  - **contradicted**: an open conflict;
  - **dead**: never injected or retrieved in N sessions. This needs durable
    per-claim access counts. Today `UtilityTracker` counts recalls in memory
    only: it is process-local, LRU-bounded, and lost on restart.
- "Which memories exist, which are stale, which were never used, which are
  wrong" becomes a lint report, not a SQL investigation.

## 6. Covering documentation's own failure modes

| Failure (§1) | Mechanism |
|---|---|
| 6. Documents drift | Anchor checks at recall flag drift automatically; re-verification repairs it |
| 7. A rewrite loses information silently | Edits are **patches to sections with citations**. Removing a claim requires a supersession record. Free-form page rewrites are not a write operation |
| 8. Concurrent agents conflict | Section-level ownership; documents merge through git like code |
| 9. Outgrows the context window | The agent never reads the whole brain: map plus anchored injection plus document-first retrieval |
| 10. Foreground update cost | A "did anything change?" check at end of task skips no-op updates; only touched sections are patched |

## 7. Write path

1. **Every turn is retained as evidence.** The `stop` hook already spools
   finished turns.
2. **At end of task, while the agent still holds full context,** it proposes
   patches. This borrows the essay's best idea. Each patch adds, supersedes,
   or re-verifies claims, with citations to the turns that justify it.
   **Patches are delivered where the team already reviews** (decision 5): a
   pull request against Markdown in the repository first, then suggestions in
   documentation tools such as Confluence or Notion. Astrocyte builds no review
   UI of its own; approval happens in the team's tool, and Astrocyte records
   the outcome.
3. **Its own task summary is retained as evidence,** alongside the extracted
   facts. A summary written with full context is better evidence than facts
   extracted from a transcript afterwards.
4. **Patches pass the policy layer before they are written:** the PII barrier,
   the conflict check (§5.1), and trust assignment (`agent_inferred` unless the
   claim cites a human statement or a re-verification against code).
   Documents committed to a repository are published to everyone with access
   to it, so the barrier is mandatory here, not optional.
5. **Background consolidation stays,** but only for the episodic long tail
   that nobody will curate.

## 8. What exists and what is new

| Piece | State |
|---|---|
| Verbatim evidence with bitemporal stamps | exists (retain path, `occurred_at` / `retained_at`) |
| Wiki pages, revisions, compile | exists ([`llm-wiki-compile.md`](llm-wiki-compile.md)) |
| Wiki lint table | exists; the four checks in §5.5 are new |
| OKF Markdown export | exists (roadmap §6.1, phase 1) |
| Session summary injection, stop-hook spooling | exists (`astrocyte/harness/hooks.py`) |
| Entity resolution | exists |
| Recall counting | in memory only (`UtilityTracker`); durable counts are new |
| Claim model with citations, anchors, trust, validity | **new** |
| Anchor checks at recall, re-verification task | **new** |
| File-touch hook and anchored injection | **new**: needs a new hook event in the harness |
| Write-time conflict classification | **new**: decision-model call site |
| Patch-based write path at end of task | **new** |
| Delivery as pull requests, then doc-tool suggestions | **new** (decision 5) |
| Reading operator-memory's Markdown brain as documents, adding anchors | **new** (decision 6) |
| Per-chunk extraction metadata persisted by `retain()` | **prerequisite**: discarded until 2026-10-04; fix in progress |

## 9. Evaluation — the essay offers none, so this must

1. **Build the essay's system as a baseline:** a Markdown brain only, no
   retrieval, the agent instructed to consult and update. It is cheap, and it
   is the only honest test of the claim.
2. **AML's coding-memory track is the matched test:** reuse engineering
   experience from earlier work in the same repository, across time-constrained
   historical tasks. Compare four arms: documents only, memory only, this
   hybrid, and **federated** (the hybrid drawing on a team's existing
   documentation and RAG systems, [`federated-sources.md`](federated-sources.md)).
   Report accuracy with confidence intervals and **latency p50/p95** per arm,
   and publish the comparison whichever way it comes out (decision 8).
3. **A staleness benchmark, which nobody publishes:** write memories, change
   the code underneath them, then measure how often a stale memory misleads
   the agent. This tests critique 3 directly, and anchor checks should drive
   the misleading rate toward zero.
4. **Cost and latency alongside accuracy,** per task. "Token-burning" is a
   measurable claim, and so is the foreground cost of end-of-task updates.

The roadmap's measurement rules apply: paired comparisons on identical items,
replicate judge passes, and no number reported without its error bar (roadmap
§9 item 9).

## 10. Phasing

All phases depend on the prerequisite in §0: extracted metadata must survive
`retain()`.

| Phase | Scope | Exit criterion |
|---|---|---|
| **P0** | Documents-only baseline; staleness benchmark | Both runnable; baseline numbers with error bars |
| **P1** | Claim model; code anchors and checks at recall; file-touch injection; the map at session start | Staleness misleading rate measurably below baseline |
| **P2** | Patch-based write path at end of task, delivered as repository pull requests; citations; trust; write-time conflict check; operator-memory adapter | No silent claim loss across a long run (every removal has a supersession record) |
| **P3** | Lint checks (§5.5); durable access counts; audit report | Report answers "stale / orphan / contradicted / dead" without SQL |

P1 delivers the largest change in behaviour and reuses the most existing
parts, so it goes first after the baseline.

**P0 status (2026-10-04): both runnable, first numbers in.**
- *Documents-only baseline:* `astrocyte_aml/docs_baseline.py`. Operator
  Memory's model as a separate system on the AML Add/Search contract, so it
  runs through the same harness, items, and judge as the Astrocyte arms, with
  the same LLM provider configuration. Message timestamps are "now" for each
  message, matching the reference date Astrocyte's extraction is given; a
  document is never overwritten unread.
- *Staleness benchmark:* `aml_selfeval/staleness.py`. A git repository of
  configuration facts is stated in conversation, then half the files change
  silently in a second commit. Scoring is deterministic, by exact value match:
  changed facts are stale-unflagged, flagged, current, or missing; unchanged
  facts measure recall. Systems receive the repository path and HEAD, so an
  anchored system can check them.
- *First run* (24 facts, seed 42, Claude Haiku via `claude -p` for both
  systems, Astrocyte with structured extraction off; 12 facts per group, so
  directional only):

  | | changed facts served stale, unflagged | changed facts missing | unchanged facts recalled |
  |---|---|---|---|
  | Astrocyte (no anchors) | 10/12 (83%, CI 55–95%) | 2/12 | 10/12 (83%, CI 55–95%) |
  | Documents only | 6/12 (50%, CI 25–75%) | 6/12 | 5/12 (42%, CI 19–68%) |

  Neither system flags a stale fact or knows a new value, which is the
  expected baseline without anchors. The documents-only system looks less
  stale only because it **lost information**: its brain ended as one
  986-character document holding 12 of the 24 facts it was told. A re-run
  after ruling out an unread overwrite in the baseline gave the same result,
  so the loss is the rewrite itself, not the harness. That is critique 7
  ("an LLM rewrite loses information silently"), measured once, on one model.
  The P1 exit criterion is now concrete: drive *stale, unflagged* toward zero
  while holding unchanged-fact recall.

## 11. Open questions and risks

- **Claim granularity.** Sentences are too fine to maintain, pages too coarse
  to anchor. The working assumption is a short bullet-level statement;
  validate it on P0 data.
- **Anchor cost on large repositories.** Caching per `(path, sha)` bounds it,
  but a monorepo with frequent commits could still make checks at recall
  noticeable. Measure in P1, with a budget.
- **Workspaces without git, and multiple repositories.** Code anchors assume a
  repository; content hashes can stand in for SHAs elsewhere.
- **Review burden.** If every agent patch needs a human, nobody will review
  them. Trust levels exist so that only contradictions and human-trust edits
  demand attention.
- **Trust inflation.** An agent re-verifying its own inferred claim should not
  promote it to `human`. Only a human statement or a re-verification against
  code raises trust.
- **Privacy.** Committed documents leave the memory store's access control.
  The PII barrier on patches (§7) is load-bearing, and some banks may need
  documents that are never committed.

## 12. Non-goals

- Not a replacement for episodic memory. Conversational and multi-user memory
  (LongMemEval-style: something said in session 37 of 500) has no author for
  documents and stays on the evidence layer.
- Not an AML lever. AML's Add/Search contract has no file-touch events and no
  documents; nothing here changes the submission.
- Not a rewrite of retrieval. Similarity retrieval stays as the fallback; it
  stops being the primary mechanism.
