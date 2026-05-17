---
title: "Document Engine — Phase 2 Roadmap"
draft: false
topic: design
---

# Document Engine — Phase 2 Roadmap

**Status:** design (2026-05-17)  
**Predecessors:** M17 shipped — Document Engine Phase 1+2 complete (tree types, markdown builder, adaptive summarizer, DocumentStore SPI + Postgres impl, DocumentIngestor). M17 locked policy §6 explicitly deferred tree-search retrieval to "a separate cycle if Phase 4 verdict invites it." Phase 4 shipped: +10.56pp LME (3.2σ). Verdict invites it.  
**Parallel track:** Conversation Engine + Memory Engine benchmark work (M18 quick-wins).

---

## 1. What M17 shipped and what it deferred

### Shipped (M17, cycle closed 2026-05-17)

| Component | Location |
|---|---|
| `Document`, `DocumentTree`, `TreeNode`, `NodeSummary` | `astrocyte/documents/types.py` |
| `build_markdown_tree()` — code-fence-aware two-pass builder | `documents/builders/md_builder.py` |
| `AdaptiveSummarizer` — PageIndex-parity 200-token gate | `documents/builders/summarizer.py` |
| `MarkdownParser` + `ParserRegistry` | `documents/parsers/` |
| `DocumentStore` SPI + `InMemoryDocumentStore` | `documents/storage.py` |
| `PostgresDocumentStore` + migrations 025-026 | `adapters-storage-py/astrocyte-postgres/` |
| `DocumentIngestor` — tree → memory.retain() bridge | `documents/ingestor.py` |

### Deferred from M17

| Item | M17 reference | Notes |
|---|---|---|
| PDF parsers (MarkitdownParser, LlamaParseParser) | Phase 6 | Deferred pending real PDF use case |
| Tree-search / reasoning-based retrieval | §6 out-of-scope | "Separate cycle if Phase 4 verdict invites it" — verdict: invited |
| Document Engine bench (FinanceBench) | §8.6 | "Will bench against FinanceBench (Mafin2.5) in a future M-cycle" |
| External `pageindex` dep removal | Phase 7 | Waiting on production readiness |
| MCP tools exposing trees | §6 | Blocked on MCP server work |

---

## 2. Three paths — the explicit design contract

These three paths are the architectural foundation. They must not be conflated.

---

### Path 1: DE → ME (document-only)

A file is parsed into a tree, stored structurally, and ingested into the Memory Engine node by node.

**Ingest:**

```
Document (PDF / markdown / DOCX)
  │
  ├─ Parser.convert()                # bytes → markdown text
  ├─ build_markdown_tree()           # markdown → DocumentTree
  ├─ AdaptiveSummarizer              # populate node.summary per node
  ├─ DocumentStore.save_document()   # persist tree to astrocyte_documents +
  │                                  # astrocyte_document_nodes (Postgres)
  └─ DocumentIngestor.ingest()       # walk tree pre-order →
                                     # memory.retain() per node
                                     #   content  = node.text or node.summary.text
                                     #   metadata = {
                                     #     source:                "astrocyte.documents",
                                     #     source_document_id:    str,
                                     #     tree_node_id:          str,
                                     #     tree_node_parent_id:   str | None,
                                     #     tree_node_depth:       int,
                                     #     tree_node_title:       str,
                                     #   }
```

**Two storage targets:** `DocumentStore` holds the tree structure (for tree-search retrieval). The Memory Engine's `VectorStore` holds the embedded node texts (for vector recall). They are written independently in the same ingest call.

**Recall — two options, complementary:**

| Option | Call | Returns | When to use |
|---|---|---|---|
| Vector | `memory.recall(query, bank_id)` | Ranked node texts (metadata carries `tree_node_id`, `tree_node_title`) | "Find anything in this bank about X" — broad, approximate |
| Tree-search | `navigator.search(query, [doc_id])` | `SectionHit[]` with breadcrumb, full text, relevance reasoning | "Find the specific section about X in this document" — precise, structure-aware |

Tree-search reads from `DocumentStore`. Vector reads from `VectorStore`. Neither path depends on the other.

---

### Path 2: CE → ME (conversation-only)

A conversation is chunked at turn boundaries and ingested into the Memory Engine chunk by chunk.

**Ingest:**

```
Conversation (chat session, Slack thread, agent dialog)
  │
  └─ ConversationIngestor.ingest()   # turn-aware chunking (Hindsight-style):
                                     #   rule: never split mid-turn
                                     #   rule: oversized turn stands alone
                                     # memory.retain() per chunk
                                     #   content  = chunk text
                                     #   metadata = {
                                     #     source:          "astrocyte.conversations",
                                     #     conversation_id: str,
                                     #     speaker_id:      str,
                                     #     turn_role:       "user"|"assistant"|"system",
                                     #     timestamp:       str,
                                     #   }
```

**One storage target:** the Memory Engine's `VectorStore` only. Conversations are not stored in `DocumentStore` — there is no conversation tree, no `ConversationStore` tree-search path.

**Recall — vector only:**

```
memory.recall(query, bank_id)
  → vector search over retained turn chunks
  → ranked hits (metadata carries conversation_id, speaker_id, turn_role)
```

There is no tree-search path for conversations. CE → ME is vector-only.

---

### Path 3: DE + CE → ME (composition)

A document is uploaded as part of a conversation — "chat with a document." Both engines write to the **same `bank_id`**. Bank-level co-location is the composition mechanism: no shared tables, no foreign keys, no engine cross-imports.

**Ingest (concurrent, independent):**

```
session_bank = "synapse-thread-<id>"   # shared by both engines

Document (uploaded PDF):
  └─ [Path 1 ingest] → DocumentStore + memory.retain(bank_id=session_bank) per node

Conversation (chat turns):
  └─ [Path 2 ingest] → memory.retain(bank_id=session_bank) per chunk
```

Neither engine knows about the other. Neither ingest path blocks the other.

**Recall — four scopes:**

| Scope | Call | Returns |
|---|---|---|
| `@all` (default) | `memory.recall(query, bank_id)` | Ranked mix of document nodes AND conversation chunks; Memory Engine is source-agnostic |
| `@document` | `navigator.search(query, [doc_id])` | Tree-navigated sections from that document only |
| `@conversation` | `memory.recall(query, bank_id, filters={"source": "astrocyte.conversations"})` | Conversation chunks only |
| `@documents` | `navigator.search(query, [doc_A, doc_B, …])` | Tree-navigated across specified documents |

**The `doc_ids` tracking requirement:**

`navigator.search()` requires explicit `doc_ids`. In the composition scenario, the **calling application** (Synapse, Cerebro) tracks which document IDs are attached to which session. Astrocyte does not maintain a session → documents registry — that is the caller's responsibility.

This is intentional. Astrocyte provides primitives; the presentation layer owns session state.

**Cross-source retrieval (the value of composition):**

`memory.recall(query, bank_id)` — no source filter — ranks document nodes and conversation chunks together by vector similarity. The Memory Engine sees both as retained content, differentiated only by their `source` metadata field. When a graph store is configured, entity extraction builds entity links across both sources — enabling queries like "did we discuss this company in the conversation AND in the uploaded contract?"

**Scope annotation (design decision, not yet implemented):**

The `filters` parameter on `memory.recall()` is not yet part of the public API. The `@conversation` and `@document` vector-recall scopes require a metadata filter on `source`. This needs to be added to the recall SPI before the composition path is fully usable from the application layer. Tracked as an open question in §9.

---

## 3. Scope

Three workstreams, in dependency order:

```
[W1] PDF support       → MarkitdownParser + LlamaParseParser + page-range tracking in TreeNode
[W2] Tree-search       → DocumentRetriever + DocumentNavigator + TreeSkeleton types
[W3] Document bench    → FinanceBench baseline (vector recall) → tree-search run → verdict
```

W1 and W2 are independent. W3 requires both.

---

## 4. W1 — PDF support (M17 Phase 6)

### 4.1 New parsers

The `Parser` ABC and `ParserRegistry` are already designed for this. Two new implementations:

```python
# astrocyte/documents/parsers/markitdown.py
class MarkitdownParser(Parser):
    """PDF/DOCX/HTML/PPTX → Markdown via markitdown (Microsoft, Apache 2.0).

    Local, no API key, no network. Covers text-heavy documents well.
    Loses page-boundary structure (all content becomes markdown; page
    numbers aren't preserved). For page-accurate retrieval, the future
    PdfTreeBuilder is needed (out of scope for this cycle).
    """
    def name(self) -> str: return "markitdown"
    def supports(self, filename, content_type=None) -> bool: ...

# astrocyte/documents/parsers/llama_parse.py
class LlamaParseParser(Parser):
    """PDF → Markdown via LlamaParse cloud API (proprietary, paid, opt-in).

    Requires LLAMA_CLOUD_API_KEY. Higher quality for complex layouts:
    multi-column, scanned text (OCR), tables, charts.
    Activated via: pip install astrocyte[llamaparse]
    """
    def name(self) -> str: return "llamaparse"
    def supports(self, filename, content_type=None) -> bool: ...
```

**Registration order in `default_registry()` (updated):**
```python
def default_registry() -> ParserRegistry:
    reg = ParserRegistry()
    if os.environ.get("LLAMA_CLOUD_API_KEY"):
        reg.register(LlamaParseParser())    # opt-in, highest quality for PDFs
    reg.register(MarkitdownParser())        # local fallback for PDF/DOCX/HTML
    reg.register(MarkdownParser())          # catch-all for .md / .txt
    return reg
```

### 4.2 Page-range tracking in TreeNode

PDF retrieval in PageIndex uses `get_page_content(pages="5-7")` — page coordinates, not line numbers. For parity, `TreeNode` needs page fields alongside the existing line fields.

**Change:** additive two new optional fields on `TreeNode` (no existing field renamed):

```python
@dataclass
class TreeNode:
    ...
    line_start: int | None = None   # for markdown (1-indexed)
    line_end: int | None = None
    page_start: int | None = None   # for PDF (1-indexed); None for markdown nodes
    page_end: int | None = None
```

**Migration 029:** `ALTER TABLE astrocyte_document_nodes ADD COLUMN page_start int, ADD COLUMN page_end int;`

Existing markdown nodes get NULL for page fields — no backfill needed.

### 4.3 PdfTreeBuilder (future, not this cycle)

The markitdown path (PDF → markdown → `build_markdown_tree()`) is the right starting point — it reuses all existing infrastructure. It loses page-boundary information; section boundaries come from heading structure only.

A native `PdfTreeBuilder` (preserving page coordinates, TOC detection, recursive splitting) is a future investment. Build it after validating the markitdown path is the quality bottleneck.

---

## 5. W2 — Tree-search retrieval

### 5.1 Problem statement

Current retrieval:

```
memory.recall(query, bank_id) → vector search → flat list of node texts
```

This loses document structure context entirely. For document-anchored questions — "find the termination clause," "what does Section 4.2 say about pricing?" — an LLM navigating the tree structure is more precise than vector similarity.

PageIndex's claim: 98.7% on FinanceBench via tree-search vs. industry-average ~50-80% via vector RAG. The mechanism: structure-aware reasoning beats semantic approximation for document-specific questions.

### 5.2 New module: `astrocyte/documents/retrieval/`

```
astrocyte/documents/retrieval/
    __init__.py
    types.py         — TreeSkeleton, SkeletonNode, NodeContent,
                       DocumentSearchResult, SectionHit
    retriever.py     — DocumentRetriever (low-level, no LLM)
    navigator.py     — DocumentNavigator (agentic loop)
    tools.py         — make_retrieval_tools() for agent framework integration
```

**Isolation:** retrieval module knows nothing about the Memory Engine. Reads from `DocumentStore` only. No vector search, no embeddings at query time, no `bank_id`.

### 5.3 Types

```python
@dataclass
class SkeletonNode:
    """A node stripped of text — for efficient LLM tree reasoning.

    Token budget: ~30-50 tokens per node (title + summary fragment).
    A 100-node document → ~3-5k tokens to transmit the full skeleton.
    """
    node_id: str
    parent_id: str | None
    depth: int
    title: str
    summary: str | None        # node.summary.text if available
    has_children: bool
    child_count: int
    page_start: int | None     # for PDFs; None for markdown
    line_start: int | None     # for markdown; None for PDFs


@dataclass
class TreeSkeleton:
    """Tree structure without node text. Pre-order flat list.

    The reasoning surface the LLM uses to decide which nodes to fetch.
    Never contains node.text — prevents accidental large-text dumps to LLM.
    """
    document_id: str
    title: str
    node_count: int
    nodes: list[SkeletonNode]


@dataclass
class NodeContent:
    """A single node's full content + its immediate children (skeleton form)."""
    node_id: str
    document_id: str
    title: str
    depth: int
    parent_id: str | None
    text: str
    summary: str | None
    summary_kind: str | None   # "raw" | "llm" | "prefix"
    children: list[SkeletonNode]
    page_start: int | None
    page_end: int | None
    line_start: int | None
    line_end: int | None


@dataclass
class SectionHit:
    """One relevant section found by the DocumentNavigator."""
    document_id: str
    node_id: str
    node_title: str
    node_depth: int
    breadcrumb: list[str]        # titles from root to parent, e.g. ["Chapter 3", "3.2 Methods"]
    text: str
    relevance_reasoning: str     # LLM's explanation


@dataclass
class DocumentSearchResult:
    query: str
    sections: list[SectionHit]
    documents_searched: int
    iterations_used: int
    strategy: Literal["tree_search"] = "tree_search"
```

### 5.4 DocumentRetriever

Low-level read access. No LLM. Wraps `DocumentStore` directly. Fully testable without any external service.

```python
class DocumentRetriever:
    """Read-only document access — three PageIndex-parity tools.

    The low-level layer. DocumentNavigator wraps this to add the agent
    loop. Consumers can also use these directly in their own agent.
    """

    def __init__(self, store: DocumentStore) -> None: ...

    async def get_document_info(self, doc_id: str) -> DocumentInfo:
        """Returns metadata: title, source_uri, node_count, depth_range."""

    async def get_document_structure(self, doc_id: str) -> TreeSkeleton:
        """Returns full tree without text — the reasoning surface.
        Raises DocumentNotFoundError if doc_id doesn't exist.
        """

    async def get_node_content(self, doc_id: str, node_id: str) -> NodeContent:
        """Returns a single node's full text + immediate children (skeleton)."""

    async def get_nodes_at_depth(self, doc_id: str, depth: int) -> list[SkeletonNode]:
        """All nodes at a specific depth level (e.g., depth=2 = all H2 sections)."""
```

### 5.5 DocumentNavigator

Adds the LLM agent loop. Mirrors PageIndex's `PageIndexClient` agentic pattern.

```python
class DocumentNavigator:
    """Agentic tree-search — PageIndex's core retrieval innovation.

    Agent loop per document:
    1. get_document_structure() → skeleton (titles + summaries, no text)
    2. LLM: "Which node_ids are most relevant to {query}?"
    3. get_node_content() for each identified node
    4. If LLM wants to explore deeper: get children, repeat (bounded by max_iterations)
    5. Compile into DocumentSearchResult

    Cost: 1-2 LLM calls per document. Cheaper than embedding + vector search
    for known-document retrieval because no embedding is generated at query time.
    """

    def __init__(
        self,
        retriever: DocumentRetriever,
        llm_call: LlmCall,           # async def fn(prompt: str) -> str
        *,
        max_iterations: int = 5,
        max_sections: int = 3,
    ) -> None: ...

    async def search(
        self,
        query: str,
        document_ids: list[str],
    ) -> DocumentSearchResult: ...
```

**`llm_call` pattern:** same injectable coroutine pattern as `AdaptiveSummarizer`. Tests pass a fake; production passes a real LLM wrapper. No hard dependency on any specific LLM provider.

### 5.6 Agent tool surface

```python
# astrocyte/documents/retrieval/tools.py

def make_retrieval_tools(retriever: DocumentRetriever) -> list[Callable]:
    """Returns the three PageIndex-parity tools as plain async callables.

    Suitable for wiring into OpenAI Agents SDK tool lists, Anthropic
    tool_use definitions, or any other agent framework.

    Tools returned:
      - get_document_info(doc_id: str) → DocumentInfo
      - get_document_structure(doc_id: str) → TreeSkeleton
      - get_node_content(doc_id: str, node_id: str) → NodeContent
    """
```

Framework-agnostic first — no OpenAI-specific JSON schema baked in. Consumers wrap in their framework's tool descriptor.

### 5.7 Relationship to Memory Engine recall

Two retrieval paths, independent and complementary:

| Path | Use case | Reads from | `bank_id` required |
|---|---|---|---|
| `memory.recall(query, bank_id)` | Cross-document concept retrieval | VectorStore | Yes |
| `navigator.search(query, doc_ids)` | Known-document section finding | DocumentStore | No |

The navigator **does not go through the Memory Engine**. No vector search, no embedding at query time. It reads tree structure directly from `DocumentStore`.

**When to use each:**
- `memory.recall`: "What do any of my documents say about X?" (broad sweep)
- `navigator.search`: "Given this specific contract, find the indemnification clause." (precise, structure-aware)

---

## 6. W3 — Document Engine bench

### 6.1 Target: FinanceBench

PageIndex reported 98.7% on FinanceBench (10-K filings, 150 financial QA questions). M17 §8.6 identified FinanceBench as the right future bench. This is the natural comparison.

Astrocyte's variable: replace PageIndex's retrieval with DocumentNavigator (same tree structure; different retrieval mechanism; Astrocyte's infrastructure instead of PageIndex's external dep).

### 6.2 Run design

| Run | Ingest | Retrieval | Hypothesis |
|---|---|---|---|
| Baseline | MarkitdownParser → `build_markdown_tree` → `DocumentIngestor` → vector store | `memory.recall()` | Establishes Astrocyte baseline; expected below PageIndex's 98.7% |
| Tree-search | Same ingest + save tree to `DocumentStore` | `navigator.search()` | Hypothesis: significantly above vector baseline |
| Hybrid | Same ingest | `recall` + `navigator`, fused | Hypothesis: matches or beats tree-search alone |

### 6.3 Ship gate

| Gate | Value | Rationale |
|---|---|---|
| Tree-search ≥ vector baseline + 10pp | +10pp | Minimum meaningful lift to justify W2 investment |
| Tree-search ≥ 90% FinanceBench | 90% | Conservative floor; PageIndex is 98.7% — we should be competitive |

**Null verdict policy:** if bench fails but W1+W2 product gates clear, ship anyway. PDF parsing + tree-search retrieval API have independent product value (cleaner ingest API, removed external dep, structured retrieval surface for long docs).

---

## 7. Non-goals

- **MCP tools for trees** — depends on MCP server work (tracked separately)
- **Multi-document trees** ("PageIndex File System") — single-document only
- **OCR / vision PDF** — markitdown and LlamaParse handle text PDFs sufficiently
- **PdfTreeBuilder (native page-accurate)** — markitdown path first; native builder after validating markitdown is the bottleneck
- **Backfill existing rows** — forward-only; existing nodes have `page_start/page_end = NULL`
- **Replacing vector recall** — tree-search is additive, not a replacement
- **Streaming document ingest** — batch/async only

---

## 8. Phase plan

| Phase | Description | Dep | Approx cost |
|---|---|---|---|
| **A** | `MarkitdownParser` + `LlamaParseParser` + registry update + unit tests | — | $0 |
| **B** | Migration 029 (`page_start/page_end` on `astrocyte_document_nodes`) | — | $0 |
| **C** | `TreeSkeleton`, `SkeletonNode`, `NodeContent`, `SectionHit`, `DocumentSearchResult` types + `DocumentRetriever` + unit tests | B | $0 |
| **D** | `DocumentNavigator` + agent prompts + integration tests (fake LLM) | C | $0 |
| **E** | `make_retrieval_tools()` + tool surface tests | C | $0 |
| **F** | FinanceBench baseline run (vector recall) | A | ~$5 |
| **G** | FinanceBench tree-search run (`navigator.search`) | D, F | ~$15 |
| **H** | Ship gate evaluation; product-side docs + cookbook example | G | ~$5 |

A and C can run concurrently (no shared dependency). F requires A (PDF parsing). G requires D + F.

---

## 9. Open questions

| # | Question | Leaning | Reason |
|---|---|---|---|
| Q1 | `TreeSkeleton` as new type vs. `DocumentTree` with `text=None`? | **New type** | Type-safe — prevents accidentally passing full-text tree to LLM |
| Q2 | `navigator.search()` takes single `doc_id` or `list[str]`? | **`list[str]`** | PageIndex supports multi-doc queries; list is more general, no cost when single |
| Q3 | `llm_call: LlmCall` injectable (same as `AdaptiveSummarizer`)? | **Yes** | Testability, no provider lock-in |
| Q4 | Page fields as two new `TreeNode` fields vs. a `SourceLocation` union? | **Two fields** | Additive, backward-compatible, no type system complexity |
| Q5 | `make_retrieval_tools()` framework-agnostic or OpenAI-schema? | **Framework-agnostic first** | Avoids baking in one framework; consumers add their schema wrapper |
| Q6 | FinanceBench: Mafin2.5 harness or custom? | **Mafin2.5 if adoptable** | Purpose-built; evaluate setup cost first |
| Q7 | LLM model for `DocumentNavigator`? | **gpt-4o-mini default** | Matches `AdaptiveSummarizer`; configurable via `llm_call` |
| Q8 | Should `DocumentNavigator` cache `TreeSkeleton` responses? | **Short TTL (5 min)** | Matches LLM prompt cache TTL; avoids redundant `get_document_structure` calls per session |
| Q9 | Add `filters` parameter to `memory.recall()` for source-scoped recall (`@conversation`, `@document`)? | **Yes, needed for Path 3** | Without it, the `@conversation` and `@document` vector-recall scopes in the composition path require the caller to post-filter; a proper `filters` parameter belongs in the recall SPI |

---

## 10. Dependency map

```
W1 — PDF support
  Phase A: MarkitdownParser + LlamaParseParser ─────────────────────────────┐
  Phase B: migration 029 (page fields on nodes)                             │
                ↓                                                           │
W2 — Tree-search                                                      W3 — Bench
  Phase C: DocumentRetriever + types ──────────────────────────────► Phase F: FinanceBench baseline
  Phase D: DocumentNavigator ──────────────────────────────────────► Phase G: FinanceBench tree-search
  Phase E: make_retrieval_tools()                                            │
                                                                      Phase H: verdict + docs
```

---

## 11. Future cycles (out of scope here)

- **PdfTreeBuilder (native):** page-accurate TOC detection + 3-mode fallback (Mode A/B/C matching PageIndex's pipeline). Build after FinanceBench reveals whether markitdown's heading-derived tree is the quality bottleneck.
- **MCP tools exposing trees:** `get_document_structure` + `get_node_content` as MCP tools. Blocked on MCP server work.
- **Hybrid recall surface:** `recall_documents(query, strategy="auto")` that routes to vector or tree based on whether `doc_ids` are specified.
- **DoubleBench:** check if document-heavy portion of DoubleBench is a useful secondary signal after FinanceBench.
- **External `pageindex` dep removal:** Phase 7 from M17, pending confirmed production readiness.
