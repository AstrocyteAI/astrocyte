# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The Python package `astrocyte` is built from `astrocyte-py/`; its version comes from Git tags via Hatch VCS.

## [Unreleased]

### Added

- Optional **`[gateway]`** extra: Starlette ASGI app `create_ingest_webhook_app` for `POST /v1/ingest/webhook/{source_id}` (thin HTTP binding ahead of full M6 gateway).
- **SPI contract tests** (`tests/test_spi_vector_store_contract.py`) as the reference harness for M5 vector store adapters.

## [0.7.0] — 2026-04-11 (M4 external data sources — library ingest)

### Added

- **`astrocyte.ingest`**: `IngestSource` protocol, `WebhookIngestSource`, `SourceRegistry`, HMAC helpers, `handle_webhook_ingest` → `brain.retain()`; `IngestError`.
- Bank resolution: `target_bank` / `target_bank_template` with `{principal}`; JSON webhook body (`content` / `text`, optional `metadata`, …).

### Notes

- **Proxy / federated recall** (merge external API hits with local recall) is **not** in this release — tracked as **M4.1 / federated recall** (see `product-roadmap-v1.md`).
- Full **standalone gateway** (JWT, Docker, admin routes) remains **M6**.

## [0.6.0] — 2026-04-11 (M3 extraction pipeline)

### Added

- Inbound extraction chain: **normalize → chunk → optional LLM entity extraction → embed → store**, with `content_type` routing and `extraction_profiles` (including `builtin_text` / `builtin_conversation`).
- Profile-driven `metadata_mapping`, `tag_rules`, `entity_extraction`, and `fact_type` on retain.
- Packaged defaults: `astrocyte/pipeline/extraction_builtin.yaml`; stable imports: `prepare_retain_input`, `merged_extraction_profiles`, `extraction_profile_for_source`, `PreparedRetainInput`.

[Unreleased]: https://github.com/AstrocyteAI/astrocyte/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.7.0
[0.6.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.6.0
