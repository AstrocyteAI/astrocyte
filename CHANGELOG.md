# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The Python package `astrocyte` is built from `astrocyte-py/`; its version comes from Git tags via Hatch VCS.

## [Unreleased]

## [0.8.0] — 2026-04-12 (M5 + M6 + M7 — production adapters, gateway, recall authority)

This release bundles **M5** (production storage providers), **M6** (standalone HTTP gateway), and **M7** (structured recall authority) on one minor version line, per `docs/_design/product-roadmap-v1.md`. Subsequent connector and edge work ships as **v0.8.1**, **v0.8.2**, …

### Changed

- **PyPI / install**: adapter and gateway packages now require **`astrocyte>=0.7.0,<0.9`** (was **`<0.8`**) so **v0.8.x** resolves cleanly; pin **`astrocyte==0.8.0`** (and matching adapter versions) together for the M5–M7 milestone bundle.

### Added

- **M5 — Storage adapters** (separate PyPI packages under `adapters-storage-py/`): **`astrocyte-pgvector`**, **`astrocyte-qdrant`**, **`astrocyte-neo4j`**, **`astrocyte-elasticsearch`** implementing Tier 1 `VectorStore` / `GraphStore` / `DocumentStore`; CI and publish workflows.
- **M6 — `astrocyte-gateway-py`**: FastAPI REST (`/v1/retain`, `/v1/recall`, `/v1/reflect`, `/v1/forget`), webhook ingest, health (`/health`, `/health/ingest`), optional admin routes, JWT/OIDC/`api_key`/`dev` auth → `AstrocyteContext`, Docker/Compose/Helm, GHCR image workflow.
- **M7 — Structured recall authority**: optional `recall_authority:` in config (`RecallAuthorityConfig`), `RecallResult.authority_context`, optional reflect injection; see ADR-004 and `built-in-pipeline.md`.
- **M4.1 — Federated / proxy recall**: `sources:` with `type: proxy`, remote JSON merge with local RRF (`astrocyte.recall.proxy`); OAuth helpers, tiered retrieval integration, observability hooks — see prior `[Unreleased]` notes in git history for detail.
- **Ingest transports**: `astrocyte-ingestion-kafka`, `astrocyte-ingestion-redis` (streams), `astrocyte-ingestion-github` (poll); `astrocyte[poll]` / `astrocyte[stream]` extras.
- **Gateway edge hardening (v0.8.x track)**: optional **`ASTROCYTE_RATE_LIMIT_PER_SECOND`**, documented CORS/body limits; **`scripts/bench_gateway_overhead.py --max-overhead-p99-ms`**; OpenAPI path contract tests; operator doc **[Gateway edge & API gateways](docs/_end-user/gateway-edge-and-api-gateways.md)**.

### Notes

- **SPI contract tests** for vector stores remain the reference for adapter authors (`astrocyte-py/tests/test_spi_vector_store_contract.py`).
- Release: tag **`v0.8.0`** at repo root → **`release.yml`** publishes **`astrocyte`** → **`astrocyte-pgvector`** → gateway image; see **`RELEASING.md`**.

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

[Unreleased]: https://github.com/AstrocyteAI/astrocyte/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.8.0
[0.7.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.7.0
[0.6.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.6.0
