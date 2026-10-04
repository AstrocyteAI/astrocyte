"""Team-memory change feed (team-memory.md §8, G3): store SPI, cursor, ``Astrocyte.list_changes``.

Timestamps are explicit wherever order matters: Windows clocks tick every
~15 ms, so consecutive operations can share a timestamp.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from astrocyte import MemoryChange, MemoryChangePage
from astrocyte._astrocyte import Astrocyte
from astrocyte._sync import decode_cursor, encode_cursor
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied, AstrocyteError, CapabilityNotSupported, ConfigError, InvalidCursor
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryEngineProvider, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import AccessGrant, AstrocyteContext, VectorItem

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _item(mid: str, *, bank: str = "bank-1", at: datetime = T0, text: str | None = None) -> VectorItem:
    return VectorItem(
        id=mid,
        bank_id=bank,
        vector=[1.0, 0.0, 0.0],
        text=text or f"memory {mid}",
        metadata={"_created_at": at.isoformat(), "source": "test"},
        tags=["t1"],
        fact_type="world",
        occurred_at=at - timedelta(days=1),
        retained_at=at,
    )


def _brain(store: object | None = None, *, acl: bool = False) -> tuple[Astrocyte, InMemoryVectorStore]:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    if acl:
        config.access_control.enabled = True
        config.access_control.default_policy = "deny"
    brain = Astrocyte(config)
    vs = store if store is not None else InMemoryVectorStore()
    brain.set_pipeline(PipelineOrchestrator(vector_store=vs, llm_provider=MockLLMProvider()))
    return brain, vs  # type: ignore[return-value]


async def _drain(brain: Astrocyte, bank: str, limit: int, cursor: str | None = None, **kw) -> list[MemoryChange]:
    seen: list[MemoryChange] = []
    while True:
        page = await brain.list_changes(bank, cursor=cursor, limit=limit, **kw)
        seen.extend(page.changes)
        assert len(page.changes) <= limit
        if not page.has_more:
            return seen
        assert len(page.changes) == limit
        cursor = page.next_cursor


# ── cursor ───────────────────────────────────────────────────────────────


class TestCursor:
    def test_round_trip(self):
        at = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
        assert decode_cursor(encode_cursor(at, "abc123")) == (at, "abc123")

    def test_is_urlsafe_base64_json(self):
        cursor = encode_cursor(T0, "x_y-z")
        assert "=" not in cursor and "+" not in cursor and "/" not in cursor
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        assert payload == {"changed_at": T0.isoformat(), "id": "x_y-z"}

    def test_naive_datetimes_are_utc(self):
        assert decode_cursor(encode_cursor(T0.replace(tzinfo=None), "a")) == (T0, "a")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not base64!!",
            base64.urlsafe_b64encode(b"not json").decode(),
            base64.urlsafe_b64encode(b"[1, 2]").decode(),
            base64.urlsafe_b64encode(b'{"id": "a"}').decode(),
            base64.urlsafe_b64encode(b'{"changed_at": "yesterday", "id": "a"}').decode(),
            base64.urlsafe_b64encode(b'{"changed_at": "2026-01-01T00:00:00", "id": "a"}').decode(),  # no tz
            base64.urlsafe_b64encode(b'{"changed_at": "2026-01-01T00:00:00+00:00", "id": 5}').decode(),
            base64.urlsafe_b64encode(b'{"changed_at": "2026-01-01T00:00:00+00:00", "id": ""}').decode(),
            base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        ],
    )
    def test_invalid_cursor(self, bad: str):
        with pytest.raises(InvalidCursor):
            decode_cursor(bad)
        assert issubclass(InvalidCursor, ValueError) and issubclass(InvalidCursor, AstrocyteError)


# ── in-memory store SPI ──────────────────────────────────────────────────


class TestInMemoryListChanges:
    async def test_orders_by_changed_at_then_id(self):
        store = InMemoryVectorStore()
        await store.store_vectors([_item("c", at=T0), _item("a", at=T0), _item("b", at=T0 - timedelta(hours=1))])
        await store.store_vectors([_item("other", bank="bank-2", at=T0)])
        changes = await store.list_changes("bank-1")
        assert [(c.id, c.changed_at) for c in changes] == [
            ("b", T0 - timedelta(hours=1)),
            ("a", T0),
            ("c", T0),
        ]
        a = changes[1]
        assert a == MemoryChange(
            id="a",
            bank_id="bank-1",
            changed_at=T0,
            text="memory a",
            occurred_at=T0 - timedelta(days=1),
            retained_at=T0,
            tags=["t1"],
            fact_type="world",
            metadata={"_created_at": T0.isoformat(), "source": "test"},
        )

    async def test_after_is_strict(self):
        store = InMemoryVectorStore()
        await store.store_vectors([_item(m, at=T0) for m in ("a", "b", "c")])
        assert [c.id for c in await store.list_changes("bank-1", after=(T0, "a"))] == ["b", "c"]
        assert [c.id for c in await store.list_changes("bank-1", after=(T0, "c"))] == []
        assert [c.id for c in await store.list_changes("bank-1", after=(T0 - timedelta(1), "z"))] == ["a", "b", "c"]
        assert [c.id for c in await store.list_changes("bank-1", limit=2)] == ["a", "b"]

    async def test_delete_leaves_tombstone_and_restore_clears_it(self):
        store = InMemoryVectorStore()
        await store.store_vectors([_item("a"), _item("b")])
        assert await store.delete(["a"], "bank-2") == 0  # bank isolation
        assert await store.delete(["a"], "bank-1") == 1
        changes = await store.list_changes("bank-1")
        assert [(c.id, c.deleted) for c in changes] == [("b", False), ("a", True)]
        tomb = changes[1]
        assert tomb.text is None and tomb.metadata is None and tomb.changed_at > T0
        assert await store.list_changes("bank-2") == []
        later = datetime.now(UTC) + timedelta(hours=1)
        await store.store_vectors([_item("a", at=later)])
        assert [(c.id, c.deleted) for c in await store.list_changes("bank-1")] == [("b", False), ("a", False)]


# ── Astrocyte.list_changes ───────────────────────────────────────────────


class TestAstrocyteListChanges:
    async def test_pages_resume_without_gaps_or_repeats(self):
        brain, store = _brain()
        # 25 rows share one changed_at — only the id tiebreak orders them —
        # bracketed by rows before and after.
        items = [_item(f"m{i:02d}", at=T0) for i in range(25)]
        items += [_item("early", at=T0 - timedelta(minutes=5)), _item("late", at=T0 + timedelta(minutes=5))]
        await store.store_vectors(items)
        expected = ["early", *sorted(f"m{i:02d}" for i in range(25)), "late"]
        for limit in (1, 2, 7, 25, 26, 27, 100):
            assert [c.id for c in await _drain(brain, "bank-1", limit)] == expected, limit

    async def test_next_cursor_and_has_more(self):
        brain, store = _brain()
        empty = await brain.list_changes("bank-1")
        assert empty == MemoryChangePage(changes=[], next_cursor=None, has_more=False)

        await store.store_vectors([_item(m, at=T0) for m in ("a", "b", "c")])
        first = await brain.list_changes("bank-1", limit=2)
        assert [c.id for c in first.changes] == ["a", "b"] and first.has_more
        assert decode_cursor(first.next_cursor) == (T0, "b")
        rest = await brain.list_changes("bank-1", cursor=first.next_cursor, limit=2)
        assert [c.id for c in rest.changes] == ["c"] and not rest.has_more
        # Caught up: an empty page hands the cursor back, so the client keeps its place.
        caught_up = await brain.list_changes("bank-1", cursor=rest.next_cursor)
        assert caught_up.changes == [] and caught_up.next_cursor == rest.next_cursor
        # A change made later shows up after that cursor.
        await store.store_vectors([_item("d", at=T0 + timedelta(seconds=1))])
        assert [c.id for c in (await brain.list_changes("bank-1", cursor=rest.next_cursor)).changes] == ["d"]

    async def test_forget_appears_as_tombstone_after_the_cursor(self):
        brain, store = _brain()
        await store.store_vectors([_item("keep"), _item("gone")])
        page = await brain.list_changes("bank-1")
        assert [c.id for c in page.changes] == ["gone", "keep"]
        await brain.forget("bank-1", memory_ids=["gone"])
        after = await brain.list_changes("bank-1", cursor=page.next_cursor)
        [tomb] = after.changes
        assert (tomb.id, tomb.deleted, tomb.text) == ("gone", True, None)
        # From the start, the forgotten row is a tombstone in its new place.
        assert [(c.id, c.deleted) for c in (await brain.list_changes("bank-1")).changes] == [
            ("keep", False),
            ("gone", True),
        ]

    async def test_retain_shows_up_in_the_feed(self):
        brain, _ = _brain()
        result = await brain.retain("we use SQS for the job queue", bank_id="bank-1")
        [change] = (await brain.list_changes("bank-1")).changes
        assert change.id == result.memory_id and change.text == "we use SQS for the job queue"
        assert change.changed_at == change.retained_at and not change.deleted

    async def test_settle_window_holds_back_the_newest_changes(self):
        brain, store = _brain()
        now = datetime.now(UTC)
        await store.store_vectors([_item("old1", at=T0), _item("old2", at=T0), _item("fresh", at=now)])
        page = await brain.list_changes("bank-1", settle_seconds=60)
        # Only the settled prefix; the fresh row waits, so nothing is "more" yet.
        assert [c.id for c in page.changes] == ["old1", "old2"] and page.has_more is False
        assert decode_cursor(page.next_cursor) == (T0, "old2")
        assert (await brain.list_changes("bank-1", cursor=page.next_cursor, settle_seconds=60)).changes == []
        # Once settled (here: a smaller window), it follows on from the same cursor.
        later = await brain.list_changes("bank-1", cursor=page.next_cursor, settle_seconds=0)
        assert [c.id for c in later.changes] == ["fresh"]
        first = await brain.list_changes("bank-1", limit=1, settle_seconds=60)
        assert [c.id for c in first.changes] == ["old1"] and first.has_more is True

    @pytest.mark.parametrize("limit", [0, -1, 1001])
    async def test_limit_out_of_range(self, limit: int):
        brain, _ = _brain()
        with pytest.raises(ValueError, match="limit"):
            await brain.list_changes("bank-1", limit=limit)

    async def test_invalid_cursor_and_bank_id(self):
        brain, _ = _brain()
        with pytest.raises(InvalidCursor):
            await brain.list_changes("bank-1", cursor="garbage")
        with pytest.raises(ConfigError):
            await brain.list_changes("bad bank id")

    async def test_requires_read(self):
        brain, store = _brain(acl=True)
        await store.store_vectors([_item("a")])
        brain.set_access_grants(
            [
                AccessGrant(bank_id="bank-1", principal="user:reader", permissions=["read"]),
                AccessGrant(bank_id="bank-1", principal="user:writer", permissions=["write"]),
            ]
        )
        page = await brain.list_changes("bank-1", context=AstrocyteContext(principal="user:reader"))
        assert [c.id for c in page.changes] == ["a"]
        for ctx in (AstrocyteContext(principal="user:writer"), None):
            with pytest.raises(AccessDenied):
                await brain.list_changes("bank-1", context=ctx)

    async def test_store_without_list_changes_is_unsupported(self):
        class LegacyStore(InMemoryVectorStore):
            list_changes = None  # type: ignore[assignment]

        brain, _ = _brain(LegacyStore())
        with pytest.raises(CapabilityNotSupported) as exc:
            await brain.list_changes("bank-1")
        assert exc.value.capability == "list_changes"

    async def test_engine_provider_is_unsupported(self):
        config = AstrocyteConfig()
        config.barriers.pii.mode = "disabled"
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())
        with pytest.raises(CapabilityNotSupported):
            await brain.list_changes("bank-1")
