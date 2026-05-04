# Memory API reference

Complete reference for Astrocyte's core memory operations -- retain, recall, reflect, forget, history, audit, compile, graph, import, and export -- via Python library and REST gateway.

---

## Quick overview

| Operation | Purpose | Python method | HTTP endpoint |
|-----------|---------|---------------|---------------|
| **retain** | Store content into memory | `astrocyte.retain()` | `POST /v1/retain` |
| **recall** | Retrieve relevant memories | `astrocyte.recall()` | `POST /v1/recall` |
| **reflect** | Synthesize an answer from memory | `astrocyte.reflect()` | `POST /v1/reflect` |
| **forget** | Remove memories | `astrocyte.forget()` | `POST /v1/forget` |
| **history** | Recall memories as they existed at a past point in time | `astrocyte.history()` | `POST /v1/history` |
| **audit** | LLM-judged coverage analysis for a memory bank | `astrocyte.audit()` | `POST /v1/audit` |
| **compile** | Build durable wiki pages from retained memories | `astrocyte.compile()` | `POST /v1/compile` |
| **graph search** | Search entities in the graph store | `astrocyte.graph_search()` | `POST /v1/graph/search` |
| **graph neighbors** | Traverse graph-linked memories | `astrocyte.graph_neighbors()` | `POST /v1/graph/neighbors` |
| **export/import** | Move bank contents via AMA JSONL | `astrocyte.export_bank()` / `astrocyte.import_bank()` | `POST /v1/export`, `POST /v1/import` |

---

## retain() -- Store content into memory

Ingests content, extracts facts, deduplicates, and stores into a memory bank.

### Python signature

```python
async def retain(
    self,
    content: str,
    bank_id: str,
    *,
    metadata: dict[str, str | int | float | bool | None] | None = None,
    tags: list[str] | None = None,
    context: AstrocyteContext | None = None,
    content_type: str = "text",
    extraction_profile: str | None = None,
    occurred_at: datetime | None = None,
    source: str | None = None,
    pii_detected: bool = False,
) -> RetainResult:
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `content` | `str` | The text content to store. |
| `bank_id` | `str` | Target memory bank identifier. |
| `metadata` | `dict` | Optional key-value pairs attached to the memory. |
| `tags` | `list[str]` | Optional tags for categorisation and filtering. |
| `context` | `AstrocyteContext` | Optional actor identity and session context. |
| `content_type` | `str` | Content format hint. Defaults to `"text"`. |
| `extraction_profile` | `str` | Named extraction profile to control fact extraction behaviour. |
| `occurred_at` | `datetime` | Timestamp of when the content originally occurred. |
| `source` | `str` | Free-text source label (e.g. `"slack"`, `"meeting-notes"`). |
| `pii_detected` | `bool` | Flag indicating the content contains PII. Defaults to `False`. |

### RetainResult

| Field | Type | Description |
|-------|------|-------------|
| `stored` | `bool` | Whether the content was stored. |
| `memory_id` | `str \| None` | ID of the created memory, if stored. |
| `deduplicated` | `bool` | Whether the content was merged with an existing memory. |
| `error` | `str \| None` | Error message if the operation failed. |
| `retention_action` | `str \| None` | Action taken by the retention pipeline (e.g. `"created"`, `"merged"`). |
| `curated` | `bool` | Whether curation rules were applied. |
| `memory_layer` | `str \| None` | Layer the memory was stored in (e.g. `"episodic"`, `"semantic"`). |

### Python example

```python
from astrocyte import Astrocyte

ast = Astrocyte.from_config("astrocyte.yaml")

result = await ast.retain(
    content="Customer prefers dark-mode UI and weekly email digests.",
    bank_id="user-prefs",
    tags=["ui", "notifications"],
    metadata={"customer_id": "cust_8291"},
    source="support-ticket",
)

print(result.memory_id)   # "mem_3f8a..."
print(result.deduplicated) # False
```

### REST equivalent

```
POST /v1/retain
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "content": "Customer prefers dark-mode UI and weekly email digests.",
  "bank_id": "user-prefs",
  "metadata": {"customer_id": "cust_8291"},
  "tags": ["ui", "notifications"]
}
```

### curl example

```bash
curl -X POST https://gateway.example.com/v1/retain \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "content": "Customer prefers dark-mode UI and weekly email digests.",
    "bank_id": "user-prefs",
    "tags": ["ui", "notifications"],
    "metadata": {"customer_id": "cust_8291"}
  }'
```

---

## recall() -- Retrieve relevant memories

Searches one or more memory banks and returns ranked hits.

### Python signature

```python
async def recall(
    self,
    query: str,
    bank_id: str | None = None,
    *,
    banks: list[str] | None = None,
    strategy: Literal["cascade", "parallel", "first_match"] | MultiBankStrategy | None = None,
    max_results: int = 10,
    max_tokens: int | None = None,
    tags: list[str] | None = None,
    context: AstrocyteContext | None = None,
    external_context: list[MemoryHit] | None = None,
    fact_types: list[str] | None = None,
    time_range: tuple[datetime, datetime] | None = None,
    include_sources: bool = False,
    layer_weights: dict[str, float] | None = None,
    detail_level: str | None = None,
    as_of: datetime | None = None,
) -> RecallResult:
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str` | Natural-language search query. |
| `bank_id` | `str` | Single bank to search. Mutually exclusive with `banks`. |
| `banks` | `list[str]` | Multiple banks to search. |
| `strategy` | `str \| MultiBankStrategy` | Multi-bank search strategy: `"cascade"`, `"parallel"`, or `"first_match"`. |
| `max_results` | `int` | Maximum number of hits to return. Defaults to `10`. |
| `max_tokens` | `int` | Token budget for returned content. |
| `tags` | `list[str]` | Filter results to memories matching these tags. |
| `context` | `AstrocyteContext` | Optional actor identity and session context. |
| `external_context` | `list[MemoryHit]` | Additional context to blend into results. |
| `fact_types` | `list[str]` | Filter to specific fact types (e.g. `["preference", "event"]`). |
| `time_range` | `tuple[datetime, datetime]` | Restrict results to a time window. |
| `include_sources` | `bool` | Include source metadata in each hit. Defaults to `False`. |
| `layer_weights` | `dict[str, float]` | Per-layer scoring weights (e.g. `{"semantic": 1.2, "episodic": 0.8}`). |
| `detail_level` | `str` | Controls verbosity of returned text. |
| `as_of` | `datetime \| None` | Time-travel filter. When set, only memories whose `retained_at` is on or before this UTC timestamp are returned. Defaults to `None` (no filter). See also [`history()`](#history----recall-a-point-in-time-snapshot). |

### RecallResult

| Field | Type | Description |
|-------|------|-------------|
| `hits` | `list[MemoryHit]` | Ranked list of matching memories. |
| `total_available` | `int` | Total matches before `max_results` truncation. |
| `truncated` | `bool` | Whether results were truncated. |
| `trace` | `RecallTrace \| None` | Diagnostic trace of the recall pipeline. |
| `authority_context` | `str \| None` | Authority resolution context, if applicable. |

### RecallTrace

`RecallTrace` is the per-query diagnostic surface — what strategies ran, how long each took, and which tier resolved the query. Useful for benchmark analysis, latency debugging, and observability dashboards.

| Field | Type | Description |
|-------|------|-------------|
| `strategies_used` | `list[str] \| None` | Ordered list of strategies that contributed candidates (e.g. `["semantic", "keyword", "graph", "temporal"]`). |
| `total_candidates` | `int \| None` | Total candidates seen across all strategies, pre-fusion. |
| `fusion_method` | `str \| None` | `"rrf"` for reciprocal-rank fusion; future fusion algorithms set their own label. |
| `latency_ms` | `float \| None` | End-to-end recall latency, in milliseconds. |
| `strategy_timings_ms` | `dict[str, float] \| None` | Per-strategy latency breakdown — useful when one arm dominates the budget. |
| `strategy_candidate_counts` | `dict[str, int] \| None` | Per-strategy candidate count contributed to fusion. |
| `tier_used` | `int \| None` | Which retrieval tier resolved the query (`1` for cache, `2` for fast path, etc.). |
| `layer_distribution` | `dict[str, int] \| None` | Hit count by memory layer — e.g. `{"fact": 5, "observation": 3, "model": 1}`. Visible when observation consolidation or mental models are enabled. |
| `cache_hit` | `bool \| None` | `True` when the recall cache satisfied the query without running retrieval strategies. |
| `wiki_tier_used` | `bool \| None` | `True` when the wiki tier (M8 W5) resolved the query (compiled wiki page surfaced before raw recall). |

### MemoryHit

| Field | Type | Description |
|-------|------|-------------|
| `memory_id` | `str` | Unique memory identifier. |
| `text` | `str` | Memory content. |
| `score` | `float` | Relevance score. |
| `bank_id` | `str` | Bank the memory belongs to. |
| `metadata` | `dict` | Attached metadata. |
| `tags` | `list[str]` | Tags on the memory. |
| `occurred_at` | `datetime \| None` | When the content originally occurred (domain time, caller-supplied). |
| `retained_at` | `datetime \| None` | UTC wall-clock time when this memory was stored. Populated by the retain pipeline; use with `as_of` for time-travel queries. |
| `source` | `str \| None` | Source label. |
| `memory_layer` | `str \| None` | Layer (e.g. `"episodic"`, `"semantic"`). |

### Python examples

**Single bank:**

```python
result = await ast.recall("What UI theme does the customer prefer?", bank_id="user-prefs")
for hit in result.hits:
    print(f"[{hit.score:.2f}] {hit.text}")
```

**Multi-bank parallel:**

```python
result = await ast.recall(
    "deployment issues last week",
    banks=["incidents", "runbooks", "team-notes"],
    strategy="parallel",
    max_results=20,
)
```

**Multi-bank cascade** (searches banks in order, stops when enough results are found):

```python
result = await ast.recall(
    "API rate limit policy",
    banks=["policies", "runbooks", "general"],
    strategy="cascade",
)
```

**Tag filtering:**

```python
result = await ast.recall(
    "onboarding steps",
    bank_id="docs",
    tags=["onboarding", "getting-started"],
)
```

**Time range filtering:**

```python
from datetime import datetime, timedelta

week_ago = datetime.now() - timedelta(days=7)
result = await ast.recall(
    "customer complaints",
    bank_id="support",
    time_range=(week_ago, datetime.now()),
)
```

### curl example

```bash
curl -X POST https://gateway.example.com/v1/recall \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "query": "What UI theme does the customer prefer?",
    "bank_id": "user-prefs",
    "max_results": 5
  }'
```

**Multi-bank:**

```bash
curl -X POST https://gateway.example.com/v1/recall \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "query": "deployment issues last week",
    "banks": ["incidents", "runbooks", "team-notes"],
    "strategy": "parallel",
    "max_results": 20
  }'
```

---

## history() -- Recall a point-in-time snapshot

Returns the memories that existed in a bank **at a specific past moment** — only memories whose `retained_at` timestamp is on or before `as_of` are included. Use this for post-mortems, compliance audits, and debugging ("what did the agent believe on date X?").

For most use cases `history()` is the right call. If you need finer control (multi-bank queries, layer weights, etc.) pass `as_of` directly to `recall()` instead.

### Python signature

```python
async def history(
    self,
    query: str,
    bank_id: str,
    as_of: datetime,
    *,
    max_results: int = 10,
    max_tokens: int | None = None,
    tags: list[str] | None = None,
) -> HistoryResult:
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str` | Natural-language search query to run against the historical snapshot. |
| `bank_id` | `str` | Bank to query. |
| `as_of` | `datetime` | UTC timestamp. Memories retained after this moment are excluded. Must be timezone-aware. |
| `max_results` | `int` | Maximum number of hits to return. Defaults to `10`. |
| `max_tokens` | `int` | Optional token budget for the result set. |
| `tags` | `list[str]` | Optional tag filter applied on top of the time filter. |

### HistoryResult

| Field | Type | Description |
|-------|------|-------------|
| `hits` | `list[MemoryHit]` | Ranked memories visible at `as_of`. |
| `total_available` | `int` | Total matches before `max_results` truncation. |
| `truncated` | `bool` | Whether results were truncated. |
| `as_of` | `datetime` | The timestamp used for the snapshot query (echoed back for traceability). |
| `bank_id` | `str` | The bank that was queried. |
| `trace` | `RecallTrace \| None` | Diagnostic trace of the recall pipeline. |

Each `MemoryHit` in `hits` includes a `retained_at` field showing exactly when that memory was stored.

### Python examples

**Post-mortem — what did the agent know on 1 Jan 2025?**

```python
from datetime import datetime, UTC

snapshot = await ast.history(
    "What did we know about Alice's employer?",
    bank_id="user-alice",
    as_of=datetime(2025, 1, 1, tzinfo=UTC),
)

print(f"Snapshot at: {snapshot.as_of}")
for hit in snapshot.hits:
    print(f"  retained={hit.retained_at:%Y-%m-%d}  {hit.text}")
```

**Compliance audit — memory state before a policy change:**

```python
from datetime import datetime, UTC

policy_change = datetime(2025, 6, 1, tzinfo=UTC)
snapshot = await ast.history(
    "user consent preferences",
    bank_id="gdpr-audit",
    as_of=policy_change,
    tags=["consent"],
)

for hit in snapshot.hits:
    print(hit.memory_id, hit.retained_at, hit.text)
```

**Check what changed between two snapshots:**

```python
from datetime import datetime, UTC

before = await ast.history("Alice employment", bank_id="contacts", as_of=datetime(2025, 1, 1, tzinfo=UTC))
after  = await ast.history("Alice employment", bank_id="contacts", as_of=datetime(2025, 6, 1, tzinfo=UTC))

before_ids = {h.memory_id for h in before.hits}
after_ids  = {h.memory_id for h in after.hits}

print("Added:  ", after_ids - before_ids)
print("Removed:", before_ids - after_ids)
```

### REST equivalent

```
POST /v1/history
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "query": "What did we know about Alice's employer?",
  "bank_id": "user-alice",
  "as_of": "2025-01-01T00:00:00Z",
  "max_results": 10
}
```

### curl example

```bash
curl -X POST https://gateway.example.com/v1/history \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "query": "What did we know about Alice'\''s employer?",
    "bank_id": "user-alice",
    "as_of": "2025-01-01T00:00:00Z"
  }'
```

---

## audit() -- Gap analysis for a memory bank

Scans a memory bank and asks an LLM judge to identify **topical gaps** — subject areas that are missing, thin, or outdated given the purpose of the bank. Use `audit()` to surface coverage blind spots before they affect recall quality.

> **Requires**: a Tier 1 pipeline with an LLM provider. `Astrocyte.audit()` recalls candidate memories, then asks the configured LLM judge to identify gaps.

### Python signature

```python
async def audit(
    self,
    scope: str,
    bank_id: str,
    *,
    max_memories: int = 50,
    max_tokens: int | None = None,
    tags: list[str] | None = None,
) -> AuditResult:
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `scope` | `str` | Natural-language description of what this bank is _supposed_ to cover. The LLM judge uses this to evaluate coverage. Example: `"customer support interactions for ACME Corp"`. |
| `bank_id` | `str` | Bank to audit. |
| `max_memories` | `int` | Maximum number of recent memories to feed to the judge. Defaults to `50`. Increase for thorough audits; lower for faster / cheaper scans. |
| `max_tokens` | `int` | Optional token budget. When set, only memories that fit within this budget are passed to the judge. |
| `tags` | `list[str]` | Optional tag filter — only memories with at least one of these tags are included in the sample. |

### AuditResult

| Field | Type | Description |
|-------|------|-------------|
| `scope` | `str` | The scope string passed to `audit()`. |
| `bank_id` | `str` | The bank that was audited. |
| `coverage_score` | `float` | 0.0–1.0 score from the LLM judge. 1.0 = excellent coverage, 0.0 = severely lacking. |
| `gaps` | `list[GapItem]` | Identified gap areas, ordered by severity. Empty when coverage is complete. |
| `memories_scanned` | `int` | Number of memories the judge actually saw. |
| `trace` | `RecallTrace \| None` | Diagnostic trace of the underlying recall used to sample memories. |

### GapItem

| Field | Type | Allowed values |
|-------|------|----------------|
| `topic` | `str` | Short label for the missing topic area. |
| `severity` | `str` | `"high"`, `"medium"`, or `"low"` |
| `reason` | `str` | One-sentence explanation of why the gap matters. |

### Python examples

**Quick coverage check:**

```python
from astrocyte import Astrocyte

brain = Astrocyte.from_config("astrocyte.yaml")
result = await brain.audit(
    "customer support tickets and resolutions for ACME Corp",
    bank_id="support-acme",
)

print(f"Coverage score: {result.coverage_score:.0%}")
for gap in result.gaps:
    print(f"  [{gap.severity.upper()}] {gap.topic} — {gap.reason}")
```

**Scheduled weekly audit with alerting:**

```python
import asyncio
from astrocyte import Astrocyte

async def weekly_audit():
    brain = Astrocyte.from_config("astrocyte.yaml")
    result = await brain.audit(
        "internal engineering runbooks and incident postmortems",
        bank_id="eng-runbooks",
        max_memories=100,
    )

    high_gaps = [g for g in result.gaps if g.severity == "high"]
    if high_gaps:
        print(f"⚠️  {len(high_gaps)} high-severity gaps found (score: {result.coverage_score:.0%})")
        for g in high_gaps:
            print(f"   • {g.topic}: {g.reason}")
    else:
        print(f"✅ Coverage looks good ({result.coverage_score:.0%})")

asyncio.run(weekly_audit())
```

**Tag-scoped audit — only check onboarding content:**

```python
result = await brain.audit(
    "employee onboarding guides and HR policies",
    bank_id="hr-docs",
    tags=["onboarding"],
    max_memories=30,
)
```

### REST equivalent

```
POST /v1/audit
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "scope": "customer support tickets and resolutions for ACME Corp",
  "bank_id": "support-acme",
  "max_memories": 50
}
```

Response:

```json
{
  "scope": "customer support tickets and resolutions for ACME Corp",
  "bank_id": "support-acme",
  "coverage_score": 0.62,
  "memories_scanned": 47,
  "gaps": [
    {
      "topic": "Billing disputes",
      "severity": "high",
      "reason": "No memories found covering refund or chargeback resolution procedures."
    },
    {
      "topic": "Escalation paths",
      "severity": "medium",
      "reason": "Only one memory references the escalation process; edge cases are absent."
    }
  ]
}
```

### curl example

```bash
curl -X POST https://gateway.example.com/v1/audit \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "scope": "customer support tickets and resolutions for ACME Corp",
    "bank_id": "support-acme",
    "max_memories": 50
  }'
```

---

## reflect() -- Synthesize an answer from memory

Recalls relevant memories and synthesizes a natural-language answer. Use `reflect` when you want an interpreted response rather than raw hits.

### Python signature

```python
async def reflect(
    self,
    query: str,
    bank_id: str | None = None,
    *,
    banks: list[str] | None = None,
    strategy: Literal["cascade", "parallel", "first_match"] | MultiBankStrategy | None = None,
    max_tokens: int | None = None,
    tags: list[str] | None = None,
    context: AstrocyteContext | None = None,
    include_sources: bool = True,
    dispositions: Any | None = None,
) -> ReflectResult:
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str` | Natural-language question to answer from memory. |
| `bank_id` | `str` | Single bank to search. Mutually exclusive with `banks`. |
| `banks` | `list[str]` | Multiple banks to search. |
| `strategy` | `str \| MultiBankStrategy` | Multi-bank search strategy. |
| `max_tokens` | `int` | Token budget for the synthesised answer. |
| `tags` | `list[str]` | Filter source memories by tag. |
| `context` | `AstrocyteContext` | Optional actor identity and session context. |
| `include_sources` | `bool` | Return the source memories used. Defaults to `True`. |
| `dispositions` | `Any` | Optional disposition hints for answer style. |

### ReflectResult

| Field | Type | Description |
|-------|------|-------------|
| `answer` | `str` | Synthesised natural-language answer. |
| `confidence` | `float \| None` | Confidence score between 0 and 1. |
| `sources` | `list[MemoryHit] \| None` | Source memories the answer was derived from. |
| `observations` | `list[str] \| None` | Additional observations from the synthesis. |
| `authority_context` | `str \| None` | Authority resolution context, if applicable. |

### Python example

```python
result = await ast.reflect(
    "What are this customer's communication preferences?",
    bank_id="user-prefs",
    include_sources=True,
)

print(result.answer)
# "The customer prefers dark-mode UI and weekly email digests."

print(result.confidence)
# 0.92

for src in result.sources:
    print(f"  - {src.memory_id}: {src.text[:60]}...")
```

### curl example

```bash
curl -X POST https://gateway.example.com/v1/reflect \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "query": "What are this customer'\''s communication preferences?",
    "bank_id": "user-prefs",
    "max_tokens": 200,
    "include_sources": true
  }'
```

---

## forget() -- Remove memories

Deletes or archives memories from a bank. Supports targeted deletion by ID, by tag, by date, or full bank wipe.

### Python signature

```python
async def forget(
    self,
    bank_id: str,
    *,
    memory_ids: list[str] | None = None,
    tags: list[str] | None = None,
    scope: str | None = None,
    context: AstrocyteContext | None = None,
    compliance: bool = False,
    reason: str | None = None,
    before_date: datetime | None = None,
) -> ForgetResult:
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `bank_id` | `str` | Bank to delete from. |
| `memory_ids` | `list[str]` | Specific memory IDs to delete. |
| `tags` | `list[str]` | Delete all memories matching these tags. |
| `scope` | `str` | Set to `"all"` to delete every memory in the bank. |
| `context` | `AstrocyteContext` | Optional actor identity and session context. |
| `compliance` | `bool` | Mark as a compliance-driven deletion (logged for audit). Defaults to `False`. |
| `reason` | `str` | Free-text reason for the deletion (stored in audit log). |
| `before_date` | `datetime` | Delete memories with `occurred_at` before this date. |

### ForgetResult

| Field | Type | Description |
|-------|------|-------------|
| `deleted_count` | `int` | Number of memories permanently deleted. |
| `archived_count` | `int` | Number of memories archived instead of deleted. |

### Python examples

**Delete specific IDs:**

```python
result = await ast.forget(
    "user-prefs",
    memory_ids=["mem_3f8a", "mem_91cb"],
)
print(result.deleted_count)  # 2
```

**Delete by tag:**

```python
result = await ast.forget("user-prefs", tags=["deprecated"])
```

**Delete all memories in a bank:**

```python
result = await ast.forget("user-prefs", scope="all")
```

**Compliance deletion with reason:**

```python
result = await ast.forget(
    "user-prefs",
    memory_ids=["mem_3f8a"],
    compliance=True,
    reason="GDPR erasure request #4821",
)
```

**Delete before a date:**

```python
from datetime import datetime

result = await ast.forget(
    "logs",
    before_date=datetime(2025, 1, 1),
    reason="Retention policy: remove data older than 1 year",
)
```

### curl example

```bash
curl -X POST https://gateway.example.com/v1/forget \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "bank_id": "user-prefs",
    "memory_ids": ["mem_3f8a", "mem_91cb"]
  }'
```

**Delete by tag:**

```bash
curl -X POST https://gateway.example.com/v1/forget \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "bank_id": "user-prefs",
    "tags": ["deprecated"]
  }'
```

---

## compile() -- Build wiki pages from memory

Compiles raw memories in a bank into durable, topic-oriented wiki pages. This is optional and requires a configured `WikiStore` plus Tier 1 pipeline.

### Python signature

```python
async def compile(
    self,
    bank_id: str,
    *,
    scope: str | None = None,
) -> CompileResult:
```

### REST equivalent

```json
{
  "bank_id": "engineering",
  "scope": "deployment"
}
```

```bash
curl -X POST https://gateway.example.com/v1/compile \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{"bank_id": "engineering", "scope": "deployment"}'
```

---

## Mental models — M9

Mental models are **curated, scope-bound summaries** persisted as a first-class
SPI alongside raw memories, observations, and wiki pages. Hindsight inspired
the layer; Astrocyte's M9 ships them with explicit revision history, scope keys
(`bank` / `tag:<name>` / `entity:<id>`), and source-id provenance so revisions
trace back to the underlying memories.

Use them when you have a stable "what does this user generally believe / want /
prefer about X" answer that the agentic-reflect loop should consult **before**
falling back to raw recall. Open-domain questions ("Would Alice still pursue
counseling after the move?") synthesize faster against a mental-model summary
than they do against the raw memory pool.

The Postgres reference adapter (`astrocyte-postgres`) ships
`PostgresMentalModelStore` registered under the `astrocyte.mental_model_stores`
entry-point group; configure with `mental_model_store: postgres` in
`astrocyte.yaml`. See [`provider-spi.md`](/plugins/provider-spi/) for the SPI
shape and [`mental-models.md`](/plugins/mental-models/) for storage detail.

### Python — set the store

```python
from astrocyte import Astrocyte

brain = Astrocyte.from_config("astrocyte.yaml")
# brain.set_mental_model_store(...) is wired automatically when
# `mental_model_store: postgres` is set in config; you can also pass
# a custom MentalModelStore implementation directly.
```

### REST — create

```bash
curl -X POST https://gateway.example.com/v1/mental-models \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "bank_id": "user-alice",
    "model_id": "preferences-v1",
    "title": "Alice's preferences",
    "content": "Alice prefers async communication, dark mode, and Python over Go.",
    "scope": "bank",
    "source_ids": ["mem-1", "mem-2", "mem-3"]
  }'
```

`scope` is one of `"bank"` (default — applies to the whole bank),
`"tag:<name>"`, or `"entity:<id>"`. `source_ids` is an optional list of memory
IDs the model summarizes; they are persisted as provenance and used by
observation-style invalidation when the underlying memories change.

### REST — list / get / refresh / delete

```bash
# List all models in a bank (optionally filtered by scope)
curl -G https://gateway.example.com/v1/mental-models \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  --data-urlencode "bank_id=user-alice" \
  --data-urlencode "scope=tag:preferences"

# Get one
curl https://gateway.example.com/v1/mental-models/preferences-v1?bank_id=user-alice \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN"

# Refresh — creates a new revision, preserves history
curl -X POST https://gateway.example.com/v1/mental-models/preferences-v1/refresh \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{
    "bank_id": "user-alice",
    "content": "Updated: Alice now prefers Slack over email.",
    "source_ids": ["mem-7", "mem-8"]
  }'

# Delete (requires ``forget`` permission, not ``write`` — destructive)
curl -X DELETE https://gateway.example.com/v1/mental-models/preferences-v1?bank_id=user-alice \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN"
```

### Observation invalidation companion

Mental models share the same scope+invalidation model as observations. When the
memories that produced a mental model are updated or forgotten, you can
explicitly invalidate dependent observations through:

```bash
curl -X POST https://gateway.example.com/v1/observations/invalidate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{"bank_id": "user-alice", "source_ids": ["mem-1", "mem-2"]}'
```

Like `/v1/forget` and `/v1/mental-models/{id}` (delete), this endpoint
requires the `forget` permission rather than `write` — the operation is
destructive of derived rows.

---

## Entity graph — M11

Astrocyte can maintain a **knowledge graph of named entities** alongside your vector memories. When entity extraction is enabled, `retain()` automatically identifies entities (people, companies, projects, etc.) and stores them in a graph store. The entity graph powers cross-bank relationship queries and alias-of deduplication (entity resolution).

> **Requires**: a `GraphStore` backend. The built-in `InMemoryGraphStore` is available for testing; production deployments use `astrocyte-age` (Apache AGE / PostgreSQL) or `astrocyte-neo4j` (Neo4j).

### Entity types

#### Entity

Represents a named thing in the knowledge graph.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Stable identifier, unique within a bank. |
| `name` | `str` | Human-readable display name. |
| `entity_type` | `str` | Category label, e.g. `"PERSON"`, `"ORG"`, `"PROJECT"`. |
| `metadata` | `dict \| None` | Arbitrary key-value metadata. |

#### EntityLink

Represents a directed relationship between two entities.

| Field | Type | Description |
|-------|------|-------------|
| `entity_a` | `str` | ID of the source entity. |
| `entity_b` | `str` | ID of the target entity. |
| `link_type` | `str` | Relationship type, e.g. `"alias_of"`, `"co_occurs_with"`, `"reports_to"`. |
| `evidence` | `str` | Free-text passage that supports this link. Defaults to `""`. |
| `confidence` | `float` | 0.0–1.0 confidence score. Defaults to `1.0`. |
| `created_at` | `datetime \| None` | When the link was created. Set automatically when stored. |
| `metadata` | `dict \| None` | Arbitrary key-value metadata. |

### Entity resolution (alias detection)

When `EntityResolver` is wired into the orchestrator, each `retain()` call runs an **alias-of** check in the background: new entities are compared against existing candidates and, if an LLM judge confirms they refer to the same real-world entity, an `alias_of` link is created automatically.

```python
from astrocyte import Astrocyte

# Enable entity resolution by setting graph_store + resolver in astrocyte.yaml:
#   graph_store:
#     type: age                    # or neo4j / in_memory
#     dsn: postgresql://...
#   entity_resolution:
#     enabled: true
#     similarity_threshold: 0.8   # vector similarity for candidate lookup
#     confirmation_threshold: 0.75 # LLM confidence to accept the alias

brain = Astrocyte.from_config("astrocyte.yaml")

# retain() runs extraction + resolution automatically
await brain.retain(
    "Alice Smith (formerly Alice Johnson) joined the platform team.",
    bank_id="people",
)
```

### Querying the entity graph (advanced)

For direct graph access use the `GraphStore` SPI in your own orchestrator. The `Astrocyte` class also exposes graph convenience methods for common searches.

**Find entity candidates by name:**

```python
entities = await brain.graph_search("Alice", bank_id="people", limit=5)
for c in entities:
    print(c.id, c.name, c.entity_type)
```

**Query neighbors — which memories mention entities related to a given entity?**

```python
from astrocyte.types import MemoryEntityAssociation

hits = await brain.graph_neighbors(
    entity_ids=["person:alice-smith"],
    bank_id="people",
    limit=20,
)
for h in hits:
    print(h.memory_id, h.text)
```

**Manually store a link:**

```python
from astrocyte.types import EntityLink

link = EntityLink(
    entity_a="person:alice-smith",
    entity_b="person:alice-johnson",
    link_type="alias_of",
    evidence="Alice Smith (formerly Alice Johnson)",
    confidence=0.95,
)
# For custom links, use a configured GraphStore adapter directly.
link_id = await graph_store.store_entity_link(link, bank_id="people")
```

### REST equivalents

```bash
curl -X POST https://gateway.example.com/v1/graph/search \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{"query": "Alice", "bank_id": "people", "limit": 5}'

curl -X POST https://gateway.example.com/v1/graph/neighbors \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{"entity_ids": ["person:alice-smith"], "bank_id": "people", "max_depth": 2, "limit": 20}'
```

### Setting up `astrocyte-age` (PostgreSQL + Apache AGE)

`astrocyte-age` is the recommended open-source graph backend. It uses Apache AGE on PostgreSQL 16 and supports pgvector on the same instance.

**1. Start the database:**

```bash
docker compose -f astrocyte-services-py/docker-compose-age.yml up -d
```

**2. Run migrations:**

```bash
docker compose -f astrocyte-services-py/docker-compose-age.yml \
    exec postgres psql -U astrocyte -d astrocyte \
    -f /migrations/001_age_extension.sql \
    -f /migrations/002_graph.sql \
    -f /migrations/003_memory_entity_map.sql
```

**3. Configure `astrocyte.yaml`:**

```yaml
graph_store:
  type: age
  dsn: postgresql://astrocyte:astrocyte@localhost:5434/astrocyte
  graph_name: astrocyte          # AGE graph name — created automatically
entity_resolution:
  enabled: true
```

**4. Install the adapter:**

```bash
pip install astrocyte-age
```

**Running integration tests:**

```bash
ASTROCYTE_AGE_TEST_DSN=postgresql://astrocyte:astrocyte@localhost:5434/astrocyte \
    uv run pytest adapters-storage-py/astrocyte-age/tests/ -v
```

---

## export_bank() and import_bank() -- Move memories

Exports and imports a bank using Astrocyte Memory Archive (AMA) JSONL. These operations are intended for operations, backup, migration, and provider portability. They require admin access when access control is enabled.

### Python signatures

```python
async def export_bank(
    self,
    bank_id: str,
    path: str,
    *,
    include_embeddings: bool = False,
    include_entities: bool = True,
    context: AstrocyteContext | None = None,
) -> int:
```

```python
async def import_bank(
    self,
    bank_id: str,
    path: str,
    *,
    on_conflict: str = "skip",
    context: AstrocyteContext | None = None,
    progress_fn: Any = None,
) -> ImportResult:
```

### REST equivalents

The standalone gateway reads and writes paths on the server filesystem:

```bash
curl -X POST https://gateway.example.com/v1/export \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{"bank_id": "user-prefs", "path": "/backups/user-prefs.ama.jsonl"}'

curl -X POST https://gateway.example.com/v1/import \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASTROCYTE_TOKEN" \
  -d '{"bank_id": "user-prefs", "path": "/backups/user-prefs.ama.jsonl", "on_conflict": "skip"}'
```

---

## Initialization

### From configuration file

```python
from astrocyte import Astrocyte

ast = Astrocyte.from_config("astrocyte.yaml")
```

The YAML file defines storage backends, memory banks, extraction profiles, and gateway settings. See [Configuration reference](configuration-reference/) for the full schema.

### Programmatic construction

```python
from astrocyte import Astrocyte

ast = Astrocyte.from_config_dict({
    "provider_tier": "storage",
    "vector_store": "in_memory",
    "llm_provider": "mock",
})
```

---

## Context and identity

Core operations accept an optional `context` parameter carrying caller identity.

```python
from astrocyte import AstrocyteContext, ActorIdentity

ctx = AstrocyteContext(
    principal="user:user_42",
    actor=ActorIdentity(type="user", id="user_42"),
    tenant_id="tenant_acme",
)

result = await ast.retain(
    content="...",
    bank_id="notes",
    context=ctx,
)
```

The context is used for access control enforcement and audit logging. When omitted, the operation runs with the default identity configured in `astrocyte.yaml`.

See [Access control setup](access-control-setup/) for role-based and attribute-based access policies.

---

## Error handling

Astrocyte raises typed exceptions for common failure modes.

| Exception | HTTP status | When |
|-----------|-------------|------|
| `AccessDenied` | 403 | The actor lacks permission for the requested operation or bank. |
| `RateLimited` | 429 | Too many requests. Retry after the duration in the `retry_after` field. |
| `BankNotFound` | 404 | The specified `bank_id` does not exist. |
| `ValidationError` | 400 | Invalid parameters (e.g. empty content, unknown bank in list). |

### Python error handling

```python
from astrocyte.exceptions import AccessDenied, RateLimited

try:
    result = await ast.recall("sensitive data", bank_id="restricted")
except AccessDenied as e:
    print(f"Permission denied: {e}")
except RateLimited as e:
    print(f"Rate limited. Retry after {e.retry_after}s")
```

### REST error responses

Error responses follow a consistent JSON structure:

```json
{
  "error": {
    "code": "access_denied",
    "message": "Actor user_42 lacks read access to bank 'restricted'."
  }
}
```

---

## Further reading

- [Configuration reference](configuration-reference/) — YAML schema for banks, storage, and extraction profiles
- [Bank management](bank-management/) — multi-bank queries, patterns, and lifecycle
- [Authentication setup](authentication-setup/) — token issuance, API keys, and OIDC integration
- [Access control setup](access-control-setup/) — role-based and attribute-based policies
- [Storage backend setup](storage-backend-setup/) — pgvector, Qdrant, Neo4j, Elasticsearch
- [MIP developer guide](/plugins/mip-developer-guide/) — declarative routing rules for retain operations
- [astrocyte-age setup](storage-backend-setup/#apache-age) — AGE + pgvector on PostgreSQL 16
