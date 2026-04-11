# ADR-004: Structured recall authority (M7)

## Status

Accepted — implemented in `astrocyte.recall.authority` with `RecallAuthorityConfig` / `RecallResult.authority_context`.

## Context

Fused recall (semantic + graph + keyword) returns a single ranked list. Downstream synthesis benefits from **explicit precedence in the prompt**: which hits are canonical vs advisory, and what rules the model must follow. This is distinct from **tiered retrieval** (cost/latency tiers), which remains in `tiered_retrieval`.

## Decision

1. **Configuration** (`AstrocyteConfig.recall_authority`):
   - `enabled`, optional `rules_inline` or `rules_path` (UTF-8 file; `rules_inline` wins when both are set at format time).
   - `tiers`: ordered bands `{ id, priority, label }`. Lower `priority` values appear first in the formatted block.

2. **Hit labeling**: Producers attach `metadata["authority_tier"]` on `MemoryHit` (string matching `tier.id`). Unmatched hits are listed under `[UNASSIGNED]`.

3. **Runtime**: After DLP scanning on `Astrocyte.recall`, when enabled, `apply_recall_authority` sets `RecallResult.authority_context` to a single string suitable for injection next to raw hits.

## Consequences

- **Phase 1 (current)**: One fused `RecallResult`; authority only **formats** and labels hits already present.
- **Future**: Per-tier retrieval or multi-query recall can populate tiers without changing the formatter contract, as long as hits carry `authority_tier`.

## Related

- `docs/_design/built-in-pipeline.md` — retrieval strategies.
- `docs/_design/product-roadmap-v1.md` — M7.
