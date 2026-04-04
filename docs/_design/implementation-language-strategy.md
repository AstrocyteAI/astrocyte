# Implementation language strategy

This document defines how Astrocytes is delivered as **two parallel implementations** in one repository - **Python** (`astrocyte-py/`) and **Rust** (`astrocyte-rs/`) - that are intended to be **drop-in replacements** for each other at the framework contract level (configuration, semantics, provider SPIs, and portable data shapes). Design documents under `docs/` apply to **both** unless a section says otherwise.

---

## Parallel implementations (drop-in)

| Codebase | Location | Role |
|---|---|---|
| **Python Astrocytes** | **`astrocyte-py/`** (Python package; PyPI name **`astrocyte`**) | PyPI distribution; LangChain/LangGraph, MCP, and the Python provider ecosystem. |
| **Rust Astrocytes** | **`astrocyte-rs/`** | Native Rust library for services and embedders without a Python runtime. |

**Drop-in replacement** means: the same **logical** framework - users choose **one** implementation for a deployment. Shared YAML (or equivalent) profiles, the same policy and pipeline semantics, and the same provider entry-point model should allow swapping **`astrocyte`** for **`astrocyte-rs`** (or vice versa) without redesigning integrations, subject only to language-specific packaging and binding details documented per implementation.

Conformance tests and versioned SPI contracts are the enforcement mechanism for parity, not a shared native extension inside the Python wheel.

---

## 1. Two-phase rollout (per implementation)

### Phase 1: Baseline

The **Python** package targets **100% Python** (3.11+) first. This optimizes for ecosystem reach, contribution friction, and native `async/await` for I/O.

The **Rust** crate matures in parallel under `astrocyte-rs/`, implementing the same contract with idiomatic Rust (`async`/tokio where appropriate).

### Phase 2: Performance hardening

Each implementation optimizes **within its own language**: faster data paths, tighter allocation profiles, and native libraries where appropriate - **without** changing the portable contract (DTOs, config schema, SPI versions). This is not “embed Rust inside Python”; it is **parity and speed** in each tree independently.

---

## 2. Hot paths in the Rust implementation (`astrocyte-rs`)

These components are strong candidates for native Rust in **`astrocyte-rs`** (CPU-bound, high frequency, or concurrency-sensitive):

| Component | Why Rust | Latency impact |
|---|---|---|
| PII regex scanning | High-throughput regex on every retain | Lower per-scan overhead |
| Token counting | Recall/reflect budget enforcement | Predictable CPU cost |
| RRF fusion | Numerical work over result sets | Scales with result count |
| Rate limiter / token bucket | High-frequency atomic operations | Under load |
| Circuit breaker state machine | Lock-free or carefully ordered access | Correctness under concurrency |
| Cosine similarity (dedup) | Vector math for signal-quality checks | At scale |
| Embedding vector operations | Normalization, distances | Batch paths |

### What stays in high-level / orchestration layers

| Component | Why it stays in orchestration (both langs) |
|---|---|
| SPI protocols (VectorStore, EngineProvider, LLMProvider) | Community providers remain implementable in the host language |
| Pipeline orchestrator | Coordinates async I/O - not CPU-bound |
| Configuration loading / profile resolution | Parsing and resolution, not hot-path math |
| OTel span creation | Binds to the language’s telemetry SDK |
| DTOs | Portable shapes (Python `dataclass` / Rust structs) |
| Fallback reflect (LLM synthesis) | Dominated by LLM API latency |

---

## 3. Design constraints for cross-implementation portability

These constraints apply **now** so Python and Rust stay aligned. All interface design must follow these rules.

### 3.1 DTOs must use simple, serializable types

All request/result types must be representable as JSON-serializable structures shared by both implementations. No Python- or Rust-only constructs in **portable** DTOs.

**Allowed types in portable DTOs:**
- `str`, `int`, `float`, `bool`, `None`
- `list[T]`, `dict[str, T]` where T is an allowed type
- `datetime` (ISO 8601 string at serialization boundaries)
- `tuple[T, ...]` (array at serialization boundaries)
- `dataclass` / struct (dict/struct at serialization boundaries)

**Not allowed in portable DTOs:**
- Callable / function references
- Generator / async generator
- Open-ended dynamic objects as contract fields (use explicit unions or maps with primitive values)

```python
# GOOD - portable DTO
@dataclass
class RecallRequest:
    query: str
    bank_id: str
    max_results: int = 10
    max_tokens: int | None = None
    tags: list[str] | None = None
    time_range: tuple[datetime, datetime] | None = None

# BAD - not portable
@dataclass
class RecallRequest:
    query: str
    filter_fn: Callable[[MemoryHit], bool]  # Not serializable
    context: Any                             # Untyped, not portable
```

### 3.2 Policy functions must be pure computations

Policy layer functions (rate limiting, token counting, PII scanning, fusion) must be **pure functions** or **stateful but self-contained objects** so each implementation can optimize independently without changing call sites.

```python
# GOOD - same contract in Python or Rust
class RateLimiter:
    def __init__(self, max_per_minute: int): ...
    def check(self, bank_id: str) -> bool: ...
    def record(self, bank_id: str) -> None: ...

# BAD - deeply entangled with one runtime
class RateLimiter:
    async def check_with_callback(self, bank_id: str, on_limit: Callable): ...
```

### 3.3 Async boundary at the orchestrator

**Python:** CPU-bound helpers are **synchronous**; the async orchestrator awaits I/O and calls sync hot paths from async context (no heavy work inside micro-tasks that should be sync).

**Rust:** I/O stays in `async` boundaries (e.g. tokio); pure numerical work remains synchronous inside the same layering rule.

```python
# Pipeline orchestrator (Python, async I/O + sync hot paths)
async def recall(self, request: RecallRequest) -> RecallResult:
    raw_results = await self._vector_store.search_similar(query_vec, ...)
    fused = rrf_fusion(raw_results)
    truncated = enforce_token_budget(fused)
    return RecallResult(hits=truncated, ...)
```

### 3.4 Configuration must be data, not code

All configuration must be representable as YAML/JSON. No lambdas or language-specific code objects in portable config.

The `provider` / `vector_store` fields may still name entry points resolved by each implementation’s loader.

### 3.5 State must be explicit and contained

Mutable state (rate limiters, circuit breakers, dedup caches) lives in **explicit objects**, not hidden globals, so both implementations can match behavior and test harnesses can reset state deterministically.

---

## 4. Module boundary maps

Conceptual layout for each implementation directory. Names may evolve; the split between **orchestration**, **portable policy math**, and **providers** should stay stable.

### Python (`astrocyte-py/`)

The import package remains **`astrocyte`**; it lives under `astrocyte-py/` in the repository.

```
astrocyte-py/
└── astrocyte/               # Python package (PyPI distribution name `astrocyte`)
    ├── __init__.py           # Public API
    ├── provider.py           # SPI protocols
    ├── types.py              # Portable DTOs
    ├── capabilities.py
    ├── config.py             # YAML loading, profiles
    ├── pipeline/             # Orchestration + policy hooks
    ├── policy/               # Policy layer (Python implementations)
    └── testing/              # Conformance suites
```

### Rust (`astrocyte-rs/`)

```
astrocyte-rs/
├── src/
│   ├── lib.rs
│   ├── provider/            # SPI traits
│   ├── types/               # Portable structs
│   ├── config/
│   ├── pipeline/
│   └── policy/              # Native hot paths + shared semantics
└── tests/                   # Conformance / parity tests vs Python where applicable
```

---

## 5. Packaging

### Python (`astrocyte-py/`)

`pyproject.toml` lives at **`astrocyte-py/pyproject.toml`**; the distribution name on PyPI remains **`astrocyte`**.

```toml
# pyproject.toml (at astrocyte-py/)
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "astrocyte"
requires-python = ">=3.11"
```

### Rust (`astrocyte-rs/`)

Standard **Cargo** crate layout; publish to **crates.io** when ready. Versioning tracks the same semantic SPI and policy contract as the Python package.

---

## 6. Interface design checklist

When designing any new interface in Astrocytes, verify:

- [ ] **Portable DTOs** use only serializable primitive shapes (str, int, float, bool, None, list, dict, datetime, explicit structs)
- [ ] **No `Any`** in portable DTOs - use explicit unions
- [ ] **No callables in portable DTOs** - callbacks belong in orchestration or language-specific extension points
- [ ] **CPU-bound functions are sync** - no `async` on pure computation
- [ ] **I/O functions are async** - clean separation from CPU-bound code
- [ ] **State is contained in objects** - no module globals for policy hot state
- [ ] **Config is data** - YAML/JSON representable
- [ ] **Hot-path functions avoid language-private runtime hooks** so both implementations can match behavior
