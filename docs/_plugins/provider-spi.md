# Provider SPI: Retrieval, Memory Engine, LLM, Memory Export Sink, and Outbound Transport interfaces

**SPI** means **Service Provider Interface**: a documented contract that third-party packages implement and register (same idea as Java’s `ServiceLoader` SPI; in Python, `typing.Protocol` plus `pyproject.toml` entry points).

This document defines the **three memory-related** Service Provider Interfaces (**Retrieval** = VectorStore / GraphStore / DocumentStore adapters; **Memory Engine**; **LLM**), an **optional cross-cutting Memory Export Sink** SPI for warehouse / lakehouse / open table-format **durable export** (append events—not online `recall`), plus an **optional cross-cutting** Outbound Transport SPI for HTTP/TLS/proxy configuration. For architecture and the read vs export split, see `architecture-framework.md` §2, `storage-and-data-planes.md`, and `memory-export-sink.md`. For outbound transport rationale and packaging, see `outbound-transport.md`.

**Operators** (reference REST gateway, poll-based ingest, API edge): [Quick Start](/end-user/quick-start/), [Production-grade HTTP service](/end-user/production-grade-http-service/), [Poll ingest with the standalone gateway](/end-user/poll-ingest-gateway/), [Gateway edge & API gateways](/end-user/gateway-edge-and-api-gateways/). **Distribution and entry points:** [Ecosystem & packaging](/plugins/ecosystem-and-packaging/).

---

## 1. Retrieval Provider SPI (Tier 1)

### 1.1 Overview

**Scope:** Tier 1 adapters are **retrieval-oriented** (not blob/object storage). In RAG and hybrid-search literature, the same ideas are often called **retrieval backends** or **retrieval infrastructure**: **VectorStore** ≈ dense / semantic (vector DB, ANN index), **GraphStore** ≈ structured graph traversal (knowledge graph), **DocumentStore** ≈ sparse / lexical (BM25, full-text). See `architecture-framework.md` §2 (Tier 1 table).

Retrieval providers are simple adapters over those systems. They handle CRUD operations on vectors, entities, and documents **as indexed for retrieval**. Astrocyte's **built-in intelligence pipeline** (see `built-in-pipeline.md`) orchestrates these stores to provide the full memory experience: embedding, entity extraction, multi-strategy retrieval, fusion, and synthesis.

The **logical backend** may be a **serving layer** on top of a warehouse or lakehouse (SQL endpoint, query engine on Iceberg/Delta, OLAP tier, or your own HTTP façade), not only a dedicated vector DB—**if** you can implement the Tier 1 protocols with acceptable latency. That is **operational `recall`**. **Durable export** to the same warehouse or lake for BI/compliance uses the separate **Memory Export Sink** SPI (`memory-export-sink.md`); do not conflate `emit` / `flush` with `search_similar`.

Writing a Tier 1 provider is intentionally easy. The barrier to entry is **implementing a few async methods against your database**, not building a memory engine.

### 1.2 VectorStore protocol (required for Tier 1)

Every Tier 1 configuration must have at least one vector store.

```python
class VectorStore(Protocol):
    """SPI for vector database adapters."""

    async def store_vectors(
        self,
        items: list[VectorItem],
    ) -> list[str]:
        """Store vectors with metadata. Returns stored IDs."""
        ...

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        """Find similar vectors. Returns hits sorted by similarity."""
        ...

    async def delete(
        self,
        ids: list[str],
        bank_id: str,
    ) -> int:
        """Delete vectors by ID. Returns count deleted."""
        ...

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[VectorItem]:
        """List vectors in a bank with pagination.

        Returns up to ``limit`` vectors starting at ``offset``.
        Implementations should return vectors in a stable order (e.g., by ID).
        Used by consolidation (dedup scan) and ``forget(scope="all")``.
        """
        ...

    async def health(self) -> HealthStatus:
        """Check database connectivity."""
        ...

@dataclass
class VectorItem:
    id: str
    bank_id: str
    vector: list[float]
    text: str                              # Original text for this vector
    metadata: Metadata | None = None       # Metadata = dict[str, str | int | float | bool | None]
    tags: list[str] | None = None
    fact_type: str | None = None           # "world", "experience", "observation"
    occurred_at: datetime | None = None
    memory_layer: str | None = None        # "fact", "observation", "model" — memory hierarchy

@dataclass
class VectorFilters:
    bank_id: str | None = None
    tags: list[str] | None = None
    fact_types: list[str] | None = None
    time_range: tuple[datetime, datetime] | None = None
    metadata_filters: dict[str, Any] | None = None

@dataclass
class VectorHit:
    id: str
    text: str
    score: float                           # 0.0 - 1.0 similarity
    metadata: Metadata | None = None       # Metadata = dict[str, str | int | float | bool | None]
    tags: list[str] | None = None
    fact_type: str | None = None
    occurred_at: datetime | None = None
    memory_layer: str | None = None        # "fact", "observation", "model"
```

### 1.3 GraphStore protocol (optional for Tier 1)

Adds entity-link storage and graph traversal. When present, the pipeline uses it for entity extraction results and graph-based retrieval.

```python
class GraphStore(Protocol):
    """SPI for graph database adapters. Optional."""

    async def store_entities(
        self,
        entities: list[Entity],
        bank_id: str,
    ) -> list[str]:
        """Store or update entities. Returns entity IDs."""
        ...

    async def store_links(
        self,
        links: list[EntityLink],
        bank_id: str,
    ) -> list[str]:
        """Store relationships between entities. Returns link IDs."""
        ...

    async def link_memories_to_entities(
        self,
        associations: list[MemoryEntityAssociation],
        bank_id: str,
    ) -> None:
        """Associate memory IDs with entity IDs."""
        ...

    async def query_neighbors(
        self,
        entity_ids: list[str],
        bank_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphHit]:
        """Find memories connected to given entities via links."""
        ...

    async def query_entities(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
    ) -> list[Entity]:
        """Search entities by name or alias."""
        ...

    async def health(self) -> HealthStatus: ...

@dataclass
class Entity:
    id: str
    name: str                              # Canonical name
    entity_type: str                       # "PERSON", "ORG", "LOCATION", etc.
    aliases: list[str] | None = None
    metadata: dict[str, Any] | None = None

@dataclass
class EntityLink:
    source_entity_id: str
    target_entity_id: str
    link_type: str                         # "works_at", "located_in", "related_to"
    metadata: dict[str, Any] | None = None

@dataclass
class MemoryEntityAssociation:
    memory_id: str                         # VectorItem.id
    entity_id: str

@dataclass
class GraphHit:
    memory_id: str
    text: str
    connected_entities: list[str]          # Entity IDs that connected this memory
    depth: int                             # Hops from query entities
    score: float                           # Relevance based on graph distance
```

### 1.4 DocumentStore protocol (optional for Tier 1)

Adds full-text search (BM25) and source document storage. When present, the pipeline uses it for keyword-based retrieval.

```python
class DocumentStore(Protocol):
    """SPI for document storage with full-text search. Optional."""

    async def store_document(
        self,
        document: Document,
        bank_id: str,
    ) -> str:
        """Store a source document. Returns document ID."""
        ...

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        """BM25 full-text search over stored content."""
        ...

    async def get_document(
        self,
        document_id: str,
        bank_id: str,
    ) -> Document | None: ...

    async def health(self) -> HealthStatus: ...

@dataclass
class Document:
    id: str
    text: str
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None

@dataclass
class DocumentFilters:
    tags: list[str] | None = None
    metadata_filters: dict[str, Any] | None = None

@dataclass
class DocumentHit:
    document_id: str
    text: str                              # Matched passage
    score: float                           # BM25 relevance
    metadata: dict[str, Any] | None = None
```

### 1.5 Tier 1 configuration

```yaml
# astrocyte.yaml - Tier 1 setup
provider_tier: storage

# Required: one vector store
vector_store: pgvector
vector_store_config:
  connection_url: postgresql://localhost:5432/memories

# Optional: graph store for entity-link retrieval
graph_store: neo4j
graph_store_config:
  uri: bolt://localhost:7687
  user: neo4j
  password: ${NEO4J_PASSWORD}

# Optional: document store for BM25 retrieval
# If omitted and vector store supports full-text (e.g., pgvector with tsvector),
# the vector store adapter may implement DocumentStore as well.
document_store: null

# LLM provider required for Tier 1 (entity extraction, query analysis, reflect)
llm_provider: anthropic
llm_provider_config:
  api_key: ${ANTHROPIC_API_KEY}
  model: claude-sonnet-4-20250514
```

### 1.6 What the pipeline does with retrieval providers

When `provider_tier: storage`, the built-in pipeline activates and uses the retrieval SPIs:

| Pipeline stage | Uses |
|---|---|
| Chunking | Internal (no SPI needed) |
| Embedding generation | LLM Provider SPI (or local models) |
| Entity extraction | LLM Provider SPI (or spaCy NER) |
| Entity + link storage | GraphStore SPI (if configured) |
| Vector storage | VectorStore SPI |
| Document storage | DocumentStore SPI (if configured) |
| Semantic retrieval | VectorStore.search_similar() |
| Graph retrieval | GraphStore.query_neighbors() (if configured) |
| Keyword retrieval | DocumentStore.search_fulltext() (if configured) |
| Fusion (RRF) | Internal (no SPI needed) |
| Reranking | Internal (optional cross-encoder or LLM) |
| Reflect / synthesis | LLM Provider SPI |

See `built-in-pipeline.md` for the full pipeline specification.

---

## 2. Memory Engine Provider SPI (Tier 2)

### 2.1 Overview

Memory engine providers are full-stack memory systems that handle the entire pipeline internally - from content ingestion through retrieval and synthesis. When a memory engine provider is active, Astrocyte's built-in pipeline **steps aside**. The framework only applies **governance** (policy layer), not intelligence.

Memory engine providers own their own storage backends (vector DBs, graph DBs, etc.) internally. Users configure database choices through the memory engine's `provider_config`, not through Astrocyte's retrieval SPIs.

### 2.2 The protocol

```python
class EngineProvider(Protocol):
    """Memory Engine Provider SPI - implemented by full-stack memory engines (`EngineProvider` is the stable Python protocol name)."""

    def capabilities(self) -> EngineCapabilities:
        """Declare what this engine supports. Called once at init."""
        ...

    async def health(self) -> HealthStatus:
        """Liveness and readiness check."""
        ...

    async def retain(self, request: RetainRequest) -> RetainResult:
        """Store content into memory. Required."""
        ...

    async def recall(self, request: RecallRequest) -> RecallResult:
        """Retrieve relevant memories for a query. Required."""
        ...

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        """Synthesize an answer from memory. Optional."""
        ...

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        """Remove or archive memories. Optional."""
        ...
```

### 2.3 Memory engine capabilities declaration

```python
@dataclass(frozen=True)
class EngineCapabilities:
    # Core operations
    supports_reflect: bool = False
    supports_forget: bool = False

    # Retrieval strategies the engine implements internally
    supports_semantic_search: bool = True
    supports_keyword_search: bool = False
    supports_graph_search: bool = False
    supports_temporal_search: bool = False

    # Advanced features
    supports_dispositions: bool = False
    supports_consolidation: bool = False
    supports_entities: bool = False
    supports_tags: bool = False
    supports_metadata: bool = True

    # Limits
    max_retain_bytes: int | None = None
    max_recall_results: int | None = None
    max_embedding_dims: int | None = None
```

### 2.4 Request and result DTOs

All DTOs are owned by the `astrocyte` core package. Both Tier 1 (pipeline-assembled) and Tier 2 (memory-engine-provided) results use the same types. Callers never know which tier served them.

**RetainRequest** - what to store:

```python
@dataclass
class RetainRequest:
    content: str                          # The text to memorize
    bank_id: str                          # Isolation boundary
    metadata: dict[str, Any] | None       # Caller-defined key-values
    tags: list[str] | None                # Visibility / filtering tags
    occurred_at: datetime | None          # When this happened (not when stored)
    source: str | None                    # Origin system identifier
    content_type: str = "text"            # "text", "conversation", "document"
```

**RecallRequest** - what to retrieve:

```python
@dataclass
class RecallRequest:
    query: str                            # Natural language query
    bank_id: str
    max_results: int = 10
    max_tokens: int | None = None         # Token budget for results
    fact_types: list[str] | None = None   # Filter: "world", "experience", etc.
    tags: list[str] | None = None         # Filter by tags
    time_range: tuple[datetime, datetime] | None = None
    include_sources: bool = False         # Return provenance info
```

**RecallResult** - what comes back:

```python
@dataclass
class RecallResult:
    hits: list[MemoryHit]
    total_available: int                  # How many matched before truncation
    truncated: bool                       # Whether token budget cut results
    trace: RecallTrace | None = None      # Optional diagnostic info

@dataclass
class MemoryHit:
    text: str
    score: float                          # 0.0 - 1.0 relevance
    fact_type: str | None                 # "world", "experience", "observation"
    metadata: dict[str, Any] | None
    tags: list[str] | None
    occurred_at: datetime | None
    source: str | None
```

**ReflectRequest / ReflectResult** - synthesis:

```python
@dataclass
class ReflectRequest:
    query: str
    bank_id: str
    max_tokens: int | None = None
    include_sources: bool = True          # Cite which memories informed answer
    dispositions: Dispositions | None = None

@dataclass
class Dispositions:
    """Personality modifiers for synthesis. Provider may ignore if unsupported."""
    skepticism: int = 3                   # 1 (trusting) to 5 (skeptical)
    literalism: int = 3                   # 1 (flexible) to 5 (rigid)
    empathy: int = 3                      # 1 (detached) to 5 (empathetic)

@dataclass
class ReflectResult:
    answer: str
    confidence: float | None              # 0.0 - 1.0
    sources: list[MemoryHit] | None       # Memories that informed the answer
    observations: list[str] | None        # New knowledge formed during reflect
```

### 2.5 Tier 2 configuration

```yaml
# astrocyte.yaml - Tier 2 setup
provider_tier: engine

provider: mystique
provider_config:
  endpoint: https://mystique.company.com
  api_key: ${MYSTIQUE_API_KEY}
  tenant_id: my-tenant
  # Mystique manages its own pgvector, entity graph, etc. internally.
  # Database configuration happens in Mystique's own config, not here.

# LLM provider is optional for Tier 2 - only needed if:
# - PII barrier uses LLM mode
# - Signal quality scoring uses LLM
# - Fallback reflect is configured for a memory engine that lacks reflect()
llm_provider: null
```

---

## 3. Capability negotiation and fallback

### 3.1 Tier selection

The `provider_tier` field in config determines which path the core takes:

| Config | Behavior |
|---|---|
| `provider_tier: storage` | Built-in pipeline active. Retrieval SPIs used. LLM provider **required** (for entity extraction, reflect, etc.). |
| `provider_tier: engine` | Built-in pipeline inactive. Memory Engine SPI used. LLM provider optional. |

### 3.2 Fallback for missing memory engine capabilities

When a Tier 2 memory engine provider lacks a capability, the core applies a **fallback strategy**:

```yaml
# astrocyte.yaml
provider_tier: engine
provider: mem0
fallback_strategy: local_llm   # "local_llm" | "error" | "degrade"
```

| Strategy | Behavior |
|---|---|
| `local_llm` | Core performs `recall()` through the memory engine, then synthesizes via the LLM Provider SPI. Requires an LLM provider. |
| `error` | Raises `CapabilityNotSupported`. Caller handles it. |
| `degrade` | Returns raw recall results without synthesis (for reflect), or no-ops (for forget). |

### 3.3 Fallback decision matrix (Tier 2 only)

| Operation | Memory engine supports it | Memory engine lacks it + `local_llm` | Memory engine lacks it + `degrade` | Memory engine lacks it + `error` |
|---|---|---|---|---|
| `retain()` | Direct call | N/A (required) | N/A | N/A |
| `recall()` | Direct call | N/A (required) | N/A | N/A |
| `reflect()` | Direct call | `recall()` + LLM synthesis | Return recall hits as-is | `CapabilityNotSupported` |
| `forget()` | Direct call | N/A (no LLM fallback) | No-op with warning | `CapabilityNotSupported` |

Note: Tier 1 does not need this matrix - the built-in pipeline handles all operations using the retrieval SPIs directly.

### 3.4 Capability-aware feature adaptation

Beyond binary support/not-supported, capabilities inform **how** the core uses a memory engine provider:

- If `supports_temporal_search` is false but the caller passes a `time_range`, the core logs a warning and passes the request through (memory engine will ignore the filter).
- If `supports_tags` is false, the core strips tags before forwarding and logs a diagnostic.
- If `supports_dispositions` is false, dispositions in a reflect request are applied only at the fallback LLM synthesis layer, not forwarded to the memory engine.

This prevents callers from needing to know which tier or provider is active. They code against the full API; the core adapts.

---

## 4. LLM Provider SPI

### 4.1 Overview

The built-in pipeline (Tier 1) and certain policy features (both tiers) need LLM access for completions and embeddings. Rather than hardcode a specific LLM SDK, Astrocyte defines a narrow SPI that adapters implement for any LLM gateway or provider.

This is **not** an LLM gateway. Astrocyte does not normalize chat APIs, route between models, or track LLM spend. It delegates those concerns to whatever LLM system the user already has. The SPI is just the interface between Astrocyte and that system.

### 4.2 The protocol

```python
class LLMProvider(Protocol):
    """SPI for LLM access needed by the Astrocyte core."""

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        """Generate a text completion."""
        ...

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embeddings for texts. Used by built-in pipeline."""
        ...

@dataclass
class Message:
    role: str         # "system", "user", "assistant"
    content: str      # or list[ContentPart] for multimodal LLM inputs - see §4.11

@dataclass
class Completion:
    text: str
    model: str
    usage: TokenUsage | None = None

@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
```

**Design note on `embed()`:** Not all LLM providers offer embeddings (some gateways route completions only). If a provider's `embed()` raises `NotImplementedError`, the core falls back to a local embedding model (sentence-transformers). This means users can pair a cloud completion provider with local embeddings at zero API cost for the embedding step.

### 4.3 When the LLM provider is used

| Feature | Tier 1 (retrieval) | Tier 2 (memory engine) |
|---|---|---|
| Embedding generation | **Required** (or local fallback) | Not used (engine embeds internally) |
| Entity extraction | **Required** | Not used |
| Query analysis | **Required** | Not used |
| Reflect / synthesis | **Required** | Only if fallback needed |
| Reranking (LLM mode) | Required if enabled | Not used |
| PII scanning (LLM mode) | Required if enabled | Required if enabled |
| Signal quality scoring (LLM mode) | Required if enabled | Required if enabled |

**Summary:** Tier 1 always needs an LLM provider (for `complete()` at minimum). Tier 2 only needs one if specific policy features are enabled or reflect fallback is configured.

If no LLM provider is configured and a feature requires one, the core raises a clear error at initialization (fail-fast, not at request time).

### 4.4 LLM gateway and provider landscape

The Astrocyte LLM SPI is intentionally minimal (`complete()` + `embed()`) so that adapters for any LLM system are trivial to write. The landscape includes:

| Category | Examples | Adapter approach |
|---|---|---|
| **Unified gateways** | LiteLLM, OpenRouter | Single adapter wraps 100+ models. Best for users with diverse model needs. |
| **Direct provider SDKs** | OpenAI, Anthropic, Google Gemini, Groq, Mistral, Cohere | One adapter per SDK. Minimal dependencies, no gateway overhead. |
| **Cloud-managed endpoints** | AWS Bedrock, Azure OpenAI, Google Vertex AI | Adapter wraps the cloud SDK. Handles cloud auth (IAM roles, managed identity, service accounts). |
| **Self-hosted inference** | Ollama, vLLM, LM Studio, TGI, llama.cpp | Adapter calls local HTTP endpoint. No API key needed. |
| **Enterprise proxies** | Custom API gateways, internal LLM platforms | Adapter calls an OpenAI-compatible endpoint with custom auth headers. |
| **Presentation / multimodal** | Conversational video (Tavus, HeyGen, D-ID, …), voice (ElevenLabs, …) | **Not** the default LLM SPI - see §4.10 and `presentation-layer-and-multimodal-services.md`. |

### 4.5 Shipped adapters

| Package | Wraps | Models / Coverage | Dependency |
|---|---|---|---|
| `astrocyte-litellm` | LiteLLM | 100+ models across all providers (OpenAI, Anthropic, Bedrock, Vertex, Azure, Groq, Ollama, etc.) | `litellm` |
| `astrocyte-openai` | OpenAI SDK | GPT-4o, GPT-4o-mini, o1, o3, text-embedding-3-* | `openai` |
| `astrocyte-anthropic` | Anthropic SDK | Claude Opus, Sonnet, Haiku | `anthropic` |

**Recommendation:** Use `astrocyte-litellm` if you need flexibility across providers or plan to switch models. Use a direct adapter if you're committed to one provider and want minimal dependencies.

### 4.6 Cloud-managed LLM endpoints

For enterprise users on AWS Bedrock, Azure OpenAI, or Google Vertex AI, there are two paths:

**Option A: Via LiteLLM (recommended).** LiteLLM natively supports Bedrock, Azure, and Vertex with their respective auth mechanisms:

```yaml
llm_provider: litellm
llm_provider_config:
  # AWS Bedrock
  model: bedrock/anthropic.claude-sonnet-4-20250514-v1:0
  # Uses AWS credentials from environment (IAM role, env vars, etc.)

  # Azure OpenAI
  model: azure/gpt-4o
  api_base: https://my-resource.openai.azure.com
  api_key: ${AZURE_OPENAI_API_KEY}
  api_version: "2024-10-21"

  # Google Vertex AI
  model: vertex_ai/gemini-2.0-flash
  vertex_project: my-project
  vertex_location: us-central1
```

**Option B: Direct cloud SDK adapter.** For users who cannot take a LiteLLM dependency, community adapters can wrap cloud SDKs directly:

```yaml
# Hypothetical community adapter
llm_provider: bedrock
llm_provider_config:
  model_id: anthropic.claude-sonnet-4-20250514-v1:0
  region: us-east-1
  # Uses boto3 credential chain
```

### 4.7 Self-hosted and local models

For users running local inference (privacy, cost, air-gapped environments):

**Via LiteLLM:**

```yaml
llm_provider: litellm
llm_provider_config:
  model: ollama/llama3.2
  api_base: http://localhost:11434
```

**Via direct adapter:**

```yaml
# OpenAI-compatible endpoint (works with vLLM, LM Studio, Ollama, TGI, etc.)
llm_provider: openai
llm_provider_config:
  api_base: http://localhost:8000/v1
  api_key: not-needed
  model: local-model
```

Most self-hosted inference servers expose an OpenAI-compatible API, so `astrocyte-openai` with a custom `api_base` works without a dedicated adapter.

### 4.8 Separate completion and embedding providers

Some architectures use different services for completions vs embeddings (e.g., Claude for reasoning + a local model for embeddings to minimize API costs). Astrocyte supports this via split configuration:

```yaml
llm_provider: anthropic
llm_provider_config:
  api_key: ${ANTHROPIC_API_KEY}
  model: claude-sonnet-4-20250514        # Used for complete()

embedding_provider: openai               # Separate provider for embed()
embedding_provider_config:
  api_key: ${OPENAI_API_KEY}
  model: text-embedding-3-small

# Or use local embeddings with no API cost:
embedding_provider: local
embedding_provider_config:
  model: all-MiniLM-L6-v2               # sentence-transformers model
```

When `embedding_provider` is configured, the core uses it for `embed()` calls instead of the main `llm_provider`. When omitted, the main `llm_provider` handles both.

The `local` embedding provider is built into the `astrocyte` core (optional dependency on `sentence-transformers`) and requires no external API.

### 4.9 Writing a custom LLM adapter

For enterprise users with custom LLM platforms or proxy services:

```python
"""astrocyte-mycompany-llm: adapter for internal LLM platform."""

from astrocyte.provider import LLMProvider
from astrocyte.types import Message, Completion, TokenUsage

class MyCompanyLLMProvider(LLMProvider):
    def __init__(self, config: dict):
        self._endpoint = config["endpoint"]
        self._api_key = config["api_key"]

    async def complete(
        self, messages, model=None, max_tokens=1024, temperature=0.0,
    ) -> Completion:
        # Call your internal API
        response = await self._client.post(
            f"{self._endpoint}/v1/chat/completions",
            json={"messages": [{"role": m.role, "content": m.content} for m in messages],
                  "model": model or "default", "max_tokens": max_tokens},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        data = response.json()
        return Completion(
            text=data["choices"][0]["message"]["content"],
            model=data["model"],
            usage=TokenUsage(
                input_tokens=data["usage"]["prompt_tokens"],
                output_tokens=data["usage"]["completion_tokens"],
            ),
        )

    async def embed(self, texts, model=None) -> list[list[float]]:
        # If your platform doesn't support embeddings, raise NotImplementedError
        # and configure a separate embedding_provider
        raise NotImplementedError("Use local or OpenAI embeddings")
```

Register via entry point:

```toml
[project.entry-points."astrocyte.llm_providers"]
mycompany = "astrocyte_mycompany_llm:MyCompanyLLMProvider"
```

Users install exactly one LLM provider (or none if using Tier 2 with no LLM-dependent policies). The `embedding_provider` is an optional second provider specifically for embeddings.

### 4.10 Presentation and multimodal services (not the LLM SPI)

**LiteLLM**, **OpenRouter**, and direct adapters integrate **text** completion and **embedding** APIs. Products such as **conversational video avatars** (e.g. Tavus, HeyGen, D-ID), **enterprise avatar video** (e.g. Synthesia, Hour One), and **voice-first** platforms (e.g. ElevenLabs for TTS/agents) are **different**: they expose **session, video, audio, or avatar** APIs, not a substitute for `complete()` / `embed()` unless they also publish an **OpenAI-compatible chat** (or similar) endpoint you deliberately configure.

**How to combine them with Astrocyte:** keep **memory and text LLM** on the **`LLMProvider`** path; call **presentation vendors from your application** using their SDKs/REST, optionally feeding **`recall()` / `reflect()`** output into their conversations. If a vendor documents **custom LLM** or **OpenAI-compatible** URLs for *their* dialogue stack, you may reuse the **same** text endpoint for Astrocyte **when** it matches the adapter’s HTTP shape.

See **`presentation-layer-and-multimodal-services.md`** for the competitive landscape (Tavus-class vs voice), patterns, and examples.

### 4.11 Multimodal LLM inputs (first-class image / audio)

**Gateways** (LiteLLM, OpenRouter, …) can route **vision and audio** models when messages use **structured content parts**, not only strings. Astrocyte extends the DTOs accordingly:

- **`ContentPart`** - tagged union: `TextPart`, `ImageUrlPart`, `ImageBase64Part`, `AudioUrlPart`, `AudioBase64Part`, optional `DocumentUrlPart`.
- **`Message.content`** - `str` (**backward compatible**) or `list[ContentPart]`.
- **`LLMCapabilities`** - `supports_multimodal_completion`, `modalities_supported`, `supports_multimodal_embedding`.
- **Optional `embed_multimodal()`** - for providers with multimodal embedding APIs; **text-only `embed()`** remains the default path.

**Adapters** map `ContentPart` lists to **OpenAI-style** chat message JSON (or Anthropic/Google shapes) as required by the gateway.

**Pipeline:** Tier 1 uses strategies such as **caption-then-embed** (vision model → text → embed) or **multimodal embed** when implemented. Stages that need plain text (NER, BM25) consume **extracted or captioned** text.

Full specification: **`multimodal-llm-spi.md`**.

---

## 5. Memory Export Sink SPI (optional, cross-cutting)

### 5.1 Overview

Deployments that need **warehouse-scale history**, **lakehouse tables** (Iceberg, Delta, Hudi, Paimon, …), **Parquet** layouts, or curated **SQL** fact tables for BI and compliance should use a **Memory Export Sink**—not a `VectorStore` over raw object storage.

This SPI is **orthogonal** to `provider_tier` (`storage` vs `engine`). Sinks observe **successful, policy-cleared** operations (and optional explicit exports) and **emit** structured events. They do **not** implement `search_similar()`; online `recall` remains Tier 1 / Tier 2. Full design rationale and event taxonomy: **`memory-export-sink.md`**.

**Relationship to webhooks:** Until core wiring lands, the same payloads can be delivered via `event-hooks.md` HTTP webhooks to a custom ingestor; packages SHOULD converge on the DTOs described in `memory-export-sink.md` for portability.

### 5.2 The protocol (illustrative)

```python
class MemoryExportSink(Protocol):
    """Optional. Durable export — warehouse / lake / open table formats."""

    async def emit(self, event: MemoryExportSinkEvent) -> None:
        """Append one governed event (at-least-once semantics typical)."""
        ...

    async def flush(self) -> None:
        """Optional: commit buffers (micro-batch, staging files, bus flush)."""
        ...

    async def health(self) -> HealthStatus:
        ...

    def capabilities(self) -> MemoryExportSinkCapabilities:
        """Which event kinds, whether embeddings are ever included, etc."""
        ...
```

`MemoryExportSinkEvent` is a **versioned discriminated union** (retain committed, forget applied, bank lifecycle, export completed, optional sampled recall aggregates). Exact fields live in shared `astrocyte.types` alongside other DTOs.

Register via entry point:

```toml
[project.entry-points."astrocyte.memory_export_sinks"]
iceberg = "astrocyte_sink_iceberg:IcebergMemorySink"
```

YAML (illustrative):

```yaml
memory_export_sinks:
  - provider: iceberg
    config: { catalog_uri: thrift://metastore:9083, database: astrocyte, table: memory_events }
```

Packaging and naming: **`ecosystem-and-packaging.md`** (`astrocyte-sink-*` prefix).

---

## 6. Outbound Transport SPI (optional, cross-cutting)

### 6.1 Overview

Some deployments route **all outbound HTTP** through a **credential gateway**, corporate proxy, or MITM-capable TLS stack. That requirement is **orthogonal** to memory tiers and to the LLM Provider SPI: it concerns **how** TCP/TLS/HTTP is established, not **what** `retain()` / `recall()` mean or **which** `complete()` / `embed()` API is called.

Astrocyte defines an **optional** `OutboundTransportProvider` protocol. When configured, the core applies it at a **single choke point** when building HTTP clients used by LLM adapters and other outbound HTTP. **This is not an LLM gateway** and **not** a memory provider - see `architecture-framework.md` section 4.5 and the full design in `outbound-transport.md`.

**Baseline without a plugin:** If `HTTP_PROXY` / `HTTPS_PROXY` / standard trust env vars are sufficient, users need no transport package; clients should respect the process environment.

### 6.2 The protocol (illustrative)

```python
class OutboundTransportProvider(Protocol):
    """Optional. Configures shared HTTP clients (proxy, CA, mounts, headers)."""

    def apply(self, ctx: HttpClientContext) -> None:
        ...

    def subprocess_env(self) -> dict[str, str]:
        ...

    def capabilities(self) -> TransportCapabilities:
        ...
```

Register via entry point:

```toml
[project.entry-points."astrocyte.outbound_transports"]
onecli = "astrocyte_transport_onecli:OneCLIOutboundTransport"
```

YAML:

```yaml
outbound_transport:
  provider: onecli
  config: { ... }
```

---

## 7. Provider lifecycle

### 7.1 Initialization (Tier 1)

```python
brain = Astrocyte.from_config("astrocyte.yaml")
# 1. Load config, determine provider_tier = "storage"
# 2. Optionally discover and instantiate OutboundTransportProvider (if outbound_transport in config)
# 3. Discover and instantiate VectorStore (required)
# 4. Discover and instantiate GraphStore (optional)
# 5. Discover and instantiate DocumentStore (optional)
# 6. Discover and instantiate LLM provider (required for Tier 1) - HTTP clients use transport from step 2
# 7. Initialize built-in pipeline with stores + LLM provider
# 8. Initialize policy layer (optionally register MemoryExportSink instances from memory_export_sinks config)
# 9. Health check all stores - warn if unhealthy
```

### 7.2 Initialization (Tier 2)

```python
brain = Astrocyte.from_config("astrocyte.yaml")
# 1. Load config, determine provider_tier = "engine"
# 2. Optionally discover and instantiate OutboundTransportProvider (if outbound_transport in config)
# 3. Discover and instantiate EngineProvider - Memory Engine Provider SPI (via entry point or import path)
# 4. Call provider.capabilities() - cache result
# 5. Optionally discover and instantiate LLM provider (HTTP clients use transport from step 2)
# 6. Validate: if fallback_strategy=local_llm but no LLM provider → error
# 7. Initialize policy layer with capabilities + config (optional MemoryExportSink wiring)
# 8. Call provider.health() - warn if unhealthy
```

### 7.3 Request flow: retain (Tier 1)

```
caller → brain.retain(content, metadata)
  → Policy: rate limit check (homeostasis)
  → Policy: content validation (barrier)
  → Policy: PII scan (barrier)
  → Policy: metadata sanitization (barrier)
  → Policy: dedup check via VectorStore.search_similar() (signal quality)
  → Policy: start OTel span (observability)
  → Pipeline: chunk content
  → Pipeline: extract entities via LLM Provider
  → Pipeline: generate embeddings via LLM Provider
  → Pipeline: store vectors via VectorStore
  → Pipeline: store entities + links via GraphStore (if configured)
  → Pipeline: store document via DocumentStore (if configured)
  → Policy: close OTel span
  → return RetainResult
```

### 7.4 Request flow: retain (Tier 2)

```
caller → brain.retain(content, metadata)
  → Policy: rate limit check (homeostasis)
  → Policy: content validation (barrier)
  → Policy: PII scan (barrier)
  → Policy: metadata sanitization (barrier)
  → Policy: start OTel span (observability)
  → Engine: retain(request)
  → Policy: close OTel span
  → return RetainResult
```

### 7.5 Request flow: recall (Tier 1)

```
caller → brain.recall(query, budget)
  → Policy: rate limit check
  → Policy: sanitize query
  → Policy: start OTel span
  → Pipeline: analyze query via LLM Provider (extract intent, temporal refs)
  → Pipeline: generate query embedding via LLM Provider
  → Pipeline: parallel retrieval
  │   ├── VectorStore.search_similar() (semantic)
  │   ├── GraphStore.query_neighbors() (graph, if configured)
  │   └── DocumentStore.search_fulltext() (BM25, if configured)
  → Pipeline: RRF fusion
  → Pipeline: reranking (optional cross-encoder)
  → Policy: token budget enforcement (homeostasis)
  → Policy: close OTel span
  → return RecallResult
```

### 7.6 Request flow: recall (Tier 2)

```
caller → brain.recall(query, budget)
  → Policy: rate limit check
  → Policy: sanitize query
  → Policy: start OTel span
  → Engine: recall(request)   ← with circuit breaker
  → Policy: token budget enforcement (homeostasis)
  → Policy: close OTel span
  → return RecallResult
```

---

## 8. Provider implementation guides

### 8.1 Building a Tier 1 retrieval provider

1. **Create a package** named `astrocyte-{database}` (e.g., `astrocyte-pgvector`, `astrocyte-neo4j`).
2. **Implement one or more protocols**: `VectorStore` (required for vector DBs), `GraphStore` (for graph DBs), `DocumentStore` (for full-text search).
3. **Register via entry point** in `pyproject.toml`:
   ```toml
   [project.entry-points."astrocyte.vector_stores"]
   pgvector = "astrocyte_pgvector:PgVectorStore"

   [project.entry-points."astrocyte.graph_stores"]
   neo4j = "astrocyte_neo4j:Neo4jGraphStore"
   ```
4. **Handle your own connection management.** Accept connection config via `__init__`, manage pools, handle reconnection.
5. **Async PostgreSQL + pgvector (when applicable):** If you use **psycopg 3** `AsyncConnection` with the [**pgvector**](https://github.com/pgvector/pgvector) Python package, use **`register_vector_async`** in pool `configure` callbacks—not the sync **`register_vector`**, which leaves coroutines unawaited on async connections. With default transaction settings, end `configure` with **`await conn.commit()`** so the pool does not discard connections left **`INTRANS`**. Register vector adapters only after the **`vector`** extension exists (migration order or `CREATE EXTENSION` before first vector I/O). Reference: [`astrocyte-pgvector`](../adapters-storage-py/astrocyte-pgvector/README.md).
6. **Keep it simple.** You are implementing CRUD operations, not a memory engine. Let the pipeline handle the intelligence.

### 8.2 Building a Tier 2 memory engine provider

1. **Create a package** named `astrocyte-{engine}` (e.g., `astrocyte-mystique`, `astrocyte-mem0`).
2. **Implement `EngineProvider`** protocol with at least `retain()`, `recall()`, `health()`, `capabilities()`.
3. **Register via entry point** in `pyproject.toml`:
   ```toml
   [project.entry-points."astrocyte.engine_providers"]
   mystique = "astrocyte_mystique:MystiqueProvider"
   ```
4. **Use Astrocyte DTOs** for all inputs and outputs. Map to/from your engine's native types inside your provider.
5. **Declare capabilities honestly.** Over-declaring causes runtime errors; under-declaring causes unnecessary fallbacks.
6. **Own your storage.** Your engine manages its own vector DB, graph DB, etc. Users configure those through `provider_config`, not through Astrocyte's retrieval SPIs.

### 8.3 Building an outbound transport plugin

1. **Create a package** named `astrocyte-transport-{name}` (not `astrocyte-{name}` - that pattern is reserved for memory-related providers).
2. **Implement `OutboundTransportProvider`**: configure shared HTTP clients only; do not implement `complete()` / `embed()` or memory operations.
3. **Register** under `astrocyte.outbound_transports` in `pyproject.toml`.
4. **Document** env-only alternatives (`HTTP_PROXY`, trust bundles) so users can avoid a plugin when possible.

Rationale, OTel, and security constraints: `outbound-transport.md`.

### 8.4 Building a memory export sink plugin

1. **Create a package** named **`astrocyte-sink-{target}`** (for example `astrocyte-sink-iceberg`, `astrocyte-sink-kafka`) — distinct from **`astrocyte-transport-*`** and from Tier 1 **`astrocyte-{database}`** packages.
2. **Implement `MemoryExportSink`**: `emit()` at minimum; optional `flush()` for batched commits; return honest `capabilities()` (which event kinds, whether embeddings are ever serialized).
3. **Register** under `astrocyte.memory_export_sinks` in `pyproject.toml`.
4. **Do not** implement `search_similar()` here — that belongs to Tier 1 adapters when a warehouse exposes vector SQL suitable for online recall.

Event taxonomy and governance: `memory-export-sink.md`. Packaging tables: `ecosystem-and-packaging.md`.

Full implementation guide with examples: see `ecosystem-and-packaging.md`.
