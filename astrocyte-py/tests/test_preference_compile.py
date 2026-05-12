"""M14.6: preference consolidation unit tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.preference_compile import compile_preferences_for_document
from astrocyte.testing.in_memory import InMemoryMentalModelStore
from astrocyte.types import Completion, PageIndexFact


def _fact(
    fact_id: str,
    text: str,
    *,
    fact_type: str = "preference",
    entities: list[str] | None = None,
) -> PageIndexFact:
    return PageIndexFact(
        id=fact_id,
        bank_id="b1",
        document_id="doc-1",
        line_num=5,
        text=text,
        fact_type=fact_type,
        speaker="user",
        entities=entities or [],
    )


def _provider(text: str) -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(return_value=Completion(text=text, model="gpt-4o-mini"))
    return p


pytestmark = pytest.mark.asyncio


class TestPreferenceCompile:
    async def test_zero_preference_facts_returns_empty(self) -> None:
        store = InMemoryMentalModelStore()
        provider = MagicMock()
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=[],  # no facts at all
            provider=provider,
        )
        assert out == []
        provider.complete.assert_not_called() if hasattr(provider.complete, "assert_not_called") else None

    async def test_one_preference_fact_skip_too_few(self) -> None:
        store = InMemoryMentalModelStore()
        provider = MagicMock()
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=[_fact("f1", "User prefers oatmeal")],  # only 1 preference
            provider=provider,
        )
        assert out == []
        # No LLM call — < 2 preferences is below the consolidation floor
        provider.complete.assert_not_called() if hasattr(provider.complete, "assert_not_called") else None

    async def test_filters_non_preference_facts(self) -> None:
        store = InMemoryMentalModelStore()
        provider = MagicMock()
        # 1 preference + 5 non-preference facts: still < 2 prefs, should skip
        facts = [
            _fact("f1", "User prefers oatmeal"),
            _fact("f2", "User visited Dr. Patel", fact_type="experience"),
            _fact("f3", "User attended a wedding", fact_type="experience"),
            _fact("f4", "Paris is in France", fact_type="world"),
            _fact("f5", "User plans to travel", fact_type="plan"),
        ]
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        assert out == []
        provider.complete.assert_not_called() if hasattr(provider.complete, "assert_not_called") else None

    async def test_happy_path_consolidates_two_preferences(self) -> None:
        store = InMemoryMentalModelStore()
        provider = _provider('{"preferences": ['
                             '{"title": "Breakfast preference",'
                             ' "content": "User prefers oatmeal with berries for breakfast on busy mornings, citing time efficiency.",'
                             ' "source_fact_ids": ["f1", "f2"]},'
                             '{"title": "Coffee preference",'
                             ' "content": "User strongly prefers cortado in mornings over drip, preferring intensity.",'
                             ' "source_fact_ids": ["f3"]}'
                             ']}')
        facts = [
            _fact("f1", "User said they like oatmeal"),
            _fact("f2", "Oatmeal with berries was their busy-morning go-to"),
            _fact("f3", "User prefers cortado in the morning"),
        ]
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        assert len(out) == 2

        # Both saved with kind='preference' and scope='document:doc-1'
        saved = await store.list("b1", scope="document:doc-1", kind="preference")
        assert len(saved) == 2
        for m in saved:
            assert m.kind == "preference"
            assert m.scope == "document:doc-1"
            assert m.bank_id == "b1"

    async def test_kind_filter_isolates_preference_models(self) -> None:
        """Kind-filtering on the store works — preference vs general are
        distinguishable. Ensures M14.6's kind discriminator round-trips."""
        store = InMemoryMentalModelStore()
        provider = _provider('{"preferences": ['
                             '{"title": "Coffee", "content": "User prefers cortado.",'
                             ' "source_fact_ids": ["f1"]},'
                             '{"title": "Music", "content": "User prefers jazz.",'
                             ' "source_fact_ids": ["f2"]}'
                             ']}')
        facts = [
            _fact("f1", "User prefers cortado over drip"),
            _fact("f2", "User prefers jazz at home"),
        ]
        await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )

        prefs_only = await store.list("b1", kind="preference")
        general_only = await store.list("b1", kind="general")
        assert len(prefs_only) == 2
        assert len(general_only) == 0

    async def test_idempotent_on_existing_preferences(self) -> None:
        """Re-running on a scope that already has preference models is a
        no-op (M14.6 v1 idempotency)."""
        store = InMemoryMentalModelStore()
        provider = _provider('{"preferences": ['
                             '{"title": "Coffee", "content": "User prefers cortado.",'
                             ' "source_fact_ids": ["f1"]}'
                             ']}')
        facts = [
            _fact("f1", "User prefers cortado"),
            _fact("f2", "User likes strong coffee"),
        ]
        first = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        assert len(first) == 1
        # Reset the mock for the second call so we can assert no second LLM call
        provider.complete.reset_mock()
        second = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        # Idempotent: returned the existing model_ids without re-LLM
        assert len(second) == 1
        assert first == second
        provider.complete.assert_not_called()

    async def test_malformed_json_returns_empty(self) -> None:
        store = InMemoryMentalModelStore()
        provider = _provider("not valid json")
        facts = [_fact(f"f{i}", f"pref {i}") for i in range(3)]
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        assert out == []

    async def test_llm_failure_returns_empty(self) -> None:
        store = InMemoryMentalModelStore()
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("api down"))
        facts = [_fact(f"f{i}", f"pref {i}") for i in range(3)]
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        assert out == []

    async def test_drops_entries_with_missing_title_or_content(self) -> None:
        store = InMemoryMentalModelStore()
        provider = _provider('{"preferences": ['
                             '{"title": "Valid", "content": "User prefers X.",'
                             ' "source_fact_ids": ["f1"]},'
                             '{"title": "", "content": "Missing title",'
                             ' "source_fact_ids": ["f2"]},'
                             '{"title": "Missing content", "content": "",'
                             ' "source_fact_ids": ["f3"]}'
                             ']}')
        facts = [_fact(f"f{i}", f"pref {i}") for i in range(1, 4)]
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider,
        )
        assert len(out) == 1

    async def test_caps_at_max_preferences(self) -> None:
        store = InMemoryMentalModelStore()
        prefs_json = ",".join(
            f'{{"title":"P{i}","content":"User prefers item-{i}.","source_fact_ids":["f{i}"]}}'
            for i in range(20)
        )
        provider = _provider(f'{{"preferences": [{prefs_json}]}}')
        facts = [_fact(f"f{i}", f"pref {i}") for i in range(20)]
        out = await compile_preferences_for_document(
            mental_model_store=store, bank_id="b1", document_id="doc-1",
            facts=facts, provider=provider, max_preferences=5,
        )
        assert len(out) == 5
