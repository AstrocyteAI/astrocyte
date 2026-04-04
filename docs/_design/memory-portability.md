# Memory portability

Astrocyte owns the DTO layer across all providers. This means it can define a **portable memory format** that enables migration between providers without data loss. No other tool in the market offers this.

---

## 1. The problem

Users who start with Mem0 and want to upgrade to Mystique face a wall: memories are locked in the provider's internal format. There's no standard way to export from one system and import into another.

This lock-in discourages adoption ("what if I want to switch later?") and hurts the ecosystem. Astrocyte breaks this lock-in.

---

## 2. Portable memory format

### 2.1 Format: Astrocyte Memory Archive (AMA)

A newline-delimited JSON (JSONL) file where each line is one memory unit, plus a header line for metadata.

```jsonl
{"_ama_version": 1, "bank_id": "user-123", "exported_at": "2026-04-03T12:00:00Z", "provider": "mem0", "memory_count": 42}
{"id": "mem_001", "text": "Calvin prefers dark mode", "fact_type": "experience", "score": null, "tags": ["preference"], "metadata": {"source": "chat"}, "occurred_at": "2026-03-15T10:30:00Z", "created_at": "2026-03-15T10:30:05Z"}
{"id": "mem_002", "text": "The deployment pipeline uses GitHub Actions", "fact_type": "world", "score": null, "tags": ["technical"], "metadata": {"source": "onboarding"}, "occurred_at": "2026-03-10T09:00:00Z", "created_at": "2026-03-10T09:00:12Z"}
```

### 2.2 Why JSONL

- Streamable: no need to load the entire file into memory
- Line-oriented: easy to process with standard Unix tools
- Self-describing: each line is a valid JSON object
- Portable DTOs: aligns with the Python/Rust drop-in contract (see `implementation-language-strategy.md`)

### 2.3 Schema

Each memory line contains:

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | Yes | Original memory ID (for dedup on import) |
| `text` | string | Yes | The memory content |
| `fact_type` | string | No | "world", "experience", "observation" |
| `tags` | list[string] | No | Tags |
| `metadata` | dict | No | Caller-defined metadata |
| `occurred_at` | ISO 8601 | No | When the event happened |
| `created_at` | ISO 8601 | No | When the memory was stored |
| `source` | string | No | Origin system |
| `entities` | list[EntityRef] | No | Extracted entities (if available) |
| `embedding` | list[float] | No | Vector embedding (if available, provider-dependent) |

**EntityRef:**

| Field | Type | Description |
|---|---|---|
| `name` | string | Entity name |
| `entity_type` | string | PERSON, ORG, LOCATION, etc. |
| `aliases` | list[string] | Known aliases |

### 2.4 What is NOT included

- Provider-specific internal IDs (graph link IDs, internal sequence numbers)
- Provider-specific scoring or ranking metadata
- Consolidation state (observations are exported as memories with `fact_type: "observation"`)
- Bank configuration (exported separately, see below)

### 2.5 Relationship to memory export sinks

AMA export/import is for **portability and migration** between providers. **Ongoing** replication of memory lifecycle into a **data warehouse** or **lakehouse** uses **`MemoryExportSink`** ([`memory-export-sink.md`](./memory-export-sink.md)) or, until core wiring ships, **`event-hooks.md`** toward an ingestor. AMA snapshots can still be **scheduled** into object storage for cold backups; sinks add **row/event granularity** and SQL-curated schemas for downstream analytics.

---

## 3. API surface

### 3.1 Export

```python
await brain.export_bank(
    bank_id="user-123",
    path="./user-123-backup.ama.jsonl",
    include_embeddings=False,       # Embeddings are provider/model-specific
    include_entities=True,
)
```

### 3.2 Import

```python
await brain.import_bank(
    bank_id="user-123",              # Target bank (may differ from source)
    path="./user-123-backup.ama.jsonl",
    on_conflict="skip",              # "skip" | "overwrite" | "error"
    re_embed=True,                   # Re-generate embeddings for new provider
    re_extract_entities=False,       # Use exported entities if available
)
```

### 3.3 Bank configuration export

Separate from memory data, bank settings can be exported:

```python
await brain.export_bank_config(
    bank_id="user-123",
    path="./user-123-config.yaml",
)
```

Produces:

```yaml
bank_id: user-123
profile: personal
dispositions:
  skepticism: 3
  literalism: 3
  empathy: 4
tags: [personal]
created_at: "2026-01-15T00:00:00Z"
```

---

## 4. Migration workflow

### 4.1 Provider switch (e.g., Mem0 → Mystique)

```bash
# 1. Export from current provider
astrocyte export --bank user-123 --output ./backup.ama.jsonl

# 2. Update config to new provider
# Edit astrocyte.yaml: provider: mem0 → provider: mystique

# 3. Import into new provider
astrocyte import --bank user-123 --input ./backup.ama.jsonl --re-embed
```

### 4.2 Tier switch (e.g., Tier 1 pgvector → Tier 2 Mystique)

Same workflow. The AMA format is tier-agnostic.

### 4.3 Bulk migration

```python
banks = await brain.list_banks()
for bank in banks:
    await brain.export_bank(bank.id, path=f"./backup/{bank.id}.ama.jsonl")

# Switch provider in config...

for bank in banks:
    await brain.import_bank(bank.id, path=f"./backup/{bank.id}.ama.jsonl", re_embed=True)
```

---

## 5. Embedding portability

Embeddings are **not portable** across providers or models. An embedding generated by `text-embedding-3-small` is meaningless to a provider using `voyage-3`.

Options on import:

| Option | Behavior | Cost |
|---|---|---|
| `re_embed=True` | Discard exported embeddings, regenerate with new provider/model | LLM API cost for embedding every memory |
| `re_embed=False` | Use exported embeddings as-is (only valid if same model) | Free, but risks incompatible embeddings |
| Embeddings not in export | Always regenerated on import | LLM API cost |

**Default:** `include_embeddings=False` on export, `re_embed=True` on import. This is safest.

---

## 6. Limitations

- **Provider-specific features are lost.** Mystique's internal entity links, spreading activation weights, and observation hierarchies are not captured in the AMA format. After import, the new provider rebuilds these from the raw memories.
- **Consolidation state resets.** Observations are preserved as memories, but the relationships between observations and source facts are not. The new provider will re-consolidate over time.
- **Import is not instant.** Re-embedding and re-extracting entities can take significant time for large banks. Provide progress callbacks.

---

## 7. CLI support

```
astrocyte export --bank <bank_id> [--output <path>] [--include-embeddings] [--include-entities]
astrocyte import --bank <bank_id> --input <path> [--on-conflict skip|overwrite|error] [--re-embed] [--re-extract-entities]
astrocyte migrate --from-config <old.yaml> --to-config <new.yaml> [--banks <bank1,bank2,...>]
```

The `migrate` command combines export + config switch + import in one step.
