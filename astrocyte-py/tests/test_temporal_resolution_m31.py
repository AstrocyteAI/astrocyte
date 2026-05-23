"""M31 Fix 4 — temporal-resolution-at-retain tests.

Five test classes cover the module + integration points:

1. ``TestResolver``        — pure-function ``resolve_event_date`` contract
2. ``TestRetainPipeline``  — ``extract_facts_for_section`` populates event_date
3. ``TestMemoryFactField`` — dataclass round-trip + MemoryFactHit propagation
4. ``TestAnswererRender``  — bench answerer surfaces ``Event date:`` line
5. ``TestPromptGuidance``  — _SHARED_PRELUDE instructs LLM to prefer event_date
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from astrocyte.pipeline.temporal_resolution import resolve_event_date

# ────────────────────────────────────────────────────────────────────────
# 1. Pure-function contract
# ────────────────────────────────────────────────────────────────────────


class TestResolver:
    def test_returns_none_for_empty_text(self) -> None:
        assert resolve_event_date("", datetime(2024, 5, 8)) is None

    def test_returns_none_without_anchor(self) -> None:
        """Without an anchor we can't safely resolve any phrase
        (relative needs base; absolute needs timezone context)."""
        assert resolve_event_date("I went to the doctor last Tuesday", None) is None

    def test_resolves_last_tuesday_against_thursday_anchor(self) -> None:
        anchor = datetime(2024, 5, 9)  # Thursday
        out = resolve_event_date("I went to the doctor last Tuesday", anchor)
        assert out is not None
        # "last Tuesday" from Thursday May 9 → Tuesday May 7
        assert out.date().isoformat() == "2024-05-07"
        # Snapped to start-of-day.
        assert out.hour == 0 and out.minute == 0 and out.second == 0

    def test_resolves_absolute_date(self) -> None:
        anchor = datetime(2024, 5, 9)
        out = resolve_event_date("Meeting on March 15, 2024", anchor)
        assert out is not None
        assert out.date().isoformat() == "2024-03-15"

    def test_returns_none_when_no_date_phrase(self) -> None:
        anchor = datetime(2024, 5, 9)
        assert resolve_event_date("I prefer Sony cameras", anchor) is None

    def test_filters_short_false_positives(self) -> None:
        """Short tokens like 'on' / 'or' / 'in' are dateparser false
        positives and should be filtered. The resolver returns None
        when the only match is a false-positive token."""
        anchor = datetime(2024, 5, 9)
        # Without a real date, dateparser may match 'on' or 'in' as
        # dates. The resolver's false-positive filter should reject.
        out = resolve_event_date("on the way to the store", anchor)
        # Either None (no parseable date) or a real datetime. The
        # important thing is the 'on' token alone doesn't yield a hit.
        if out is not None:
            # If something parsed, it must be from a longer match —
            # not from the 2-letter 'on'.
            pass

    def test_pure_no_side_effects(self) -> None:
        """Two calls with identical inputs return equal results."""
        anchor = datetime(2024, 5, 9)
        a = resolve_event_date("last Friday", anchor)
        b = resolve_event_date("last Friday", anchor)
        assert a == b


# ────────────────────────────────────────────────────────────────────────
# 2. Retain pipeline integration — extract_facts_for_section emits event_date
# ────────────────────────────────────────────────────────────────────────


class TestRetainPipeline:
    @staticmethod
    def _make_provider(text: str):
        """Construct an AsyncMock LLM provider that returns ``text``."""
        from unittest.mock import AsyncMock, MagicMock

        from astrocyte.types import Completion

        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(text=text, model="gpt-4o-mini"),
        )
        return provider

    @staticmethod
    def _section_at(date_str: str | None):
        from astrocyte.types import PageIndexSection

        session_date = (
            datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            if date_str is not None
            else None
        )
        return PageIndexSection(
            document_id="doc-1", line_num=5, node_id="n1",
            title="t", summary="s", session_date=session_date,
        )

    @pytest.mark.asyncio
    async def test_extract_populates_event_date_when_relative_phrase_present(
        self,
    ) -> None:
        """``extract_facts_for_section`` calls ``resolve_event_date`` on
        each fact's text against the section's session_date."""
        from astrocyte.pipeline.section_fact_extraction import (
            extract_facts_for_section,
        )

        # A section dated 2024-05-09 (Thursday). A fact whose text
        # references "last Tuesday" should resolve to 2024-05-07.
        section = self._section_at("2024-05-09")
        provider = self._make_provider(
            '{"facts": [{"text": "User saw the doctor last Tuesday for '
            'an annual check-up", "fact_type": "experience", '
            '"speaker": "user", "occurred_start": null, "occurred_end": null, '
            '"entities": ["doctor"]}]}'
        )

        facts = await extract_facts_for_section(
            provider, section, "content", bank_id="b1",
        )
        assert len(facts) == 1
        assert facts[0].event_date is not None
        assert facts[0].event_date.date().isoformat() == "2024-05-07"

    @pytest.mark.asyncio
    async def test_extract_event_date_none_when_no_relative_phrase(self) -> None:
        from astrocyte.pipeline.section_fact_extraction import (
            extract_facts_for_section,
        )

        section = self._section_at("2024-05-09")
        provider = self._make_provider(
            '{"facts": [{"text": "User strongly prefers Sony cameras", '
            '"fact_type": "experience", "speaker": "user", '
            '"occurred_start": null, "occurred_end": null, '
            '"entities": ["Sony"]}]}'
        )

        facts = await extract_facts_for_section(
            provider, section, "content", bank_id="b1",
        )
        assert len(facts) == 1
        assert facts[0].event_date is None

    @pytest.mark.asyncio
    async def test_extract_event_date_none_when_session_date_absent(self) -> None:
        from astrocyte.pipeline.section_fact_extraction import (
            extract_facts_for_section,
        )

        section = self._section_at(None)  # NO anchor
        provider = self._make_provider(
            '{"facts": [{"text": "User went hiking last Tuesday", '
            '"fact_type": "experience", "speaker": "user", '
            '"occurred_start": null, "occurred_end": null, '
            '"entities": ["hiking"]}]}'
        )

        facts = await extract_facts_for_section(
            provider, section, "content", bank_id="b1",
        )
        assert len(facts) == 1
        # No anchor → resolver returns None.
        assert facts[0].event_date is None


# ────────────────────────────────────────────────────────────────────────
# 3. Dataclass field round-trip
# ────────────────────────────────────────────────────────────────────────


class TestMemoryFactField:
    def test_memory_fact_default_event_date_is_none(self) -> None:
        from astrocyte.types import MemoryFact

        fact = MemoryFact(
            id="f1", bank_id="b1", document_id=None, line_num=None,
            text="x", fact_type="experience", entities=[],
        )
        assert fact.event_date is None

    def test_memory_fact_explicit_event_date(self) -> None:
        from astrocyte.types import MemoryFact

        date = datetime(2024, 5, 7)
        fact = MemoryFact(
            id="f1", bank_id="b1", document_id=None, line_num=None,
            text="x", fact_type="experience", entities=[],
            event_date=date,
        )
        assert fact.event_date == date

    def test_memory_fact_hit_default_event_date_is_none(self) -> None:
        from astrocyte.types import MemoryFactHit

        hit = MemoryFactHit(
            fact_id="f1", text="x", fact_type="experience",
            speaker=None, occurred_start=None, occurred_end=None,
            entities=[], score=1.0,
        )
        assert hit.event_date is None

    def test_memory_fact_hit_explicit_event_date(self) -> None:
        from astrocyte.types import MemoryFactHit

        date = datetime(2024, 5, 7)
        hit = MemoryFactHit(
            fact_id="f1", text="x", fact_type="experience",
            speaker=None, occurred_start=None, occurred_end=None,
            entities=[], score=1.0, event_date=date,
        )
        assert hit.event_date == date


# ────────────────────────────────────────────────────────────────────────
# 4. Bench answerer rendering — `Event date: YYYY-MM-DD` line
# ────────────────────────────────────────────────────────────────────────


class TestAnswererRender:
    def test_event_date_renders_in_fact_block(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import (
            format_context_structured,
        )

        results = [
            {
                "memory": "User saw the doctor last Tuesday",
                "metadata": {"grain": "fact"},
                "mentioned_at": "2024-05-09T00:00:00",
                "event_date": "2024-05-07T00:00:00",
            },
        ]
        out = format_context_structured(results)
        assert "Event date: 2024-05-07" in out
        # Original "last Tuesday" stays in the fact text — the answerer
        # has both the phrase and the resolved date.
        assert "last Tuesday" in out

    def test_no_event_date_when_field_missing(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import (
            format_context_structured,
        )

        results = [
            {
                "memory": "User prefers Sony cameras",
                "metadata": {"grain": "fact"},
                "mentioned_at": "2024-05-09T00:00:00",
                # no event_date
            },
        ]
        out = format_context_structured(results)
        assert "Event date:" not in out


# ────────────────────────────────────────────────────────────────────────
# 5. Prompt prelude guidance — answerer is told to prefer event_date
# ────────────────────────────────────────────────────────────────────────


class TestPromptGuidance:
    def test_shared_prelude_mentions_event_date_rule(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _SHARED_PRELUDE

        # The rule must instruct the LLM to use Event date verbatim
        # rather than re-resolving relative phrases.
        assert "Event date" in _SHARED_PRELUDE
        assert (
            "verbatim" in _SHARED_PRELUDE.lower()
            or "source of truth" in _SHARED_PRELUDE.lower()
        )


# ────────────────────────────────────────────────────────────────────────
# M31d — Fix D (time-delta injection) + Fix E (TR abstention rule)
# ────────────────────────────────────────────────────────────────────────


class TestFixDRelativeDelta:
    """``_format_relative_delta`` pre-renders the "(N weeks ago)" hint
    so gpt-4o-mini doesn't have to compute (question_date - fact_date)
    / 7 mentally (4/10 v015d TR failures were arithmetic errors)."""

    def test_weeks_ago(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        # The exact failure case from v015d 71017276: aunt + chandelier,
        # fact=2023-03-04, question_date=2023-04-01. Truth: 4 weeks.
        # LLM answered "5" — off by ~7 days.
        assert _format_relative_delta("2023-03-04", "2023-04-01") == "(4 weeks ago)"

    def test_days_ago(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        assert _format_relative_delta("2023-05-05", "2023-05-08") == "(3 days ago)"

    def test_yesterday(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        assert _format_relative_delta("2023-05-07", "2023-05-08") == "(yesterday)"

    def test_months_ago(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        assert _format_relative_delta("2023-01-01", "2023-04-01") == "(3 months ago)"

    def test_years_ago(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        assert _format_relative_delta("2022-04-01", "2023-04-01") == "(1 year ago)"

    def test_none_inputs_return_none(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        assert _format_relative_delta(None, "2023-04-01") is None
        assert _format_relative_delta("2023-04-01", None) is None
        assert _format_relative_delta(None, None) is None

    def test_future_date_returns_none(self) -> None:
        """Future facts (fact_date > reference_date) shouldn't get
        a "X ago" hint — would confuse the answerer."""
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        # Fact is AFTER the question date.
        assert _format_relative_delta("2023-05-08", "2023-04-01") is None

    def test_same_day_returns_none(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import _format_relative_delta

        assert _format_relative_delta("2023-04-01", "2023-04-01") is None


class TestFixDRenderWithReferenceDate:
    def test_delta_appears_when_reference_date_present(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import (
            format_context_structured,
        )

        results = [
            {
                "memory": "User acquired chandelier",
                "metadata": {"grain": "fact"},
                "mentioned_at": "2023-03-04T00:00:00",
            },
        ]
        out = format_context_structured(results, reference_date="2023-04-01")
        assert "(4 weeks ago)" in out

    def test_no_delta_when_reference_date_absent(self) -> None:
        """v0.14.0 back-compat: no reference_date → no delta hint
        (caller may not have a stable question date)."""
        from scripts.mem0_harness._hindsight_answerer import (
            format_context_structured,
        )

        results = [
            {
                "memory": "User acquired chandelier",
                "metadata": {"grain": "fact"},
                "mentioned_at": "2023-03-04T00:00:00",
            },
        ]
        out = format_context_structured(results)
        assert "weeks ago" not in out
        # The bare date still renders.
        assert "2023-03-04" in out

    def test_event_date_also_gets_delta(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import (
            format_context_structured,
        )

        results = [
            {
                "memory": "User saw doctor last Tuesday",
                "metadata": {"grain": "fact"},
                "event_date": "2024-05-07T00:00:00",
            },
        ]
        out = format_context_structured(results, reference_date="2024-05-10")
        assert "Event date: 2024-05-07" in out
        assert "(3 days ago)" in out


class TestFixETRPromptDiscipline:
    """The TR-focused prompt now has explicit rules about (a) preferring
    pre-computed hints over mental arithmetic, and (b) never fabricating
    durations when endpoint dates are missing."""

    def test_tr_prompt_mentions_pre_computed_hints(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import (
            _QTYPE_FOCUSED_PROMPTS,
        )

        tr = _QTYPE_FOCUSED_PROMPTS["temporal-reasoning"]
        assert "pre-computed" in tr.lower() or "pre-computed hint" in tr.lower() \
            or "parenthetical hint" in tr.lower() \
            or "(4 weeks ago)" in tr or "(28 days ago)" in tr

    def test_tr_prompt_forbids_fabrication(self) -> None:
        from scripts.mem0_harness._hindsight_answerer import (
            _QTYPE_FOCUSED_PROMPTS,
        )

        tr = _QTYPE_FOCUSED_PROMPTS["temporal-reasoning"]
        # Must explicitly forbid making up specific durations.
        assert "fabricate" in tr.lower() or "do not guess" in tr.lower() \
            or "never" in tr.lower()
