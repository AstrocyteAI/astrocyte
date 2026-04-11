# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow repository tags (Python package version is derived from Git via Hatch VCS when building `astrocyte-py`).

## [Unreleased]

### Added

- **M3 extraction polish**: stable imports `prepare_retain_input`, `merged_extraction_profiles`, `extraction_profile_for_source`, `PreparedRetainInput` from `astrocyte` and `astrocyte.pipeline`.
- **Packaged default profiles**: `astrocyte/pipeline/extraction_builtin.yaml` merged over code builtins (user `extraction_profiles` still wins); included in wheels via Hatch `force-include`.
- **`ExtractionProfileConfig.fact_type`**: sets `VectorItem.fact_type` for retained chunks (default `world` when unset).
- **Benchmark smoke**: optional perf test for normalize + chunk on large text (`ASTROCYTE_RUN_PERF=1`).

## [0.6.0] — 2026-04-11 (M3 extraction pipeline)

### Added

- Inbound extraction chain: **normalize → chunk → optional LLM entity extraction → embed → store**, with `content_type` routing and `extraction_profiles` (including `builtin_text` / `builtin_conversation`).
- Profile-driven `metadata_mapping`, `tag_rules`, and `entity_extraction` flags on retain.

[Unreleased]: https://github.com/AstrocyteAI/astrocyte/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/AstrocyteAI/astrocyte/releases/tag/v0.6.0
