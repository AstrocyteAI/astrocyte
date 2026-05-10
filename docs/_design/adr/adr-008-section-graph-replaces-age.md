# ADR-008: Apache AGE removed; flat tables + SQL CTEs replace graph store

## Status

Accepted — implemented in M9 (`recall.md` §4.2, §8.2).

## Context

M5 introduced Apache AGE as the graph store backend, with per-(bank, label) partial indexes and a `_ensure_label` SAVEPOINT-guarded creation pattern (Hindsight d5e6f7a8b9c0). AGE provided:

- Entity nodes + co-occurrence edges via Cypher
- Spreading activation traversals
- Causal link queries
- Semantic kNN graph
- Multi-hop entity bridging

[Hindsight](https://github.com/Hindsight-AI/hindsight) demonstrated equivalent functionality (and better latency) using **flat tables + SQL CTEs**:

- `memory_links` table with a `link_type` discriminator
- `unit_entities` table with `(entity_name)` btree
- LATERAL-capped CTEs for entity expansion (sub-100ms measured)
- RRF fusion across multiple link types
- Per-bank partial indexes are first-class (no per-label OID trick needed)

M9 builds the same pattern at section grain: `section_links` and `section_entities`.

## Decision

**Apache AGE is removed entirely from Astrocyte. No deprecation period.**

1. `graph_store: age` raises `ConfigError` on startup with a pointer to this ADR.
2. AGE-related code, tests, configs, and dependencies are deleted.
3. `DROP EXTENSION IF EXISTS age CASCADE;` ships in M9's first migration.
4. **No migration tool**: operators with AGE-backed banks rebuild from raw `memory_units`. Re-extraction is one bench-style retain run; the result is cleaner than walking AGE's per-label OID structure (it uses the current entity extractor, not whatever version wrote the original AGE rows).

Replacement at both grains:
- **Section grain**: `section_links` + `section_entities` flat tables (M9, this ADR's primary motivator) — what recall reads.
- **Memory-unit grain**: `unit_links` + `unit_entities` flat tables — added as part of M9 for callers that still need fact-grain graph operations (lifecycle, forget, audit).

## Alternatives considered

### Alt 1: Soft deprecation with 1-release window
Mark AGE as deprecated, raise a warning at startup, support both backends for one minor release.

**Rejected** because:
- Pre-1.0 framework — breaking changes are acceptable.
- The half-state (both backends supported) doubles maintenance cost: every graph-touching code path needs two implementations and two test surfaces.
- AGE's value to Astrocyte's actual workloads is zero (we don't use recursive Cypher or graph analytics); the only reason to keep it is avoiding rebuild cost for existing deployments — which is small (one retain run).

### Alt 2: Migration tool (AGE rows → flat-table rows)
Provide `age_to_flat.py` that walks AGE entity nodes + edges and writes equivalent rows to `unit_entities` + `unit_links`.

**Rejected** because:
- Re-extraction from raw `memory_units` is simpler — it runs the current entity extractor instead of preserving whatever shape the AGE rows have (which may be from a stale version).
- AGE's per-label OID structure means migration code must walk N tables (one per (bank, label) tuple) — non-trivial and brittle across AGE versions.
- For the small set of users on AGE-backed banks, "rebuild your bank from raw memories" is one CLI command and produces a cleaner result.

### Alt 3: Keep AGE as an optional analytics-only backend
Remove from the recall path but keep as an optional graph-analytics surface (PageRank, communities) for future use cases.

**Rejected** because:
- We don't expose graph analytics today and have no roadmap for them.
- Speculative retention adds operational burden (extension upgrades, version compatibility) for a hypothetical future user.
- Re-add AGE as an optional analytics layer when the use case materializes; YAGNI applies.

## Consequences

### Positive
- One fewer Postgres extension. Smaller deployment surface; one fewer thing to upgrade across major Postgres versions.
- All graph queries are plain SQL — no AGE/SQL boundary, faster planning.
- Per-bank partial indexes are first-class on flat tables; no `_ensure_label` SAVEPOINT machinery needed.
- Cypher knowledge no longer required to operate Astrocyte.
- A class of deadlock bugs (AGE writes coordinated with SQL transactions) goes away.

### Negative
- **Breaking change** for AGE-backed deployments. Operators must rebuild banks from raw `memory_units` before upgrading.
- Loss of recursive Cypher for 3+ hop traversals. We don't use these today; if needed in future, Postgres `WITH RECURSIVE` covers it.
- Loss of AGE's built-in graph analytics (PageRank, communities). Not on roadmap.

### Operational
- M9 first migration includes `DROP EXTENSION IF EXISTS age CASCADE;`. Deployments without AGE installed are unaffected.
- AGE-backed deployments must run a rebuild before upgrade:
  ```bash
  astrocyte-cli rebuild --bank-id <UUID>
  ```
  This re-runs entity extraction over raw memory_units and writes to the new flat-table backend. Rebuild cost ≈ original retain cost.
- Docker images and Helm charts drop AGE installation steps.
- `pyproject.toml` / `uv.lock` drop AGE-related Python deps.
- All `benchmarks/config-*.yaml` files drop `graph_store: age` lines.

### Verification
M9 PR1 includes a verification check:
```bash
grep -rl "apache.?age\|graph_store.*age\|from astrocyte.*age" \
  --include="*.py" --include="*.md" --include="*.yaml" \
  /Users/calvin/AstrocyteAI/astrocyte | wc -l
# Must return 0 (modulo this ADR + CHANGELOG entry)
```

## See also

- [recall.md](../recall.md) — §4.2 replacement schema, §8.2 rationale
- [adr-006-three-layer-recall-stack.md](adr-006-three-layer-recall-stack.md) — three-layer architecture
- [hindsight-informed-capabilities.md](../hindsight-informed-capabilities.md) — flat-table CTE pattern origin
- [storage-and-data-planes.md](../storage-and-data-planes.md) — updated DDL for `section_links`, `unit_links`

## References

- Hindsight `link_expansion_retrieval.py` — the canonical implementation of the flat-table CTE pattern we're adopting
- Hindsight `ops_postgresql.py:351` — `build_entity_expansion_cte` — the LATERAL-capped per-entity expansion
