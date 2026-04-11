# ADR-001: Deployment Models

- **Status**: Proposed
- **Date**: 2026-04-11
- **Decision Makers**: Platform team
- **Context Area**: Infrastructure -- deployment topology

## Context

Astrocyte currently ships as a Python library only (embedded in the agent process). This works well for Python-native single-agent applications but creates gaps:

1. **Language lock-in**: Non-Python agents (TypeScript, Go, Java) cannot use Astrocyte without FFI bindings or a network API.
2. **No centralized policy**: Each agent process runs its own policy layer. There is no single enforcement point for rate limiting, PII barriers, or access control across a fleet of agents.
3. **Inbound data pipeline limitations**: Background tasks (event stream consumers, API pollers) require careful lifecycle management inside a library -- they fight the embedding model's constraints (the agent controls the event loop, not Astrocyte).
4. **Multi-tenancy**: Library mode is inherently single-tenant. Multi-agent platforms need tenant isolation with shared infrastructure.

Three deployment models address these gaps: library (current), API gateway plugin, and standalone gateway.

## Decision

**Ship library as default. Add standalone gateway as the first additive deployment model. Defer gateway plugin.**

### Model 1: Library (Embedded) -- Default, GA in v1.0.0

The agent application installs `astrocyte` as a Python dependency and calls `brain.retain()` / `brain.recall()` directly. Astrocyte runs in the same process, sharing the agent's event loop.

**When to use**:
- Python-native agent applications
- Single-agent or small-scale deployments (<10 agents)
- Prototyping and development
- When sub-millisecond memory access latency matters
- When operational simplicity is the top priority

**What you get**:
- Zero network overhead (in-process calls)
- Full intelligence pipeline (chunking, entity extraction, embedding, fusion, reranking, reflection)
- Full policy layer (PII barriers, rate limiting, access control, homeostasis)
- MCP server support (stdio/SSE, for tool-based access)
- MIP routing (declarative memory routing)

**What you trade**:
- Python-only (no polyglot support)
- Single-tenant per process
- Background ingest (webhooks, streams, polls) requires careful lifecycle management -- the agent owns the event loop
- No centralized policy across multiple agent processes
- Scaling means scaling agent processes (coupled)

**Infrastructure requirements**:
- pgvector (or any Tier 1 vector store)
- LLM/embedding API access
- Python 3.11+

### Model 2: Standalone Gateway -- v0.9.0 (M6)

A dedicated Astrocyte process exposes the 4 core operations (retain/recall/reflect/forget) as HTTP REST endpoints. Optionally proxies LLM API calls with automatic memory injection (similar to LiteLLM's architecture).

**When to use**:
- Multi-agent platforms with >10 agents
- Polyglot environments (TypeScript, Go, Java agents)
- When inbound data pipelines (webhooks, event streams, API polls) are needed
- When centralized policy enforcement is required
- When multi-tenancy with tenant isolation is needed

**What you get**:
- Language-agnostic HTTP API
- Full intelligence pipeline + full policy layer
- Native support for all 5 data flow patterns (proxy, webhook, stream, poll, federated)
- Multi-tenancy with structured identity (user/agent/OBO)
- Centralized rate limiting, PII barriers, access control
- Independent scaling from agent processes
- MCP server support (SSE transport)

**What you trade**:
- ~10-30ms network latency per operation (one HTTP hop)
- Operational overhead (separate process, health monitoring, deployment pipeline)
- Configuration complexity (deployment section in astrocyte.yml)
- Additional failure mode (gateway process crash affects all agents)

**Infrastructure requirements**:
- Everything from library model
- FastAPI/Uvicorn (or equivalent ASGI server)
- Process manager (systemd, Docker, Kubernetes)
- Redis (optional, for shared state across multiple instances)
- Message queue (optional, for high-volume ingest buffering at >500 QPS)

**API surface**:
```
POST   /v1/retain          -- brain.retain()
POST   /v1/recall          -- brain.recall()
POST   /v1/reflect         -- brain.reflect()
POST   /v1/forget          -- brain.forget()
GET    /v1/health           -- health check
POST   /v1/chat/completions -- OpenAI-compatible proxy with memory injection (optional)
```

All endpoints require `Authorization` header. `X-Astrocyte-Context` header carries structured identity (user_id, agent_id, on_behalf_of, tenant_id).

### Model 3: Gateway Plugin -- Deferred to v1.1+

An Astrocyte plugin for existing API gateways (Kong, APISIX, Azure APIM) that intercepts LLM API calls, injects memory context into prompts (pre-hook), and extracts memories from responses (post-hook).

**When to use**:
- Organizations with existing API gateway infrastructure
- When agents must not change their code (transparent memory injection)
- When only proxy query + webhook patterns are needed

**What you get**:
- Zero agent code changes (transparent interception)
- Reuses existing gateway infrastructure (no new process)
- Pre-hook: recall + inject memory into system prompt
- Post-hook: retain from assistant response

**What you trade**:
- No reflect (requires LLM call within plugin -- exceeds typical plugin latency budget)
- No background ingestion (plugins are request-scoped)
- Limited pipeline depth (must complete within ~200ms plugin budget)
- Per-gateway SDK implementation effort (Kong Lua, APISIX Lua, Azure APIM policy XML)
- Constrained error handling (plugin failures must not break LLM proxy)

**Why deferred**:
1. Each gateway requires a separate SDK implementation (3+ codebases to maintain)
2. The value proposition is narrower (only 2 of 5 data flow patterns work naturally)
3. The standalone gateway covers all gateway plugin use cases with minor agent changes (point HTTP client at gateway instead of LLM API directly)
4. Plugin latency budget (~200ms) limits pipeline sophistication

## Consequences

### Positive
- Library remains the simplest on-ramp (pip install, zero infra beyond pgvector)
- Standalone gateway unlocks polyglot + multi-tenant + inbound pipelines without fragmenting the core
- Deferring gateway plugin reduces v1.0.0 scope without sacrificing capability (standalone covers the use cases)

### Negative
- Standalone gateway introduces a new operational surface (deployment, monitoring, scaling)
- Two deployment models means two sets of integration tests and documentation
- Gateway plugin users must wait for v1.1+ or use standalone gateway as interim

### Risks
- **Risk**: Standalone gateway becomes a maintenance burden, diverging from library behavior
  - **Mitigation**: Standalone gateway imports and delegates to the same `Astrocyte` class. The HTTP layer is a thin adapter. Single core, multiple transports.
- **Risk**: Performance regression from network hop in standalone mode
  - **Mitigation**: Benchmark continuously. Target: <30ms overhead (excluding LLM/embedding API calls). Connection pooling + keep-alive minimize TCP overhead.
- **Risk**: Gateway plugin delay frustrates users with existing Kong/APISIX setups
  - **Mitigation**: Document standalone gateway as the recommended path. Provide migration guide for when plugin ships.

## Capacity Estimation

### Standalone Gateway Sizing

**Small deployment** (10 agents, ~50 peak QPS):
- 1 gateway instance (4 Uvicorn workers)
- 1 pgvector instance (default PostgreSQL)
- asyncpg pool: 12 connections
- Memory: ~512 MB (Astrocyte core + embedding cache)
- No Redis, no message queue needed

**Medium deployment** (100 agents, ~500 peak QPS):
- 2-4 gateway instances behind load balancer
- 1 pgvector instance with 1 read replica
- pgbouncer: 200 connections -> 20 PostgreSQL connections
- Redis: shared rate limits + recall cache
- Memory: ~1 GB per instance
- Consider message queue for ingest buffering

**Large deployment** (1000+ agents, ~5000 peak QPS):
- 8-16 gateway instances behind load balancer
- pgvector cluster: primary + 2-4 read replicas, partitioned by tenant
- pgbouncer: mandatory
- Redis cluster: rate limits + recall cache + session state
- Kafka/Redis Streams: ingest buffering mandatory
- Memory: ~2 GB per instance (embedding model cache)

## Alternatives Considered

### gRPC Instead of REST for Standalone Gateway
- **Pro**: Lower serialization overhead (~30% smaller payloads), streaming support, code-generated clients
- **Con**: Harder to debug (not curl-friendly), browser support requires gRPC-Web proxy, smaller ecosystem for agent framework integrations
- **Decision**: REST with JSON. Memory operations are low-QPS (compared to e.g. ad serving) and the payloads are small (<10KB typical). REST's debuggability and ecosystem compatibility outweigh gRPC's performance advantages. Can add gRPC transport later if benchmarks justify it.

### WebSocket Instead of REST for Standalone Gateway
- **Pro**: Persistent connection, lower connection overhead for chatty agents
- **Con**: Stateful connections complicate load balancing (sticky sessions needed), more complex error handling, not all HTTP infrastructure (CDN, WAF) supports WebSocket
- **Decision**: REST. Memory operations are request/response, not streaming. WebSocket's connection persistence is unnecessary overhead for <500 QPS.

### Sidecar (Service Mesh) Deployment
- **Pro**: Per-pod isolation, automatic service discovery, consistent with Kubernetes-native patterns
- **Con**: Requires Kubernetes, high memory overhead per sidecar (~256 MB), overkill for <100 agents
- **Decision**: Not pursuing. Standalone gateway as a shared service is more resource-efficient. Sidecar can be reconsidered if tenant isolation requirements tighten.
