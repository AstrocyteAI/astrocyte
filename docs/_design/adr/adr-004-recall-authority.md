# ADR-004: Structured recall authority (M7)

## Status

Accepted — implemented in `astrocyte.recall.authority` with `RecallAuthorityConfig` / `RecallResult.authority_context`.

## Context

Fused recall (semantic + graph + keyword) returns a single ranked list. Downstream synthesis benefits from **explicit precedence in the prompt**: which hits are canonical vs advisory, and what rules the model must follow. This is distinct from **tiered retrieval** (cost/latency tiers), which remains in `tiered_retrieval`.

## Decision

1. **Configuration** (`AstrocyteConfig.recall_authority`):
   - `enabled`, optional `rules_inline` or `rules_path` (UTF-8 file; `rules_inline` wins when both are set at format time).
   - `tiers`: ordered bands `{ id, priority, label }`. Lower `priority` values appear first in the formatted block.
   - `apply_to_reflect` (default **true**): when enabled, pipeline / `Astrocyte.reflect` **prepends** the formatted authority block to the synthesis user message inside `<authority_context>` (before `<memories>`). Set to **false** to keep `authority_context` only on `RecallResult` for callers that merge prompts themselves.
   - `tier_by_bank`: optional map `bank_id → tier id` used at **retain** time to set `metadata["authority_tier"]` on stored vectors. **`extraction_profiles.*.authority_tier`** overrides this when the retain request names a profile.

2. **Hit labeling**: Producers attach `metadata["authority_tier"]` on `MemoryHit` (string matching `tier.id`). Unmatched hits are listed under `[UNASSIGNED]`.

3. **Runtime — recall**: After DLP scanning on `Astrocyte.recall`, when enabled, `apply_recall_authority` sets `RecallResult.authority_context`.

4. **Runtime — reflect**: Tier-1 `PipelineOrchestrator.reflect` and multi-bank `Astrocyte.reflect` apply the same formatter when `apply_to_reflect` is true and pass the string into `synthesize`. **`ReflectResult.authority_context`** echoes that block for tools and UIs. Tier-2 engine **native** `reflect` is unchanged (no automatic injection); use pipeline reflect or keyword-only flows if you need authority in synthesis.

## Consequences

- **Phase 1 (current)**: One fused `RecallResult`; authority only **formats** and labels hits already present.
- **Future**: Per-tier retrieval or multi-query recall can populate tiers without changing the formatter contract, as long as hits carry `authority_tier`.

### Token budget

`authority_context` adds **prompt tokens** (tier labels, rules text, and per-hit lines). Size scales with hit count and label verbosity. Combine with **`homeostasis`** limits, **`reflect_max_tokens`**, and recall **`detail_level`** so synthesis stays within budget; there is no separate automatic truncation of the authority block today.

## Related

- `docs/_design/built-in-pipeline.md` — retrieval strategies.
- `docs/_design/product-roadmap-v1.md` — M7.
