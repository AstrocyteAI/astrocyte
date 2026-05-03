# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The Python package `astrocyte` is built from `astrocyte-py/`; its version comes from Git tags via Hatch VCS.

## [Unreleased]

## [0.10.0] — 2026-05-02 (retrieval quality, gateway surface area, lifecycle, DSAR)

A substantial release covering retrieval-quality improvements (HyDE, observation consolidation, multi-query gating, adversarial defense), expanded gateway surface (graph search/neighbors, compile, DSAR erasure), bank lifecycle features (history, audit, export/import, legal hold), ingestion adapters (S3/Garage, document folders), and a 7× speedup on the retain phase of LongMemEval.

### Added

- **R1 — Hypothetical Document Embedding (HyDE)** (`pipeline/retrieval.py`): query is rewritten by an LLM into a hypothetical answer document, embedded, and used as the retrieval query alongside the literal one. Lifts recall on questions where the user's surface form differs lexically from the stored memory.
- **Observation consolidation layer** (`pipeline/observation.py`): post-retain LLM synthesis layer that compresses raw memories into structured observations stored in a dedicated `::obs` bank. Routed via MIP; surfaced through intent-gated injection during recall on open-domain queries.
- **Multi-query confidence gate** (`pipeline/recall.py`): retrieval at threshold 0.72 — when the top-K confidence is below the gate, recall expands the query and retries before returning. Reduces low-confidence hallucination on ambiguous prompts.
- **Adversarial defense** (`pipeline/reflect.py`): adversarial-abstention prompt template + temporal-aware prompt auto-selection in `brain.reflect()`. Detects and refuses prompt-injection-style queries.
- **Structured fact extraction** (`pipeline/structured_facts.py`): typed fact extraction with provenance pointers; surfaces under `MemoryHit.facts`.
- **Query temporal analyzer** (`pipeline/query_analyzer.py`): classifies queries on temporal axis (point-in-time, range, change-detection) and steers retrieval policy.
- **Agentic reflect** (`pipeline/agentic_reflect.py`): tool-calling reflect that can iteratively widen retrieval, follow links, and consolidate evidence before answering.
- **Fact-level causal links + link expansion** (`pipeline/links.py`): graph-walks at recall time to surface evidence chains, not just the top-K.
- **Bank lifecycle surface** — `brain.history()`, `brain.audit()` (gap analysis), `brain.export_bank()`, `brain.import_bank()`, `brain.bank_health()`, `brain.legal_hold()`. Each is exposed as a public method with full pipeline tracing.
- **`POST /v1/dsar/forget_principal`** (gateway): right-to-erasure endpoint for Cerebro. Enumerates a tenant's banks, erases all rows tagged `principal:{principal}`, returns per-bank deletion counts. The contract counterpart of `Synapse.DSAR.DeletionWorker` in the cerebro repo.
- **`POST /v1/compile`** + **`POST /v1/graph/search`** + **`POST /v1/graph/neighbors`** (gateway): wiki compile trigger, semantic graph search, neighbor expansion. Backed by new public methods `brain.graph_search()` and `brain.graph_neighbors()` and exposed as MCP tools.
- **S3 / Garage ingestion adapter** + **document folder ingestion adapter** (`adapters-ingestion-py/`): two new ingestion sources, available as gateway extras (`pip install astrocyte[ingest-s3]` / `astrocyte[ingest-document]`).
- **Hindsight-informed reference stack** (`pipeline/hindsight.py`): retrieval pipeline preset that mirrors the Hindsight paper's offline-augmentation pattern. Default for LoCoMo benchmark runs.
- **PgQueuer-backed memory task worker** (`pipeline/tasks.py`): out-of-band background work (compile, lifecycle sweeps) backed by Postgres queues; eliminates the in-process work loop.
- **Concurrent retain phase** (eval): token-bucket rate limiter on LongMemEval retain, ~7× speedup.
- **Configurable HNSW vector schema** (`adapters-storage-py/astrocyte-postgres/`): operators can tune `m` / `ef_construction` per bank.
- **Postgres reference stack parity**: `docker/postgres-age-pgvector/` ships a single image with both extensions; matches what the published `astrocyte-postgres` image bundles.

### Fixed

- **`pipeline/reflect.py`**: evidence-strict gate restored on weak retrieval to prevent hallucination when no high-confidence memories match.
- **`pipeline/observation.py`**: observation I/O now correctly routed through the dedicated `::obs` bank (previously could leak into the parent bank under some configs).
- **AGE adapter**: `_ensure_schema` now eagerly pre-creates the `Entity` vlabel and `LINK` elabel; removes a startup race where the first `store_entities` call could fail with "label not found." `store_entities` skips the embedding column when no embedding is supplied.
- **Entity extraction**: `raw_decode` now used to handle multi-array LLM responses (was failing with `Extra data` errors); `max_tokens` raised to 1024 to fit larger responses.
- **CodeQL**: path traversal in `portability.py` (CWE-022), `StatementNoEffect`, `EmptyExcept`, and unhandled `await` results across `pipeline/`, `tasks/`, and benchmark scripts.
- **Webhook ingest**: error responses now sanitised before being returned to the caller.

### Changed

- **Multi-arch publishing**: gateway image and reference Postgres image now published as `linux/amd64` AND `linux/arm64` manifests.
- **Reference providers**: warmed during gateway startup, not lazily on first request — eliminates the cold-start latency on the first `/v1/recall` after deploy.

### Eval

- LoCoMo retrieval pipeline rewritten with hindsight-informed reranking; significant accuracy gains on multi-session questions.
- LoCoMo retain phase concurrent with rate limiter (~7× faster).
- Benchmark history files updated through 2026-05-01.
- Stratified per-conversation sampling in `fair-bench` for reproducible category coverage.

## [0.9.1] — 2026-04-25 (patch — widen adapter dep range)

### Fixed

- **All adapter packages** (`astrocyte-postgres`, `astrocyte-qdrant`, `astrocyte-neo4j`, `astrocyte-elasticsearch`, `astrocyte-ingestion-*`, `astrocyte-llm-litellm`): dependency was `astrocyte>=0.7.0,<0.9`, causing `ResolutionImpossible` when installing alongside `astrocyte==0.9.0`. Widened to `<2`.

## [0.9.0] — 2026-04-25 (M8–M11 — wiki compile, time travel, gap analysis, entity resolution)

Four milestones completing the pre-GA feature surface. M8 (wiki compile) ships on the v0.8.x engineering track; M9–M11 constitute the v1.0.0 scope and are now fully implemented. v1.0.0 GA will be declared after the v0.9.x eval gates pass.

### Added

- **M8 — LLM wiki compile** (`pipeline/wiki_compile.py`, `pipeline/wiki_lint.py`): `WikiPage` memory type maintained by `CompileEngine`; retain-time async compile queue; threshold-based trigger; wiki-tier recall precedence (wiki hits ranked above raw memories); periodic lint pass catches contradictions, stale claims, and orphans; A/B eval harness with regression gate (≥10pp lift on LongMemEval `multi-session` / `knowledge-update`, no other category regressing >2pp). Opt-in per bank via MIP config (`compile: { enabled: true }`). `brain.compile(bank_id)` for manual trigger.
- **M9 — Time travel** (`pipeline/retrieval.py`, `types.py`, `_astrocyte.py`): `retained_at` timestamp stamped on every `VectorItem` at retain time and propagated through the full pipeline (`ScoredItem`, `MemoryHit`). `VectorFilters.as_of: datetime | None` filters retrieved memories to those retained on or before the given UTC timestamp. `brain.history(query, bank_id, as_of) → HistoryResult` convenience wrapper. `HistoryResult` carries `hits`, `total_available`, `truncated`, `as_of`, `bank_id`, and `trace`.
- **M10 — Gap analysis** (`pipeline/audit.py`, `types.py`, `_astrocyte.py`): `brain.audit(scope, bank_id) → AuditResult` — samples up to `max_memories` recent memories, sends them to an LLM judge with the operator-supplied `scope` description, and returns a `coverage_score` (0–1) plus a list of `GapItem(topic, severity, reason)`. Empty-bank fast path skips the LLM call entirely. Graceful fallback on malformed LLM response.
- **M11a — Entity resolution** (`pipeline/entity_resolution.py`, `provider.py`, `types.py`): `EntityLink` migrated to `entity_a` / `entity_b` (was `source_entity_id` / `target_entity_id`); added `evidence: str`, `confidence: float`, `created_at: datetime | None`. `EntityResolver` opt-in pipeline stage: at retain time, new entities are compared against existing candidates via `find_entity_candidates`; an LLM judge confirms aliases; confirmed pairs are stored as `alias_of` links. Resolution failures never abort retain. `GraphStore` SPI extended with `find_entity_candidates` and `store_entity_link`. `InMemoryGraphStore` implements both.
- **M11b — `astrocyte-age`** (`adapters-storage-py/astrocyte-age/`): new PyPI package. Apache AGE (PostgreSQL 16 graph extension) `GraphStore` implementation. Migrations in `migrations/` (AGE extension, graph DDL, memory-entity mapping table). `bootstrap_schema=True` (default) auto-creates on first connection. `docker/postgres-age-pgvector/Dockerfile` bundles AGE + pgvector on a single PG16 image. `astrocyte-services-py/docker-compose-age.yml` for local development. CI job in `adapters-storage-ci.yml`; `publish-astrocyte-age.yml` for PyPI Trusted Publishing; wired into `release.yml`.
- **Eval: checkpoint/resume** (`eval/checkpoint.py`): `BenchmarkCheckpoint` serialises per-question progress to JSON; `--resume` / `RESUME=1` skip already-scored questions on restart. Prevents full re-runs after network or quota failures.
- **Eval: `bench-full` target** (`Makefile`): runs LoCoMo and LongMemEval concurrently via `asyncio.gather`; unified summary table.
- **Eval: LoCoMo LLM judge** (`eval/judges/locomo_judge.py`): competitor-comparable LLM yes/no scoring alongside canonical stemmed-F1; `--canonical-judge` flag selects scoring path.
- **Docs: benchmark roadmap** (`docs/_design/benchmark-roadmap.md`): three-tier benchmark strategy — Tier 1 (LoCoMo + LongMemEval, current), Tier 2 (AMA-Bench, agentic trajectory memory, planned), Tier 3 (MemoryArena, task-execution outcomes, planned).

### Fixed

- **`retained_at` propagation**: field was silently dropped at two pipeline stages (`_semantic_search` and `basic_rerank`). Fixed at all four transformation points so `MemoryHit.retained_at` reliably reflects the original retain timestamp.
- **Temporal strategy `as_of` bypass**: `_temporal_search` used `list_vectors()` which bypassed the `VectorFilters.as_of` filter. Fixed by passing `as_of` through `parallel_retrieve → _temporal_search` and applying the filter in the scan loop.
- **`basic_rerank` field loss**: creating new `ScoredItem` instances without copying `memory_layer` or `retained_at`. Both fields now propagated.

### Changed

- **`EntityLink` field names**: `source_entity_id` → `entity_a`, `target_entity_id` → `entity_b`. **Migration note**: callers constructing `EntityLink` directly must update keyword arguments; the old names are not aliased.
- **PyPI / install**: `astrocyte-age` and updated adapters require **`astrocyte>=0.8.0,<2`** to survive the v0.9.x → v1.0.x version boundary cleanly.

## [0.8.1] — 2026-04-22 (eval polish, competitor adapters, observability)

Finalises the evaluation harness, wires the competitor adapters, tightens the RRF API contract, and adds telemetry for unstamped forget records. No new runtime features; all changes are either bug fixes, new test coverage, or hardening for production benchmark runs.

### Fixed

- **LoCoMo category map** (`eval/benchmarks/locomo.py`, `eval/judges/locomo_judge.py`): categories 1 and 4 were swapped and category 3 was mislabeled as temporal (it is open-domain). **Operator note**: per-category columns in benchmark snapshots produced before this release carry incorrect labels; re-run to get accurate per-category F1.
- **LoCoMo canonical-judge scoring dispatch**: `cid == 3` (open-domain) now uses the correct open-domain scoring path; `cid in {2, 4}` uses plain F1 (was treating open-domain as temporal).
- **`build_competitor_brain` factory** (`eval/competitors/base.py`): both `"mem0"` and `"zep"` branches now import from the correct module paths and pass `llm_provider` to the adapter constructors.

### Fixed

- **LoCoMo category map** (`eval/benchmarks/locomo.py`, `eval/judges/locomo_judge.py`): categories 1 and 4 were swapped and category 3 was mislabeled as temporal (it is open-domain). **Operator note**: per-category columns in benchmark snapshots produced before this release carry incorrect labels; re-run to get accurate per-category F1.
- **LoCoMo canonical-judge scoring dispatch**: `cid == 3` (open-domain) now uses the correct open-domain scoring path; `cid in {2, 4}` uses plain F1 (was treating open-domain as temporal).
- **`build_competitor_brain` factory** (`eval/competitors/base.py`): both `"mem0"` and `"zep"` branches now import from the correct module paths and pass `llm_provider` to the adapter constructors.

### Added

- **`LoCoMoResult.canonical_f1_overall` / `canonical_f1_by_category`**: per-category F1 is accumulated during the eval loop and reported when `use_canonical_judge=True`.
- **Benchmark serializer `judge` / `system` fields** (`scripts/run_benchmarks.py`): output JSON now carries `judge: "canonical"|"legacy"` and `system: str` for unambiguous matrix comparisons.
- **Expanded abstention phrases** (`eval/judges/locomo_judge.py`): ~20 real-world LLM "I don't know" output variants added to `_ABSTENTION_PHRASES` so the judge doesn't count hedged non-answers as wrong.
- **`Mem0BrainAdapter`** (`eval/competitors/mem0_adapter.py`): `retain` / `recall` / `reflect` fully wired against Mem0 SDK v1. Supports both sync and async clients via `asyncio.iscoroutinefunction` detection; merges `tags` into `metadata`; shapes results into `MemoryHit` / `RecallResult`.
- **`ZepBrainAdapter`** (`eval/competitors/zep_adapter.py`): `retain` / `recall` / `reflect` wired against `zep-python` v1.x (OSS / self-hosted). Lazy session creation with per-instance cache; `asyncio.to_thread` bridging for sync clients; maps `MemorySearchResult.message` → `MemoryHit`.
- **`astrocyte_forget_unstamped_records_total` counter** (`_astrocyte.py`): emitted in `_collect_too_young_ids` for each record missing an `occurred_at` stamp, labelled by `bank_id`. Operators can alert on unexpected spikes caused by ingestion pipelines that drop timestamps.
- **E2E integration test** (`tests/test_e2e_pipeline_integration.py`): 15 tests covering identity classification → MIP routing → temporal recall → intent-weighted fusion → forget across a single `InMemoryEngineProvider` brain.
- **Competitor adapter contract tests** (`tests/test_competitor_adapters.py`): 35 tests pinning the `CompetitorBrain` duck-type surface for both adapters; Zep SDK is injected via `sys.modules` so the suite runs without `zep-python` installed.

### Changed

- **`weighted_rrf_fusion` rejects negative weights** (`pipeline/fusion.py`): passing a weight `< 0.0` now raises `ValueError("RRF weight must be >= 0.0")` instead of silently clamping to 0.0. Negative weights are a caller sign error, not a feature; the explicit raise surfaces the bug immediately.

## [0.8.0] — 2026-04-12 (M5 + M6 + M7 — production adapters, gateway, recall authority)

This release bundles **M5** (production storage providers), **M6** (standalone HTTP gateway), and **M7** (structured recall authority) on one minor version line, per `docs/_design/product-roadmap.md`. Subsequent connector and edge work ships as **v0.8.1**, **v0.8.2**, …

### Changed

- **PyPI / install**: adapter and gateway packages now require **`astrocyte>=0.7.0,<2`** (was **`<0.8`**) so **v0.8.x** and later resolve cleanly; pin **`astrocyte==0.8.0`** (and matching adapter versions) together for the M5–M7 milestone bundle.

### Added

- **M5 — Storage adapters** (separate PyPI packages under `adapters-storage-py/`): **`astrocyte-postgres`**, **`astrocyte-qdrant`**, **`astrocyte-neo4j`**, **`astrocyte-elasticsearch`** implementing Tier 1 `VectorStore` / `GraphStore` / `DocumentStore`; CI and publish workflows.
- **M6 — `astrocyte-gateway-py`**: FastAPI REST (`/v1/retain`, `/v1/recall`, `/v1/reflect`, `/v1/forget`), webhook ingest, health (`/health`, `/health/ingest`), optional admin routes, JWT/OIDC/`api_key`/`dev` auth → `AstrocyteContext`, Docker/Compose/Helm, GHCR image workflow.
- **M7 — Structured recall authority**: optional `recall_authority:` in config (`RecallAuthorityConfig`), `RecallResult.authority_context`, optional reflect injection; see ADR-004 and `built-in-pipeline.md`.
- **M4.1 — Federated / proxy recall**: `sources:` with `type: proxy`, remote JSON merge with local RRF (`astrocyte.recall.proxy`); OAuth helpers, tiered retrieval integration, observability hooks — see prior `[Unreleased]` notes in git history for detail.
- **Ingest transports**: `astrocyte-ingestion-kafka`, `astrocyte-ingestion-redis` (streams), `astrocyte-ingestion-github` (poll); `astrocyte[poll]` / `astrocyte[stream]` extras.
- **Gateway edge hardening (v0.8.x track)**: optional **`ASTROCYTE_RATE_LIMIT_PER_SECOND`**, documented CORS/body limits; **`scripts/bench_gateway_overhead.py --max-overhead-p99-ms`**; OpenAPI path contract tests; operator doc **[Gateway edge & API gateways](docs/_end-user/gateway-edge-and-api-gateways.md)**.

### Notes

- **SPI contract tests** for vector stores remain the reference for adapter authors (`astrocyte-py/tests/test_spi_vector_store_contract.py`).
- Release: tag **`v0.8.0`** at repo root → **`release.yml`** publishes **`astrocyte`** → **`astrocyte-postgres`** → gateway image; see **`RELEASING.md`**.

## [0.7.0] — 2026-04-11 (M4 external data sources — library ingest)

### Added

- **`astrocyte.ingest`**: `IngestSource` protocol, `WebhookIngestSource`, `SourceRegistry`, HMAC helpers, `handle_webhook_ingest` → `brain.retain()`; `IngestError`.
- Bank resolution: `target_bank` / `target_bank_template` with `{principal}`; JSON webhook body (`content` / `text`, optional `metadata`, …).

### Notes

- **M4.1** (proxy recall) and later milestones ship in **v0.8.0+**; see **[0.8.0]** above.

## [0.6.0] — 2026-04-11 (M3 extraction pipeline)

### Added

- Inbound extraction chain: **normalize → chunk → optional LLM entity extraction → embed → store**, with `content_type` routing and `extraction_profiles` (including `builtin_text` / `builtin_conversation`).
- Profile-driven `metadata_mapping`, `tag_rules`, `entity_extraction`, and `fact_type` on retain.
- Packaged defaults: `astrocyte/pipeline/extraction_builtin.yaml`; stable imports: `prepare_retain_input`, `merged_extraction_profiles`, `extraction_profile_for_source`, `PreparedRetainInput`.

[Unreleased]: https://github.com/AstrocyteAI/astrocyte/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/AstrocyteAI/astrocyte/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/AstrocyteAI/astrocyte/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/AstrocyteAI/astrocyte/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.8.0
[0.7.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.7.0
[0.6.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.6.0
