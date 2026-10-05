"""Persisted legal holds (team-memory.md §8, G4): a hold placed through
``place_legal_hold`` lives in the store, so it survives a restart and binds
every process sharing the store. Two ``Astrocyte`` instances over one store
stand in for two gateway replicas; a new instance for a restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import LegalHoldActive
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import AstrocyteContext, VectorItem

BANK = "project:a"
DPO = AstrocyteContext(principal="user:dpo")


def _replica(store: InMemoryVectorStore, *, lifecycle: bool = False) -> Astrocyte:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    if lifecycle:
        config.lifecycle.enabled = True
        config.lifecycle.ttl.delete_after_days = 30
    brain = Astrocyte(config)
    brain.set_pipeline(PipelineOrchestrator(vector_store=store, llm_provider=MockLLMProvider()))
    return brain


async def _save(store: InMemoryVectorStore, mid: str, *, days_old: int = 0) -> None:
    created = datetime.now(UTC) - timedelta(days=days_old)
    await store.store_vectors([VectorItem(id=mid, bank_id=BANK, vector=[0.1] * 128, text=f"memory {mid}",
                                          metadata={"_created_at": created.isoformat()}, retained_at=created)])


async def test_a_hold_placed_on_one_replica_blocks_forget_on_another():
    store = InMemoryVectorStore()
    a, b = _replica(store), _replica(store)
    await _save(store, "m1")
    await a.place_legal_hold(BANK, "case-7", "pending litigation", set_by="user:counsel")
    with pytest.raises(LegalHoldActive):
        await b.forget(BANK, memory_ids=["m1"])
    assert await b.lift_legal_hold(BANK, "case-7") is True
    assert (await a.forget(BANK, memory_ids=["m1"])).deleted_count == 1, "lifted everywhere"


async def test_a_hold_survives_a_restart_and_is_listed():
    store = InMemoryVectorStore()
    await _replica(store).place_legal_hold(BANK, "case-7", "pending litigation", set_by="user:counsel")
    [hold] = await _replica(store).legal_holds(BANK)
    assert (hold.hold_id, hold.reason, hold.set_by) == ("case-7", "pending litigation", "user:counsel")
    assert await _replica(store).legal_holds("project:other") == []


async def test_a_right_to_erasure_is_not_blocked():
    store = InMemoryVectorStore()
    a, b = _replica(store), _replica(store)
    await _save(store, "m1")
    await a.place_legal_hold(BANK, "case-7", "pending litigation")
    assert (await b.forget(BANK, memory_ids=["m1"], compliance=True, context=DPO)).deleted_count == 1


async def test_lifecycle_deletion_waits_for_the_hold():
    store = InMemoryVectorStore()
    await _save(store, "old", days_old=90)
    await _replica(store).place_legal_hold(BANK, "case-7", "pending litigation")
    held = await _replica(store, lifecycle=True).run_lifecycle(BANK)
    assert held.deleted_count == 0 and {a.reason for a in held.actions} == {"legal_hold"}
    await _replica(store).lift_legal_hold(BANK, "case-7")
    assert (await _replica(store, lifecycle=True).run_lifecycle(BANK)).deleted_count == 1


async def test_lifting_an_unknown_hold_says_so():
    assert await _replica(InMemoryVectorStore()).lift_legal_hold(BANK, "nope") is False


async def test_the_in_process_api_still_works_alongside():
    """set_legal_hold keeps a hold in this process only, as before."""
    store = InMemoryVectorStore()
    a, b = _replica(store), _replica(store)
    await _save(store, "m1")
    a.set_legal_hold(BANK, "local-1", "this process only")
    with pytest.raises(LegalHoldActive):
        await a.forget(BANK, memory_ids=["m1"])
    assert (await b.forget(BANK, memory_ids=["m1"])).deleted_count == 1, "another process doesn't see it"
