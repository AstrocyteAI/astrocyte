# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The Python package `astrocyte` is built from `astrocyte-py/`; its version comes from Git tags via Hatch VCS.

## [Unreleased]

### Added

- **M4.1 — Federated / proxy recall**: `sources:` entries with `type: proxy`, `url` (supports `{query}`), and `target_bank` fetch remote JSON (`hits` or `results`) and merge with local retrieval via **RRF** in `PipelineOrchestrator.recall`. Callers may also pass `RecallRequest.external_context`. Module `astrocyte.recall.proxy` (`fetch_proxy_recall_hits`, `gather_proxy_hits_for_bank`, `merge_manual_and_proxy_hits`). Tier-2 engine-only recall merges proxy hits without double-applying them for `HybridEngineProvider`.
- **M4.1 extensions**: optional **`recall_method: POST`** and **`recall_body`** (JSON dict/str with `__astrocyte.query__` / `__astrocyte.bank_id__` placeholders); **`auth.type: api_key`** plus optional **`auth.headers`**; **`auth.type: oauth2_client_credentials`** (`token_url`, `client_id`, `client_secret`, optional `scope`) with in-memory token cache (`astrocyte.recall.oauth`). OTel span `astrocyte.proxy_recall` when tracing is enabled. **`observability.prometheus_enabled`**: counters **`astrocyte_proxy_recall_total`** (`source_id`, `status`) and histogram **`astrocyte_proxy_recall_duration_seconds`** (`source_id`) on each proxy HTTP call (metrics passed from `Astrocyte` into proxy merge). **`TieredRetriever`** skips cache when `external_context` is set, merges federated hits on BM25 shortcut, and preserves `external_context` through tier-4 reformulation. **`tiered_retrieval.enabled`**: `Astrocyte.set_pipeline` builds a `TieredRetriever` over the pipeline (optional `recall_cache` from config); **`Astrocyte._do_recall`** delegates to it instead of `PipelineOrchestrator.recall` when enabled (Tier-1 pipeline path only; not used when recall goes through a Tier-2 engine provider alone or through `HybridEngineProvider`). Stable imports: `merge_external_into_recall_result`, `PLACE_QUERY`, `PLACE_BANK`, `build_proxy_headers`, `clear_oauth2_token_cache_for_tests` on `astrocyte` and `astrocyte.recall`.
- Core dependency **`httpx`** (outbound proxy GET).
- Optional **`[gateway]`** extra: Starlette ASGI app `create_ingest_webhook_app` for `POST /v1/ingest/webhook/{source_id}` (thin HTTP binding ahead of full M6 gateway).
- **SPI contract tests** (`tests/test_spi_vector_store_contract.py`) as the reference harness for M5 vector store adapters.

## [0.7.0] — 2026-04-11 (M4 external data sources — library ingest)

### Added

- **`astrocyte.ingest`**: `IngestSource` protocol, `WebhookIngestSource`, `SourceRegistry`, HMAC helpers, `handle_webhook_ingest` → `brain.retain()`; `IngestError`.
- Bank resolution: `target_bank` / `target_bank_template` with `{principal}`; JSON webhook body (`content` / `text`, optional `metadata`, …).

### Notes

- **Proxy / federated recall** (M4.1) ships in the next release after v0.7.0 — see `[Unreleased]` / `docs/_design/product-roadmap-v1.md` §M4.1.
- Full **standalone gateway** (JWT, Docker, admin routes) remains **M6**.

## [0.6.0] — 2026-04-11 (M3 extraction pipeline)

### Added

- Inbound extraction chain: **normalize → chunk → optional LLM entity extraction → embed → store**, with `content_type` routing and `extraction_profiles` (including `builtin_text` / `builtin_conversation`).
- Profile-driven `metadata_mapping`, `tag_rules`, `entity_extraction`, and `fact_type` on retain.
- Packaged defaults: `astrocyte/pipeline/extraction_builtin.yaml`; stable imports: `prepare_retain_input`, `merged_extraction_profiles`, `extraction_profile_for_source`, `PreparedRetainInput`.

[Unreleased]: https://github.com/AstrocyteAI/astrocyte/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.7.0
[0.6.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.6.0
