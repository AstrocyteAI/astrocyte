"""Tests for memory lifecycle management — TTL, legal hold."""

from datetime import datetime, timedelta, timezone

import pytest

from astrocyte.config import LifecycleConfig, LifecycleTtlConfig
from astrocyte.errors import LegalHoldActive
from astrocyte.lifecycle import LifecycleManager


@pytest.fixture
def config() -> LifecycleConfig:
    return LifecycleConfig(
        enabled=True,
        ttl=LifecycleTtlConfig(archive_after_days=90, delete_after_days=365),
    )


@pytest.fixture
def manager(config: LifecycleConfig) -> LifecycleManager:
    return LifecycleManager(config)


class TestLegalHold:
    def test_set_legal_hold(self, manager: LifecycleManager) -> None:
        hold = manager.set_legal_hold("bank-1", "hold-abc", "litigation")
        assert hold.hold_id == "hold-abc"
        assert hold.bank_id == "bank-1"
        assert hold.reason == "litigation"
        assert hold.set_by == "user:api"

    def test_is_under_hold(self, manager: LifecycleManager) -> None:
        assert manager.is_under_hold("bank-1") is False
        manager.set_legal_hold("bank-1", "hold-1", "audit")
        assert manager.is_under_hold("bank-1") is True
        assert manager.is_under_hold("bank-2") is False

    def test_release_legal_hold(self, manager: LifecycleManager) -> None:
        manager.set_legal_hold("bank-1", "hold-1", "audit")
        assert manager.release_legal_hold("bank-1", "hold-1") is True
        assert manager.is_under_hold("bank-1") is False

    def test_release_nonexistent_hold(self, manager: LifecycleManager) -> None:
        assert manager.release_legal_hold("bank-1", "nonexistent") is False

    def test_get_holds(self, manager: LifecycleManager) -> None:
        manager.set_legal_hold("bank-1", "hold-1", "litigation")
        manager.set_legal_hold("bank-1", "hold-2", "audit")
        manager.set_legal_hold("bank-2", "hold-3", "other")

        holds = manager.get_holds("bank-1")
        assert len(holds) == 2
        assert {h.hold_id for h in holds} == {"hold-1", "hold-2"}

    def test_check_forget_allowed_raises_when_held(self, manager: LifecycleManager) -> None:
        manager.set_legal_hold("bank-1", "hold-1", "litigation")
        with pytest.raises(LegalHoldActive) as exc_info:
            manager.check_forget_allowed("bank-1")
        assert exc_info.value.bank_id == "bank-1"

    def test_check_forget_allowed_passes_when_not_held(self, manager: LifecycleManager) -> None:
        manager.check_forget_allowed("bank-1")  # Should not raise

    def test_multiple_holds_require_all_released(self, manager: LifecycleManager) -> None:
        manager.set_legal_hold("bank-1", "hold-1", "litigation")
        manager.set_legal_hold("bank-1", "hold-2", "audit")
        manager.release_legal_hold("bank-1", "hold-1")
        assert manager.is_under_hold("bank-1") is True  # hold-2 still active


class TestTtlEvaluation:
    def test_keep_recent_memory(self, manager: LifecycleManager) -> None:
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=10),
            last_recalled_at=now - timedelta(days=5),
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "keep"
        assert action.reason == "recent"

    def test_archive_unretrieved_memory(self, manager: LifecycleManager) -> None:
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=100),
            last_recalled_at=None,
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "archive"
        assert action.reason == "ttl_unretrieved"

    def test_delete_old_memory(self, manager: LifecycleManager) -> None:
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=400),
            last_recalled_at=None,
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "delete"
        assert action.reason == "ttl_expired"

    def test_exempt_tags_prevent_ttl(self, manager: LifecycleManager) -> None:
        manager._config.ttl.exempt_tags = ["pinned", "compliance"]
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=400),
            last_recalled_at=None,
            tags=["pinned"],
            fact_type=None,
            now=now,
        )
        assert action.action == "keep"
        assert action.reason == "exempt"

    def test_fact_type_override(self, manager: LifecycleManager) -> None:
        manager._config.ttl.fact_type_overrides = {"experience": 30}
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=50),
            last_recalled_at=now - timedelta(days=35),
            tags=None,
            fact_type="experience",
            now=now,
        )
        assert action.action == "archive"  # 35 days > 30 days override

    def test_legal_hold_blocks_ttl(self, manager: LifecycleManager) -> None:
        manager.set_legal_hold("test", "hold-1", "litigation")
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=400),
            last_recalled_at=None,
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "keep"
        assert action.reason == "legal_hold"

    def test_disabled_lifecycle_keeps_all(self) -> None:
        config = LifecycleConfig(enabled=False)
        manager = LifecycleManager(config)
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=999),
            last_recalled_at=None,
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "keep"
        assert action.reason == "lifecycle_disabled"

    def test_archive_by_creation_when_never_recalled(self, manager: LifecycleManager) -> None:
        """When last_recalled_at is None, use created_at for archive threshold."""
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=91),
            last_recalled_at=None,
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "archive"

    def test_recently_recalled_prevents_archive(self, manager: LifecycleManager) -> None:
        """Recent recall resets the archive timer."""
        now = datetime.now(timezone.utc)
        action = manager.evaluate_memory_ttl(
            memory_id="m1",
            bank_id="test",
            created_at=now - timedelta(days=200),
            last_recalled_at=now - timedelta(days=10),
            tags=None,
            fact_type=None,
            now=now,
        )
        assert action.action == "keep"
