"""Erasure (team-memory.md §8, G4): ``Astrocyte.erase`` and ``forget_principal``.

A forget is a soft delete on the SQL stores; erase removes a forgotten
memory from storage for good and keeps only its tombstone. The store side
(SQLite, Postgres, parity) is covered in the adapters' suites; these pin the
core's contract on the in-memory store.
"""

from __future__ import annotations

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import AccessGrant, AstrocyteContext, SyncPushRecord

ALICE = AstrocyteContext(principal="user:alice")
BOB = AstrocyteContext(principal="user:bob")
ADMIN = AstrocyteContext(principal="user:dpo")


class ListingStore(InMemoryVectorStore):
    """The in-memory store plus ``list_banks``, as the SQL stores have it, so
    the sweep finds banks that aren't configured (team ``project:*`` banks)."""

    async def list_banks(self) -> list[tuple[str, int, None]]:
        banks: dict[str, int] = {}
        for item in self._vectors.values():
            banks[item.bank_id] = banks.get(item.bank_id, 0) + 1
        return [(b, n, None) for b, n in sorted(banks.items())]


def _brain(acl: bool = False) -> Astrocyte:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    if acl:
        config.access_control.enabled = True
        config.access_control.default_policy = "deny"
    brain = Astrocyte(config)
    brain.set_pipeline(PipelineOrchestrator(vector_store=ListingStore(), llm_provider=MockLLMProvider()))
    if acl:
        brain.set_access_grants([
            AccessGrant(bank_id="project:*", principal="user:alice", permissions=["read", "write"]),
            AccessGrant(bank_id="project:*", principal="user:dpo", permissions=["read", "write", "forget", "admin"]),
        ])
    return brain


def _rec(mid: str, text: str, **kw) -> SyncPushRecord:
    return SyncPushRecord(id=mid, text=text, **kw)


async def _live(brain: Astrocyte, bank: str) -> set[str]:
    return {i.id for i in await brain._pipeline.vector_store.list_vectors(bank, limit=1000)}


class TestErase:
    async def test_erases_only_what_was_forgotten(self):
        brain = _brain()
        await brain.push_records("project:a", [_rec("aaaaaaaa1", "Deploys on Tuesdays."),
                                               _rec("aaaaaaaa2", "Staging at s.test.")], context=ALICE)
        assert await brain.erase("project:a", ["aaaaaaaa1"], context=ALICE) == 0, "live: forget first"
        await brain.forget("project:a", memory_ids=["aaaaaaaa1"], context=ALICE)
        assert await brain.erase("project:a", ["aaaaaaaa1", "aaaaaaaa1"], context=ALICE) == 1
        [tomb] = await brain._pipeline.vector_store.lookup_ids(["aaaaaaaa1"])
        assert tomb.deleted, "the tombstone stays"
        [result] = await brain.push_records("project:a", [_rec("aaaaaaaa1", "Deploys on Tuesdays.")], context=ALICE)
        assert result.status == "rejected", "an erased id stays forgotten"

    async def test_needs_forget_permission(self):
        brain = _brain(acl=True)
        with pytest.raises(AccessDenied):
            await brain.erase("project:a", ["aaaaaaaa1"], context=ALICE)
        assert await brain.erase("project:a", ["aaaaaaaa1"], context=ADMIN) == 0


class TestForgetPrincipal:
    async def _seed(self, brain: Astrocyte) -> None:
        await brain.push_records("project:a", [_rec("a-alice-1", "Alice: deploys on Tuesdays."),
                                               _rec("a-bob-001", "Bob: staging at s.test.")], context=ALICE)
        await brain.push_records("project:a", [_rec("a-bob-002", "Bob notes Alice prefers tabs.",
                                                    tags=["principal:user:alice"])], context=BOB)
        await brain.push_records("project:b", [_rec("b-alice-1", "Alice: flags in LaunchDarkly.")], context=ALICE)
        await brain.push_records("other:x", [_rec("x-alice-1", "Another tenant's memory.")], context=ALICE)
        # Bob's first row was pushed with Alice's context; re-stamp it as Bob's for the test.
        store = brain._pipeline.vector_store
        item = store._vectors["a-bob-001"]
        item.metadata = {**(item.metadata or {}), "_actor": "user:bob"}

    async def test_forgets_and_erases_what_the_principal_saved_or_is_tagged_with(self):
        brain = _brain()
        await self._seed(brain)
        swept = await brain.forget_principal("user:alice", bank_prefix="project:", context=ADMIN)
        assert swept == [("project:a", 2, 2), ("project:b", 1, 1)]
        assert await _live(brain, "project:a") == {"a-bob-001"}
        assert await _live(brain, "project:b") == set()
        assert await _live(brain, "other:x") == {"x-alice-1"}, "only the tenant's banks"

    async def test_a_legal_hold_does_not_block_an_erasure(self):
        brain = _brain()
        await self._seed(brain)
        brain.set_legal_hold("project:a", "litigation-1", "pending case")
        swept = dict((b, d) for b, d, _ in await brain.forget_principal("user:alice", bank_prefix="project:",
                                                                         context=ADMIN))
        assert swept["project:a"] == 2

    async def test_reports_banks_with_nothing_to_erase(self):
        brain = _brain()
        await self._seed(brain)
        assert await brain.forget_principal("user:carol", bank_prefix="project:", context=ADMIN) == [
            ("project:a", 0, 0), ("project:b", 0, 0)]

    async def test_needs_a_bank_prefix(self):
        with pytest.raises(ValueError):
            await _brain().forget_principal("user:alice", bank_prefix="", context=ADMIN)
