"""Tests for ``astrocyte.pipeline.section_embedding`` (PR2 commit A).

Pinned behaviours:
- Empty/whitespace summaries are skipped (no zero-length API call).
- All non-empty summaries → one batched embed call.
- Output is ``(line_num, vector)`` tuples in input order.
- Embed failures degrade silently (caller's index stays partial; the
  picker keeps working without semantic strategy on this doc).
- Length mismatch from the provider triggers a warning + empty result
  (defensive — the API should never do this but a stub provider
  could).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrocyte.pipeline.section_embedding import embed_sections
from astrocyte.types import PageIndexSection


@dataclass
class _StubProvider:
    """Minimal embed-only provider stub. Records call args so tests can
    assert against the request shape."""

    return_value: list[list[float]]
    raise_exc: Exception | None = None
    called_with: list[Any] = None
    model_seen: str | None = None

    def __post_init__(self) -> None:
        self.called_with = []

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        self.called_with.append(list(texts))
        self.model_seen = model
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


def _section(line_num: int, summary: str | None) -> PageIndexSection:
    return PageIndexSection(
        document_id="d",
        line_num=line_num,
        node_id=f"{line_num:04d}",
        title=f"node-{line_num}",
        summary=summary,
        depth=0,
    )


class TestEmbedSections:
    async def test_returns_pairs_in_input_order(self) -> None:
        sections = [
            _section(5, "first"),
            _section(12, "second"),
            _section(20, "third"),
        ]
        provider = _StubProvider(return_value=[[1.0], [2.0], [3.0]])
        result = await embed_sections(provider, sections)
        assert result == [(5, [1.0]), (12, [2.0]), (20, [3.0])]
        assert provider.called_with == [["first", "second", "third"]]

    async def test_skips_sections_without_summary(self) -> None:
        sections = [
            _section(1, None),
            _section(5, "real summary"),
            _section(10, ""),
            _section(15, "   "),  # whitespace only
            _section(20, "another"),
        ]
        provider = _StubProvider(return_value=[[1.0], [2.0]])
        result = await embed_sections(provider, sections)
        # Only line_num=5 and line_num=20 had real summaries
        assert [ln for ln, _ in result] == [5, 20]
        assert provider.called_with == [["real summary", "another"]]

    async def test_empty_input_no_api_call(self) -> None:
        provider = _StubProvider(return_value=[])
        result = await embed_sections(provider, [])
        assert result == []
        # Critical: don't burn an API call on empty input.
        assert provider.called_with == []

    async def test_all_summaries_empty_no_api_call(self) -> None:
        sections = [_section(1, None), _section(5, ""), _section(10, "  ")]
        provider = _StubProvider(return_value=[])
        result = await embed_sections(provider, sections)
        assert result == []
        assert provider.called_with == []

    async def test_embed_failure_degrades_silently(self) -> None:
        # Provider raises → return empty list, log warning. Picker keeps
        # working without semantic strategy on this doc.
        sections = [_section(5, "topic")]
        provider = _StubProvider(return_value=[], raise_exc=RuntimeError("API down"))
        result = await embed_sections(provider, sections)
        assert result == []

    async def test_length_mismatch_returns_empty(self) -> None:
        # Defensive: if provider returns a different number of vectors
        # than texts, we can't pair them safely. Better to drop the
        # whole batch than misalign.
        sections = [_section(5, "a"), _section(10, "b")]
        provider = _StubProvider(return_value=[[1.0]])  # 1 vec for 2 texts
        result = await embed_sections(provider, sections)
        assert result == []

    async def test_model_override_propagates(self) -> None:
        sections = [_section(5, "topic")]
        provider = _StubProvider(return_value=[[0.5]])
        await embed_sections(provider, sections, model="text-embedding-3-large")
        assert provider.model_seen == "text-embedding-3-large"

    async def test_default_model_is_none(self) -> None:
        # Provider's configured default wins when caller doesn't override.
        sections = [_section(5, "topic")]
        provider = _StubProvider(return_value=[[0.5]])
        await embed_sections(provider, sections)
        assert provider.model_seen is None
