"""Team-memory batch push (team-memory.md §8, G2): ``Astrocyte.push_records``.

Every pushed record becomes exactly one row with the client's id, through
the retain policy layer, never overwriting a row in another bank or undoing
a forget. Runs on the in-memory store; the SQL stores' insert-only and
lookup methods are covered by their own suites and the SQLite/Postgres parity
suite.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astrocyte import SyncPushRecord, SyncPushResult
from astrocyte._astrocyte import Astrocyte
from astrocyte._sync import content_hash
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied, CapabilityNotSupported
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.sync_push import REASON_FORGOTTEN, REASON_IN_USE, REASON_METADATA_IGNORED
from astrocyte.testing.in_memory import InMemoryEngineProvider, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import AccessGrant, AstrocyteContext, VectorItem

BANK = "project:api-1a2b3c"
OTHER = "project:web-9f9f9f"
ALICE = AstrocyteContext(principal="user:alice")
ID1, ID2, ID3 = "9f2c41d07ab3e815", "1c0d5e7f9a2b4c6d", "77aa88bb99cc00dd"


def _brain(
    *, pii: str = "disabled", pii_action: str = "redact", acl: bool = False, configure=None
) -> tuple[Astrocyte, InMemoryVectorStore, MockLLMProvider]:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = pii
    config.barriers.pii.action = pii_action
    if acl:
        config.access_control.enabled = True
        config.access_control.default_policy = "deny"
    if configure:
        configure(config)
    brain = Astrocyte(config)
    store, llm = InMemoryVectorStore(), MockLLMProvider()
    brain.set_pipeline(PipelineOrchestrator(vector_store=store, llm_provider=llm))
    return brain, store, llm


def _rec(mid: str = ID1, text: str = "We moved the job queue from SQS to Kafka.", **kw) -> SyncPushRecord:
    return SyncPushRecord(id=mid, text=text, **kw)


def _status(results: list[SyncPushResult]) -> list[tuple]:
    return [(r.id, r.status, r.duplicate_of, r.reason) for r in results]


class TestStored:
    async def test_one_row_with_the_client_id_and_no_llm_call(self):
        brain, store, llm = _brain()
        long_text = "Decision: " + " ".join(f"point {i} about the queue." for i in range(400))  # > one chunk
        occurred = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
        [result] = await brain.push_records(
            BANK,
            [_rec(text=long_text, occurred_at=occurred, tags=["decision"], fact_type="world")],
            context=ALICE,
        )
        assert result == SyncPushResult(id=ID1, status="stored")
        [row] = await store.list_vectors(BANK)
        assert (row.id, row.text, row.tags, row.fact_type, row.occurred_at) == (
            ID1,
            long_text,
            ["decision"],
            "world",
            occurred,
        )
        assert row.retained_at is not None and row.chunk_id is None
        assert llm._call_count == 0  # no extraction / completion, only embedding

    async def test_appears_in_recall_and_the_change_feed(self):
        brain, _, _ = _brain()
        await brain.push_records(BANK, [_rec()], context=ALICE)
        hits = (await brain.recall("job queue Kafka", bank_id=BANK, context=ALICE)).hits
        assert [h.memory_id for h in hits] == [ID1]
        [change] = (await brain.list_changes(BANK, context=ALICE)).changes
        assert change.id == ID1 and change.metadata["_actor"] == "user:alice"

    async def test_metadata_keeps_sync_keys_and_drops_system_and_blocked_keys(self):
        brain, store, _ = _brain()
        metadata = {
            "_created_at": "2026-10-02T09:14:03.118Z",
            "_retain_id": "r-1",
            "_chunk_index": 2,
            "session_id": "s-42",
            "source": "codex",
            "_authority_tier": "canonical",  # system key a client must not set
            "_mip.rule": "x",
            "api_key": "sk-123",  # blocked by the metadata barrier
        }
        await brain.push_records(BANK, [_rec(metadata=metadata)], context=ALICE)
        [row] = await store.list_vectors(BANK)
        assert row.metadata == {
            "_created_at": "2026-10-02T09:14:03.118Z",
            "_retain_id": "r-1",
            "_chunk_index": 2,
            "session_id": "s-42",
            "source": "codex",
            "_actor": "user:alice",
        }

    async def test_actor_is_overwritten_from_the_context(self):
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec(metadata={"_actor": "user:mallory"})], context=ALICE)
        assert (await store.list_vectors(BANK))[0].metadata["_actor"] == "user:alice"

    async def test_without_a_context_a_callers_actor_is_kept(self):
        # Library use, as retain: no authenticated caller to stamp.
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec(metadata={"_actor": "user:bob"})])
        assert (await store.list_vectors(BANK))[0].metadata["_actor"] == "user:bob"

    async def test_content_hash(self):
        brain, _, _ = _brain()
        text = "We moved the job queue from SQS to Kafka."
        results = await brain.push_records(
            BANK,
            [
                _rec(ID1, text, content_hash=content_hash(text)),
                _rec(ID2, "other text entirely", content_hash=content_hash(text)),
                _rec(ID3, "a third memory", content_hash="md5:abc"),
            ],
            context=ALICE,
        )
        assert [(r.status, r.reason) for r in results] == [
            ("stored", None),
            ("rejected", "content_hash does not match text"),
            ("rejected", "content_hash must be sha256:<64 lowercase hex>"),
        ]


class TestIdempotencyAndImmutability:
    async def test_same_id_same_text_is_unchanged(self):
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec()], context=ALICE)
        before = await store.list_changes(BANK)
        assert _status(await brain.push_records(BANK, [_rec()], context=ALICE)) == [(ID1, "unchanged", None, None)]
        assert await store.list_changes(BANK) == before  # no rewrite, no feed entry

    async def test_same_id_same_text_different_metadata_is_unchanged_and_metadata_untouched(self):
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec(metadata={"source": "codex"}, tags=["a"])], context=ALICE)
        before = await store.list_changes(BANK)
        results = await brain.push_records(
            BANK, [_rec(metadata={"source": "claude", "status": "stale"}, tags=["b"])], context=ALICE
        )
        assert _status(results) == [(ID1, "unchanged", None, REASON_METADATA_IGNORED)]
        [row] = await store.list_vectors(BANK)
        assert row.metadata == {"source": "codex", "_actor": "user:alice"} and row.tags == ["a"]
        assert await store.list_changes(BANK) == before

    async def test_same_id_different_text_is_rejected(self):
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec()], context=ALICE)
        results = await brain.push_records(BANK, [_rec(text="We use SQS.")], context=ALICE)
        assert _status(results) == [(ID1, "rejected", None, REASON_IN_USE)]
        assert [v.text for v in await store.list_vectors(BANK)] == ["We moved the job queue from SQS to Kafka."]

    async def test_same_id_twice_in_one_batch(self):
        brain, store, _ = _brain()
        results = await brain.push_records(
            BANK, [_rec(), _rec(), _rec(text="A different decision about deploys.")], context=ALICE
        )
        assert [r.status for r in results] == ["stored", "unchanged", "rejected"]
        assert len(await store.list_vectors(BANK)) == 1

    async def test_a_concurrent_writer_that_got_there_first_is_not_overwritten(self):
        brain, store, _ = _brain()
        real_insert = store.insert_vectors

        async def racing_insert(items):
            # Another push stores the same id between our lookup and our insert.
            winner = VectorItem(id=items[0].id, bank_id=BANK, vector=items[0].vector, text="their text")
            await store.store_vectors([winner])
            return await real_insert(items)

        store.insert_vectors = racing_insert  # type: ignore[method-assign]
        assert _status(await brain.push_records(BANK, [_rec()], context=ALICE)) == [
            (ID1, "rejected", None, REASON_IN_USE)
        ]
        assert [v.text for v in await store.list_vectors(BANK)] == ["their text"]


class TestDuplicates:
    async def test_near_duplicate_of_another_memory_is_reported_not_stored(self):
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec(ID1)], context=ALICE)
        results = await brain.push_records(BANK, [_rec(ID2)], context=ALICE)
        assert _status(results) == [(ID2, "duplicate", ID1, None)]
        assert [v.id for v in await store.list_vectors(BANK)] == [ID1]

    async def test_duplicate_of_a_retained_memory_found_in_the_store(self):
        # A fresh process: the dedup cache is empty, the store is consulted.
        brain, store, _ = _brain()
        await store.store_vectors(
            [VectorItem(id="existing0001", bank_id=BANK, vector=(await MockLLMProvider().embed(["same words"]))[0],
                        text="same words")]
        )
        results = await brain.push_records(BANK, [_rec(ID1, "same words")], context=ALICE)
        assert _status(results) == [(ID1, "duplicate", "existing0001", None)]

    async def test_near_duplicates_within_one_batch(self):
        brain, store, _ = _brain()
        results = await brain.push_records(BANK, [_rec(ID1), _rec(ID2)], context=ALICE)
        assert _status(results) == [(ID1, "stored", None, None), (ID2, "duplicate", ID1, None)]

    async def test_dedup_is_per_bank(self):
        brain, _, _ = _brain()
        await brain.push_records(OTHER, [_rec(ID1)], context=ALICE)
        assert (await brain.push_records(BANK, [_rec(ID2)], context=ALICE))[0].status == "stored"

    async def test_dedup_disabled_or_warn_stores_it(self):
        def off(config):
            config.signal_quality.dedup.action = "warn"

        brain, store, _ = _brain(configure=off)
        await brain.push_records(BANK, [_rec(ID1), _rec(ID2)], context=ALICE)
        assert sorted(v.id for v in await store.list_vectors(BANK)) == sorted([ID1, ID2])


class TestNeverOverwriteAcrossBanksOrForgets:
    async def test_id_held_by_another_bank_is_rejected_and_that_row_untouched(self):
        brain, store, _ = _brain()
        secret = "Bank web: the staging password rotates on Fridays."
        await store.store_vectors([VectorItem(id=ID1, bank_id=OTHER, vector=[0.5] * 128, text=secret)])
        before = await store.lookup_ids([ID1])
        results = await brain.push_records(BANK, [_rec(ID1, "an attempt to take over the row")], context=ALICE)
        assert _status(results) == [(ID1, "rejected", None, REASON_IN_USE)]
        assert secret not in (results[0].reason or "") and OTHER not in (results[0].reason or "")
        assert await store.lookup_ids([ID1]) == before  # same bank, text, vector
        assert await store.list_vectors(BANK) == []
        # Even with the other bank's exact text, it is not "unchanged" here.
        assert (await brain.push_records(BANK, [_rec(ID1, secret)], context=ALICE))[0].status == "rejected"

    async def test_forgotten_id_is_rejected_and_stays_forgotten(self):
        brain, store, _ = _brain()
        await brain.push_records(BANK, [_rec()], context=ALICE)
        await brain.forget(BANK, memory_ids=[ID1], context=ALICE)
        results = await brain.push_records(BANK, [_rec()], context=ALICE)
        assert _status(results) == [(ID1, "rejected", None, REASON_FORGOTTEN)]
        assert await store.list_vectors(BANK) == []
        [tomb] = await store.lookup_ids([ID1])
        assert tomb.deleted

    async def test_forgotten_id_in_another_bank_is_rejected_as_in_use(self):
        brain, store, _ = _brain()
        await store.store_vectors([VectorItem(id=ID1, bank_id=OTHER, vector=[0.5] * 128, text="x")])
        await store.delete([ID1], OTHER)
        assert _status(await brain.push_records(BANK, [_rec()], context=ALICE)) == [
            (ID1, "rejected", None, REASON_IN_USE)
        ]


class TestPolicy:
    async def test_pii_is_redacted(self):
        brain, store, _ = _brain(pii="regex", pii_action="redact")
        results = await brain.push_records(BANK, [_rec(text="Ping alice@example.com about the queue.")])
        assert results[0].status == "stored"
        [row] = await store.list_vectors(BANK)
        assert "alice@example.com" not in row.text and "the queue" in row.text

    async def test_pii_reject_rejects_only_that_record(self):
        brain, store, _ = _brain(pii="regex", pii_action="reject")
        results = await brain.push_records(
            BANK, [_rec(ID1, "Ping alice@example.com about the queue."), _rec(ID2, "No personal data here.")]
        )
        assert results[0].status == "rejected" and "PII detected" in results[0].reason
        assert "email" in results[0].reason
        assert results[1].status == "stored"
        assert [v.id for v in await store.list_vectors(BANK)] == [ID2]

    async def test_content_validation_and_size_limits(self):
        def limits(config):
            config.barriers.validation.max_content_length = 50
            config.homeostasis.retain_max_content_bytes = 200

        brain, store, _ = _brain(configure=limits)
        results = await brain.push_records(
            BANK,
            [_rec(ID1, "x" * 60), _rec(ID2, "y" * 300), _rec(ID3, "   "), _rec("okokokok", "fine")],
        )
        assert [r.status for r in results] == ["rejected", "rejected", "rejected", "stored"]
        assert all(r.reason for r in results[:3])

    async def test_write_permission_is_required(self):
        brain, _, _ = _brain(acl=True)
        brain.set_access_grants(
            [
                AccessGrant(bank_id="project:*", principal="user:alice", permissions=["read", "write"]),
                AccessGrant(bank_id="project:*", principal="user:reader", permissions=["read"]),
            ]
        )
        assert (await brain.push_records(BANK, [_rec()], context=ALICE))[0].status == "stored"
        for ctx in (AstrocyteContext(principal="user:reader"), None):
            with pytest.raises(AccessDenied):
                await brain.push_records(BANK, [_rec(ID2, "another")], context=ctx)

    async def test_batch_limit_and_id_format(self):
        brain, _, _ = _brain()
        with pytest.raises(ValueError, match="at most 100"):
            await brain.push_records(BANK, [_rec(f"id{i:08d}", f"t{i}") for i in range(101)])
        for bad in ("short", "has space1", "x" * 65, "semi;colon!", "ünïcödé123"):
            with pytest.raises(ValueError, match="invalid record id"):
                await brain.push_records(BANK, [_rec(bad)])
        assert await brain.push_records(BANK, []) == []

    async def test_rate_limit_applies_once_per_push(self):
        def one_per_minute(config):
            config.homeostasis.rate_limits.retain_per_minute = 1

        brain, _, _ = _brain(configure=one_per_minute)
        results = await brain.push_records(BANK, [_rec(f"id{i:08d}", f"memory number {i}") for i in range(5)])
        assert all(r.status != "rejected" for r in results)  # one rate-limit hit for the whole push
        from astrocyte.errors import RateLimited

        with pytest.raises(RateLimited):
            await brain.push_records(BANK, [_rec(ID2, "next push")])


class TestCapability:
    async def test_store_without_push_methods(self):
        class LegacyStore(InMemoryVectorStore):
            insert_vectors = None  # type: ignore[assignment]

        config = AstrocyteConfig()
        config.barriers.pii.mode = "disabled"
        brain = Astrocyte(config)
        brain.set_pipeline(PipelineOrchestrator(vector_store=LegacyStore(), llm_provider=MockLLMProvider()))
        # hasattr is true for a None attribute; the probe must treat it as absent.
        with pytest.raises(CapabilityNotSupported) as exc:
            await brain.push_records(BANK, [_rec()])
        assert exc.value.capability == "sync_push"

    async def test_engine_provider(self):
        config = AstrocyteConfig()
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())
        with pytest.raises(CapabilityNotSupported):
            await brain.push_records(BANK, [_rec()])
