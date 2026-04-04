# Memory lifecycle management

Memories are not permanent. They have a lifecycle: creation, active use, consolidation, archival, and deletion. Astrocytes manages this lifecycle with policy-driven automation and compliance-grade controls.

This maps to **Principle 7 (Pruning / phagocytosis)** - structured forgetting is a first-class concern, not an afterthought.

---

## 1. Lifecycle states

```
Created → Active → Consolidated → Archived → Deleted
   │         │          │             │
   │         │          │             └── TTL expiry, compliance purge, manual
   │         │          └── Merged into observations, source facts optionally archived
   │         └── Being recalled, contributing to reflects
   └── Just stored, not yet recalled
```

### 1.1 State definitions

| State | Description | Storage |
|---|---|---|
| **Active** | Recently created or recently recalled. Fully indexed and searchable. | Hot storage (primary vector/graph store) |
| **Consolidated** | Merged into higher-order observations. Source facts may remain active or be archived. | Hot storage (observations active, source facts optionally archived) |
| **Archived** | No longer indexed for search. Retained for audit, compliance, or potential reactivation. | Cold storage (if supported by provider) |
| **Deleted** | Permanently removed. Irreversible. | Removed from all stores |

---

## 2. TTL policies

Time-to-live policies automatically transition memories through lifecycle states.

```yaml
lifecycle:
  ttl:
    # Archive memories that haven't been recalled in 90 days
    archive_unretrieved_after_days: 90

    # Delete archived memories after 365 days
    delete_archived_after_days: 365

    # Never auto-delete memories with these tags (compliance hold)
    exempt_tags: ["legal_hold", "compliance"]

    # Per-fact-type TTLs (override global)
    fact_type_overrides:
      observation: null              # Observations never expire
      experience: 180               # Experiences archive after 180 days
      world: 365                    # World facts archive after 365 days
```

### 2.1 Recall freshness tracking

The pipeline tracks when each memory was last recalled (via metadata). Memories that are never recalled are candidates for archival. Memories that are frequently recalled stay active indefinitely.

```python
# Internal metadata updated on every recall hit
{
  "_last_recalled_at": "2026-04-01T10:00:00Z",
  "_recall_count": 7,
  "_created_at": "2026-01-15T10:30:00Z",
}
```

---

## 3. Compliance operations

### 3.1 Right to forget (GDPR, PDPA, CCPA)

```python
# Delete all memories for a user across all banks
await brain.forget(
    selector=ForgetSelector(
        bank_ids=["user-123"],
        scope="all",                     # All memories in these banks
    ),
    compliance=True,                     # Emit audit event, ensure permanent deletion
)
```

When `compliance=True`:
- All memories are **permanently deleted** (not archived)
- Embeddings are removed from vector stores
- Entity references are cleaned up in graph stores
- An **audit event** is emitted (see section 5)
- The operation is **idempotent** (safe to retry)

### 3.2 Selective forget

```python
# Forget specific memories
await brain.forget(
    selector=ForgetSelector(
        bank_ids=["user-123"],
        tags=["sensitive"],              # Only memories with this tag
        before_date=datetime(2026, 1, 1), # Only memories before this date
    ),
)
```

### 3.3 Legal hold

Memories under legal hold are exempt from TTL expiry and cannot be deleted via normal `forget()` calls:

```python
# Place a bank under legal hold
await brain.set_legal_hold(
    bank_id="user-123",
    hold_id="case-2026-001",
    reason="Litigation hold per legal request #LH-2026-001",
)

# Release hold
await brain.release_legal_hold(
    bank_id="user-123",
    hold_id="case-2026-001",
)
```

While under hold:
- TTL policies are suspended for the bank
- `forget()` calls return `LegalHoldActive` error
- Retain and recall continue normally
- The hold is logged in the audit trail

---

## 4. Consolidation lifecycle

Consolidation (see `built-in-pipeline.md` section 5) creates observations from raw facts. The lifecycle integration:

1. **Pre-consolidation**: raw facts are `Active`
2. **Consolidation runs**: observations are created from clusters of related facts
3. **Post-consolidation**: observations are `Active`, source facts can be:
   - Kept active (default) - both observation and source facts are searchable
   - Archived - source facts move to cold storage, observations remain searchable
   - Deleted - source facts removed, observations are the only record

```yaml
lifecycle:
  consolidation:
    source_fact_policy: keep_active     # "keep_active" | "archive" | "delete"
    min_facts_for_consolidation: 5      # Don't consolidate until N related facts exist
    consolidation_schedule: "0 3 * * *" # Cron: 3am daily
```

---

## 5. Audit trail

All lifecycle transitions are recorded as audit events.

```python
@dataclass
class AuditEvent:
    event_type: str                      # "created", "recalled", "archived", "deleted", "legal_hold", etc.
    bank_id: str
    memory_ids: list[str] | None
    actor: str                           # "system:ttl", "system:consolidation", "user:api", "compliance:forget"
    reason: str | None
    timestamp: datetime
    metadata: dict[str, Any] | None      # Additional context
```

### 5.1 Audit event types

| Event | Actor | Description |
|---|---|---|
| `memory.created` | `user:api` | Memory stored via retain |
| `memory.recalled` | `user:api` | Memory included in recall results |
| `memory.archived` | `system:ttl` | TTL policy moved memory to archive |
| `memory.deleted` | `system:ttl` | TTL policy permanently deleted memory |
| `memory.deleted` | `compliance:forget` | Compliance purge deleted memory |
| `memory.consolidated` | `system:consolidation` | Memories merged into observation |
| `bank.legal_hold.set` | `user:api` | Legal hold placed on bank |
| `bank.legal_hold.released` | `user:api` | Legal hold released |
| `bank.created` | `user:api` or `system:template` | New bank created |
| `bank.deleted` | `user:api` | Bank and all memories deleted |

### 5.2 Audit storage

Audit events are:
- Emitted as **OTel span events** (see `policy-layer.md`)
- Optionally written to an **audit log** (file, database, or external system via hooks - see `event-hooks.md`)
- **Never stored in the memory provider itself** (the audit trail must survive provider changes)

```yaml
lifecycle:
  audit:
    enabled: true
    sink: file                           # "file" | "webhook" | "otel_only"
    file_path: ./audit/astrocytes.audit.jsonl
    retention_days: 2555                 # 7 years for compliance
```

---

## 6. Lifecycle execution

### 6.1 Scheduled (background)

TTL checks and consolidation run on a schedule:

```yaml
lifecycle:
  scheduler:
    enabled: true
    ttl_check_schedule: "0 2 * * *"      # 2am daily
    consolidation_schedule: "0 3 * * *"  # 3am daily
```

### 6.2 On-demand

```python
# Manually trigger TTL check
await brain.run_ttl_check(bank_id="user-123")

# Manually trigger consolidation
await brain.run_consolidation(bank_id="user-123")
```

### 6.3 Provider interaction

- **Tier 1 (Storage)**: Astrocytes orchestrates lifecycle by calling retrieval SPI methods (delete, update metadata).
- **Tier 2 (Memory Engine)**: Astrocytes delegates lifecycle to the memory engine if it supports it (`supports_consolidation` in capabilities). Falls back to Astrocytes-managed lifecycle if not.
