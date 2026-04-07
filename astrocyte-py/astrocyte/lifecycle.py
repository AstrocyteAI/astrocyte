"""Memory lifecycle management — TTL, legal hold, compliance forget.

See docs/_design/memory-lifecycle.md for the design specification.
All policy functions are sync (Rust migration candidates).
"""

from __future__ import annotations

from datetime import datetime, timezone

from astrocyte.config import LifecycleConfig
from astrocyte.errors import LegalHoldActive
from astrocyte.types import LegalHold, LifecycleAction


class LifecycleManager:
    """Manages memory lifecycle: TTL evaluation and legal holds.

    Legal holds are stored in-memory. Persistence is out of scope for v1.
    TTL evaluation is a pure sync function — no I/O, Rust-portable.
    """

    def __init__(self, config: LifecycleConfig) -> None:
        self._config = config
        self._holds: dict[str, LegalHold] = {}  # key: "{bank_id}\x00{hold_id}"

    # ── Legal holds (sync) ──

    _SEP = "\x00"  # Null byte separator — cannot appear in bank_id or hold_id strings

    def set_legal_hold(self, bank_id: str, hold_id: str, reason: str, *, set_by: str = "user:api") -> LegalHold:
        """Place a bank under legal hold."""
        key = f"{bank_id}{self._SEP}{hold_id}"
        hold = LegalHold(
            hold_id=hold_id,
            bank_id=bank_id,
            reason=reason,
            set_at=datetime.now(timezone.utc),
            set_by=set_by,
        )
        self._holds[key] = hold
        return hold

    def release_legal_hold(self, bank_id: str, hold_id: str) -> bool:
        """Release a legal hold. Returns True if hold existed."""
        key = f"{bank_id}{self._SEP}{hold_id}"
        return self._holds.pop(key, None) is not None

    def is_under_hold(self, bank_id: str) -> bool:
        """Check if any legal hold is active on this bank."""
        prefix = f"{bank_id}{self._SEP}"
        return any(k.startswith(prefix) for k in self._holds)

    def get_holds(self, bank_id: str) -> list[LegalHold]:
        """Get all active holds for a bank."""
        prefix = f"{bank_id}{self._SEP}"
        return [h for k, h in self._holds.items() if k.startswith(prefix)]

    def check_forget_allowed(self, bank_id: str) -> None:
        """Raise LegalHoldActive if bank is under hold."""
        holds = self.get_holds(bank_id)
        if holds:
            raise LegalHoldActive(bank_id=bank_id, hold_id=holds[0].hold_id)

    # ── TTL evaluation (sync, pure) ──

    def evaluate_memory_ttl(
        self,
        memory_id: str,
        bank_id: str,
        created_at: datetime | None,
        last_recalled_at: datetime | None,
        tags: list[str] | None,
        fact_type: str | None,
        now: datetime,
    ) -> LifecycleAction:
        """Evaluate TTL policy for a single memory. Sync, pure function.

        Returns LifecycleAction with action="archive"|"delete"|"keep".
        """
        if not self._config.enabled:
            return LifecycleAction(memory_id=memory_id, action="keep", reason="lifecycle_disabled")

        # Legal hold blocks all lifecycle actions
        if self.is_under_hold(bank_id):
            return LifecycleAction(memory_id=memory_id, action="keep", reason="legal_hold")

        ttl = self._config.ttl

        # Check exempt tags
        if tags and ttl.exempt_tags:
            if any(t in ttl.exempt_tags for t in tags):
                return LifecycleAction(memory_id=memory_id, action="keep", reason="exempt")

        # Determine archive threshold (may be overridden by fact_type)
        archive_days = ttl.archive_after_days
        if fact_type and ttl.fact_type_overrides:
            override = ttl.fact_type_overrides.get(fact_type)
            if override is not None:
                archive_days = override

        # Check delete threshold (based on creation date)
        if created_at:
            age_days = (now - created_at).days
            if age_days >= ttl.delete_after_days:
                return LifecycleAction(memory_id=memory_id, action="delete", reason="ttl_expired")

        # Check archive threshold (based on last recall or creation)
        reference_date = last_recalled_at or created_at
        if reference_date:
            days_since = (now - reference_date).days
            if days_since >= archive_days:
                return LifecycleAction(memory_id=memory_id, action="archive", reason="ttl_unretrieved")

        return LifecycleAction(memory_id=memory_id, action="keep", reason="recent")
