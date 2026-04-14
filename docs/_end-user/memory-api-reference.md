# Memory API reference

Complete reference for Astrocyte's four core memory operations -- retain, recall, reflect, and forget -- via Python library and REST gateway.

---

## Quick overview

| Operation | Purpose | Python method | HTTP endpoint |
|-----------|---------|---------------|---------------|
| **retain** | Store content into memory | `astrocyte.retain()` | `POST /v1/retain` |
| **recall** | Retrieve relevant memories | `astrocyte.recall()` | `POST /v1/recall` |
| **reflect** | Synthesize an answer from memory | `astrocyte.reflect()` | `POST /v1/reflect` |
| **forget** | Remove memories | `astrocyte.forget()` | `POST /v1/forget` |

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

### RecallResult

| Field | Type | Description |
|-------|------|-------------|
| `hits` | `list[MemoryHit]` | Ranked list of matching memories. |
| `total_available` | `int` | Total matches before `max_results` truncation. |
| `truncated` | `bool` | Whether results were truncated. |
| `trace` | `RecallTrace \| None` | Diagnostic trace of the recall pipeline. |
| `authority_context` | `str \| None` | Authority resolution context, if applicable. |

### MemoryHit

| Field | Type | Description |
|-------|------|-------------|
| `memory_id` | `str` | Unique memory identifier. |
| `text` | `str` | Memory content. |
| `score` | `float` | Relevance score. |
| `bank_id` | `str` | Bank the memory belongs to. |
| `metadata` | `dict` | Attached metadata. |
| `tags` | `list[str]` | Tags on the memory. |
| `occurred_at` | `datetime \| None` | When the content originally occurred. |
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

## Initialization

### From configuration file

```python
from astrocyte import Astrocyte

ast = Astrocyte.from_config("astrocyte.yaml")
```

The YAML file defines storage backends, memory banks, extraction profiles, and gateway settings. See [Configuration reference](configuration-reference/) for the full schema.

### Programmatic construction

```python
from astrocyte import Astrocyte, AstrocyteConfig

config = AstrocyteConfig(
    storage_backend="postgresql",
    connection_url="postgresql://localhost:5432/astrocyte",
)
ast = Astrocyte(config)
```

---

## Context and identity

All four operations accept an optional `context` parameter carrying actor identity and session metadata.

```python
from astrocyte import AstrocyteContext, ActorIdentity

ctx = AstrocyteContext(
    actor=ActorIdentity(actor_id="user_42", role="admin"),
    session_id="sess_abc123",
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
