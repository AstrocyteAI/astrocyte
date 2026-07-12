# Python public API index

Every name exported from `astrocyte` (`astrocyte.__all__`) — the import surface
covered by the stability policy. Import from the package root, not submodules:

```python
from astrocyte import Astrocyte, RecallRequest, AccessDenied
```

This page is **generated** by `docs/scripts/generate-api-index.py`; regenerate
after changing the public surface. CI fails if a public name is missing from
the docs, and the surface itself is pinned by
`astrocyte-py/tests/test_public_api_surface.py` plus a griffe breaking-change
gate. For usage-oriented documentation see the
[Memory API reference](memory-api-reference/).

## Classes & types (62)

| Name | Summary |
|---|---|
| `AccessGrant` | AccessGrant(bank_id: 'str', principal: 'str', permissions: 'list[str]') |
| `ActorIdentity` | Structured actor for access control and bank resolution (ADR-002) |
| `Astrocyte` | The Astrocyte memory framework — unified API for AI agent memory |
| `AstrocyteContext` | Caller identity for access control |
| `AuditEvent` | AuditEvent(event_type: 'str', bank_id: 'str', actor: 'str', timestamp: 'datetime', memory_ids: 'list[str] \| None' = None, reason: 'str \| None' = None, metadata: 'Metadata \| None' = None) |
| `BankHealth` | BankHealth(bank_id: 'str', score: 'float', status: "Literal['healthy', 'warning', 'unhealthy']", issues: 'list[HealthIssue]', metrics: 'dict[str, float]', assessed_at: 'datetime') |
| `BankResolver` | Convention-based default bank id from a resolved :class:`ActorIdentity` |
| `Completion` | Completion(text: 'str', model: 'str', usage: 'TokenUsage \| None' = None, tool_calls: 'list[ToolCall] \| None' = None) |
| `ContentPart` | Tagged union for multimodal content |
| `DataClassification` | DataClassification(level: 'int', label: 'str', categories: 'list[str] \| None' = None, classified_by: 'str' = 'rules', classified_at: 'datetime \| None' = None) |
| `Dispositions` | Personality modifiers for synthesis |
| `Document` | Document(id: 'str', text: 'str', metadata: 'Metadata \| None' = None, tags: 'list[str] \| None' = None) |
| `DocumentFilters` | DocumentFilters(tags: 'list[str] \| None' = None, metadata_filters: 'Metadata \| None' = None) |
| `DocumentHit` | DocumentHit(document_id: 'str', text: 'str', score: 'float', metadata: 'Metadata \| None' = None) |
| `DocumentStore` | SPI for document storage with full-text search. Optional for Tier 1 |
| `EngineCapabilities` | EngineCapabilities(supports_reflect: 'bool' = False, supports_forget: 'bool' = False, supports_semantic_search: 'bool' = True, supports_keyword_search: 'bool' = False, supports_graph_search: 'bool' = False, supports_temporal_search: 'bool' = False, supports_dispositions: 'bool' = False, supports_consolidation: 'bool' = False, supports_entities: 'bool' = False, supports_tags: 'bool' = False, supports_metadata: 'bool' = True, supports_compile: 'bool' = False, max_retain_bytes: 'int \| None' = None, max_recall_results: 'int \| None' = None, max_embedding_dims: 'int \| None' = None) |
| `EngineProvider` | SPI for full-stack memory engines |
| `Entity` | Entity(id: 'str', name: 'str', entity_type: 'str', aliases: 'list[str] \| None' = None, metadata: 'Metadata \| None' = None, embedding: 'list[float] \| None' = None, mention_count: 'int' = 1) |
| `EntityLink` | A typed relationship between two entities in the knowledge graph |
| `EvalMetrics` | EvalMetrics(recall_precision: 'float', recall_hit_rate: 'float', recall_mrr: 'float', recall_ndcg: 'float', retain_latency_p50_ms: 'float', retain_latency_p95_ms: 'float', recall_latency_p50_ms: 'float', recall_latency_p95_ms: 'float', total_tokens_used: 'int', total_duration_seconds: 'float', reflect_accuracy: 'float \| None' = None, reflect_completeness: 'float \| None' = None, reflect_hallucination_rate: 'float \| None' = None, reflect_latency_p50_ms: 'float \| None' = None, reflect_latency_p95_ms: 'float \| None' = None, recall_coverage: 'float \| None' = None, recall_reflect_gap: 'dict[str, int] \| None' = None, abstention_rate_adversarial: 'float \| None' = None, tokens_by_phase: 'dict[str, int] \| None' = None, api_calls_total: 'int \| None' = None, api_calls_by_endpoint: 'dict[str, int] \| None' = None, cost_total_usd: 'float \| None' = None, cost_per_question_usd: 'float \| None' = None, retain_latency_p99_ms: 'float \| None' = None, recall_latency_p99_ms: 'float \| None' = None, reflect_latency_p99_ms: 'float \| None' = None, e2e_per_question_p50_ms: 'float \| None' = None, e2e_per_question_p95_ms: 'float \| None' = None, error_count_by_type: 'dict[str, int] \| None' = None, openai_retry_count: 'int \| None' = None, failed_retain_count: 'int \| None' = None, cascade_decisions: 'dict[str, int] \| None' = None, entities_resolved_count: 'int \| None' = None, entities_created_count: 'int \| None' = None, composite_score_distribution: 'dict[str, int] \| None' = None, wiki_pages_total: 'int \| None' = None, wiki_pages_per_persona: 'float \| None' = None) |
| `EvalResult` | EvalResult(suite: 'str', provider: 'str', provider_tier: 'str', timestamp: 'datetime', metrics: 'EvalMetrics', per_query_results: 'list[QueryResult]', config_snapshot: 'Metadata \| None' = None) |
| `ForgetRequest` | ForgetRequest(bank_id: 'str', memory_ids: 'list[str] \| None' = None, tags: 'list[str] \| None' = None, before_date: 'datetime \| None' = None, scope: 'str \| None' = None) |
| `ForgetResult` | ForgetResult(deleted_count: 'int', archived_count: 'int' = 0) |
| `ForgetSelector` | ForgetSelector(bank_ids: 'list[str]', scope: 'str \| None' = None, tags: 'list[str] \| None' = None, before_date: 'datetime \| None' = None, memory_ids: 'list[str] \| None' = None) |
| `GraphHit` | GraphHit(memory_id: 'str', text: 'str', connected_entities: 'list[str]', depth: 'int', score: 'float') |
| `GraphStore` | SPI for graph database adapters. Optional for Tier 1 |
| `HealthIssue` | HealthIssue(severity: "Literal['info', 'warning', 'critical']", code: 'str', message: 'str', recommendation: 'str') |
| `HealthStatus` | HealthStatus(healthy: 'bool', message: 'str \| None' = None, latency_ms: 'float \| None' = None, last_check_at: 'datetime \| None' = None) |
| `HookEvent` | HookEvent(event_id: 'str', type: 'str', timestamp: 'datetime', bank_id: 'str \| None' = None, data: 'Metadata \| None' = None, trace_id: 'str \| None' = None) |
| `HttpClientContext` | HttpClientContext(proxy: 'str \| None' = None, ca_bundle: 'str \| None' = None, headers: 'dict[str, str] \| None' = None, timeouts: 'dict[str, float] \| None' = None) |
| `HybridEngineProvider` | Merges recall from an ``EngineProvider`` and a ``PipelineOrchestrator`` |
| `IdentityConfig` | Identity-driven bank resolution and ACL helpers (M1–M2 / v0.5.0) |
| `LegalHold` | LegalHold(hold_id: 'str', bank_id: 'str', reason: 'str', set_at: 'datetime', set_by: 'str') |
| `LifecycleAction` | Result of a lifecycle TTL evaluation on a single memory |
| `LifecycleRunResult` | LifecycleRunResult(archived_count: 'int', deleted_count: 'int', skipped_count: 'int', actions: 'list[LifecycleAction]') |
| `LLMCapabilities` | LLMCapabilities(supports_multimodal_completion: 'bool' = False, modalities_supported: 'tuple[str, ...] \| None' = None, supports_multimodal_embedding: 'bool' = False, supports_batch_embed: 'bool' = True) |
| `LLMProvider` | SPI for LLM access needed by the Astrocyte core pipeline |
| `MemoryEntityAssociation` | MemoryEntityAssociation(memory_id: 'str', entity_id: 'str') |
| `MemoryHit` | MemoryHit(text: 'str', score: 'float', fact_type: 'str \| None' = None, metadata: 'Metadata \| None' = None, tags: 'list[str] \| None' = None, occurred_at: 'datetime \| None' = None, source: 'str \| None' = None, memory_id: 'str \| None' = None, bank_id: 'str \| None' = None, memory_layer: 'str \| None' = None, utility_score: 'float \| None' = None, retained_at: 'datetime \| None' = None, chunk_id: 'str \| None' = None) |
| `MemoryUsage` | MemoryUsage(memory_id: 'str', text: 'str', recall_count: 'int', last_recalled_at: 'datetime') |
| `Message` | Message(role: 'str', content: 'str \| list[ContentPart]' = '', tool_calls: 'list[ToolCall] \| None' = None, tool_call_id: 'str \| None' = None, name: 'str \| None' = None) |
| `MultiBankStrategy` | Multi-bank recall behavior. Default ``parallel`` matches legacy ``banks=[...]`` without an explicit strategy |
| `OutboundTransportProvider` | SPI for credential gateways and enterprise HTTP/TLS proxies |
| `PiiMatch` | PiiMatch(pii_type: 'str', start: 'int', end: 'int', matched_text: 'str', replacement: 'str \| None' = None) |
| `PreparedRetainInput` | Result of Raw → normalizer → metadata/tags merge (before chunk/embed in orchestrator) |
| `QualityDataPoint` | QualityDataPoint(date: 'date', retain_count: 'int', recall_count: 'int', recall_hit_rate: 'float', avg_recall_score: 'float', dedup_rate: 'float', reflect_success_rate: 'float') |
| `QueryResult` | QueryResult(query: 'str', expected: 'list[str]', actual: 'list[MemoryHit]', relevant_found: 'int', precision: 'float', reciprocal_rank: 'float', latency_ms: 'float') |
| `RecallRequest` | RecallRequest(query: 'str', bank_id: 'str', max_results: 'int' = 10, max_tokens: 'int \| None' = None, fact_types: 'list[str] \| None' = None, tags: 'list[str] \| None' = None, time_range: 'tuple[datetime, datetime] \| None' = None, include_sources: 'bool' = False, layer_weights: 'dict[str, float] \| None' = None, detail_level: 'str \| None' = None, external_context: 'list[MemoryHit] \| None' = None, as_of: 'datetime \| None' = None, query_reference_date: 'datetime \| None' = None, dispositions: "'Dispositions \| None'" = None, session_id: 'str \| None' = None) |
| `RecallResult` | RecallResult(hits: 'list[MemoryHit]', total_available: 'int', truncated: 'bool', trace: 'RecallTrace \| None' = None, authority_context: 'str \| None' = None, top_semantic_score: 'float' = 0.0) |
| `RecallTrace` | RecallTrace(strategies_used: 'list[str] \| None' = None, total_candidates: 'int \| None' = None, fusion_method: 'str \| None' = None, latency_ms: 'float \| None' = None, strategy_timings_ms: 'dict[str, float] \| None' = None, strategy_candidate_counts: 'dict[str, int] \| None' = None, tier_used: 'int \| None' = None, layer_distribution: 'dict[str, int] \| None' = None, cache_hit: 'bool \| None' = None, wiki_tier_used: 'bool \| None' = None) |
| `ReflectRequest` | ReflectRequest(query: 'str', bank_id: 'str', max_tokens: 'int \| None' = None, include_sources: 'bool' = True, dispositions: 'Dispositions \| None' = None, tags: 'list[str] \| None' = None, as_of: 'datetime \| None' = None, query_reference_date: 'datetime \| None' = None) |
| `ReflectResult` | ReflectResult(answer: 'str', confidence: 'float \| None' = None, sources: 'list[MemoryHit] \| None' = None, observations: 'list[str] \| None' = None, authority_context: 'str \| None' = None) |
| `RegressionAlert` | RegressionAlert(metric: 'str', current_value: 'float', baseline_value: 'float', delta: 'float', delta_percent: 'float', severity: "Literal['warning', 'critical']") |
| `RetainRequest` | RetainRequest(content: 'str', bank_id: 'str', metadata: 'Metadata \| None' = None, tags: 'list[str] \| None' = None, occurred_at: 'datetime \| None' = None, source: 'str \| None' = None, content_type: 'str' = 'text', extraction_profile: 'str \| None' = None, mip_pipeline: 'PipelineSpec \| None' = None, mip_rule_name: 'str \| None' = None) |
| `RetainResult` | RetainResult(stored: 'bool', memory_id: 'str \| None' = None, deduplicated: 'bool' = False, error: 'str \| None' = None, retention_action: 'str \| None' = None, curated: 'bool' = False, memory_layer: 'str \| None' = None) |
| `RoutingDecision` | Output of MIP routing — tells Astrocyte where/how to store |
| `TokenUsage` | TokenUsage(input_tokens: 'int', output_tokens: 'int') |
| `TransportCapabilities` | TransportCapabilities(supports_proxy: 'bool' = False, supports_custom_ca: 'bool' = False, supports_client_cert: 'bool' = False, supports_headers: 'bool' = False) |
| `VectorFilters` | VectorFilters(bank_id: 'str \| None' = None, tags: 'list[str] \| None' = None, fact_types: 'list[str] \| None' = None, time_range: 'tuple[datetime, datetime] \| None' = None, metadata_filters: 'Metadata \| None' = None, as_of: 'datetime \| None' = None, session_id: 'str \| None' = None) |
| `VectorHit` | VectorHit(id: 'str', text: 'str', score: 'float', metadata: 'Metadata \| None' = None, tags: 'list[str] \| None' = None, fact_type: 'str \| None' = None, occurred_at: 'datetime \| None' = None, memory_layer: 'str \| None' = None, retained_at: 'datetime \| None' = None, chunk_id: 'str \| None' = None) |
| `VectorItem` | VectorItem(id: 'str', bank_id: 'str', vector: 'list[float]', text: 'str', metadata: 'Metadata \| None' = None, tags: 'list[str] \| None' = None, fact_type: 'str \| None' = None, occurred_at: 'datetime \| None' = None, memory_layer: 'str \| None' = None, retained_at: 'datetime \| None' = None, chunk_id: 'str \| None' = None) |
| `VectorStore` | SPI for vector database adapters. Required for Tier 1 |

## Functions (24)

| Name | Summary |
|---|---|
| `accessible_read_banks` | Bank IDs where ``context`` has effective ``read`` after OBO intersection |
| `auth_with_oauth_cache_namespace` | Attach ``_oauth_cache_id`` so OAuth token caches do not collide across proxy sources |
| `build_proxy_headers` | Build HTTP headers (Bearer, API key, OAuth2 client_credentials / refresh, optional static ``headers``) |
| `clear_oauth2_token_cache_for_tests` | Test helper — clear in-memory OAuth token cache |
| `effective_permissions` | Effective permission set for ``bank_id``, including OBO intersection |
| `exchange_oauth2_authorization_code` | Exchange an authorization code for tokens (RFC 6749 §4.1.3) |
| `extraction_profile_for_source` | Resolve ``sources.*.extraction_profile`` for ingest callers (full webhook wiring is M4) |
| `fetch_oauth2_client_credentials_token` | Obtain an access token (RFC 6749 ``client_credentials``). Cached until shortly before expiry |
| `fetch_oauth2_refresh_access_token` | Obtain an access token via ``grant_type=refresh_token`` (RFC 6749) |
| `fetch_proxy_recall_hits` | Call ``source.url`` (GET or POST) and parse JSON ``hits`` / ``results`` arrays |
| `format_principal` | Serialize actor to the grant-matching principal form ``\{type\}:\{id\}`` |
| `gather_proxy_hits_for_bank` | Fetch hits from all ``type: proxy`` sources whose ``target_bank`` matches ``bank_id`` |
| `log_safe` | Return a log-safe string representation of ``value`` |
| `merge_external_into_recall_result` | Combine external hits with ``result.hits`` by score, dedupe by text, trim to ``max_results`` |
| `merge_manual_and_proxy_hits` | Combine caller ``external_context`` with configured proxy sources for this bank |
| `merged_extraction_profiles` | Return builtins plus ``config.extraction_profiles`` (user definitions override same-named builtins) |
| `Metadata` | dict() -> new empty dictionary |
| `parse_principal` | Parse ``principal`` string into :class:`ActorIdentity` (ADR-002) |
| `post_oauth2_token_endpoint` | POST ``application/x-www-form-urlencoded`` to the token endpoint |
| `prepare_retain_input` | Single entrypoint for the pre-chunk extraction chain (normalizer + profile fields) |
| `resolve_actor` | Resolved actor: explicit ``context.actor``, else parse ``context.principal`` |
| `resolve_provider` | Resolve a single provider by name from entry points, or by import path |
| `validate_proxy_recall_dns` | Async-safe DNS check: resolve *host* in a worker thread, then validate all addresses |
| `validate_proxy_recall_url` | Reject URLs that enable SSRF (private/loopback/metadata-ranged IPs, non-HTTP(S), no host) |

## Exceptions (11)

| Name | Summary |
|---|---|
| `AccessDenied` | Principal lacks required permission on bank |
| `AstrocyteError` | Base exception for all Astrocyte errors |
| `CapabilityNotSupported` | The provider does not support the requested capability |
| `ConfigError` | Configuration is invalid or missing |
| `CrossBorderViolation` | Operation would violate data residency policy |
| `IngestError` | Inbound ingest (webhook, stream, …) rejected a payload or configuration |
| `LegalHoldActive` | Operation blocked because bank is under legal hold |
| `MipRoutingError` | MIP routing configuration or evaluation error |
| `PiiRejected` | Content rejected due to PII detection policy |
| `ProviderUnavailable` | Provider is unreachable or circuit breaker is open |
| `RateLimited` | Request exceeds rate limit |

## Constants & aliases (3)

| Name | Summary |
|---|---|
| `MetadataValue` | Represent a PEP 604 union type |
| `PLACE_BANK` | str(object='') -> str |
| `PLACE_QUERY` | str(object='') -> str |

