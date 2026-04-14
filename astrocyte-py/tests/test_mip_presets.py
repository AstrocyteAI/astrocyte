"""Tests for MIP pipeline presets and PipelineSpec schema."""

from __future__ import annotations

from astrocyte.mip.presets import (
    PRESETS,
    expand_preset,
    is_known_preset,
    list_presets,
)
from astrocyte.mip.schema import (
    ActionSpec,
    ChunkerSpec,
    DedupSpec,
    PipelineSpec,
    ReflectSpec,
    RerankSpec,
)


class TestPipelineSpecSchema:
    def test_action_spec_has_optional_pipeline(self) -> None:
        # Existing usage stays valid: no pipeline kwarg required
        action = ActionSpec(bank="user-123")
        assert action.pipeline is None

    def test_action_spec_carries_pipeline_when_set(self) -> None:
        action = ActionSpec(
            bank="user-123",
            pipeline=PipelineSpec(version=1, preset="conversational"),
        )
        assert action.pipeline is not None
        assert action.pipeline.version == 1
        assert action.pipeline.preset == "conversational"

    def test_pipeline_spec_all_fields_optional(self) -> None:
        spec = PipelineSpec()
        assert spec.version is None
        assert spec.preset is None
        assert spec.chunker is None
        assert spec.dedup is None
        assert spec.rerank is None
        assert spec.reflect is None


class TestPresetRegistry:
    def test_known_presets_present(self) -> None:
        assert is_known_preset("conversational")
        assert is_known_preset("document")
        assert is_known_preset("code")
        assert is_known_preset("evidence_strict")

    def test_unknown_preset(self) -> None:
        assert not is_known_preset("nonexistent")
        assert not is_known_preset("")

    def test_list_presets_sorted(self) -> None:
        names = list_presets()
        assert names == sorted(names)
        assert "conversational" in names

    def test_conversational_preset_shape(self) -> None:
        spec = PRESETS["conversational"]
        assert spec.chunker is not None
        assert spec.chunker.strategy == "dialogue"
        assert spec.dedup is not None
        assert spec.dedup.threshold == 0.92
        assert spec.reflect is not None
        assert spec.reflect.prompt == "temporal_aware"
        assert spec.reflect.promote_metadata == ["speaker", "occurred_at"]

    def test_evidence_strict_preset_inherits_chunker(self) -> None:
        # evidence_strict is policy-only — does not impose a chunker
        spec = PRESETS["evidence_strict"]
        assert spec.chunker is None
        assert spec.reflect is not None
        assert spec.reflect.prompt == "evidence_strict"


class TestExpandPreset:
    def test_no_preset_returns_unchanged(self) -> None:
        spec = PipelineSpec(
            version=1,
            chunker=ChunkerSpec(strategy="sentence", max_size=500),
        )
        result = expand_preset(spec)
        assert result is spec

    def test_preset_alone_expands_to_full_spec(self) -> None:
        spec = PipelineSpec(version=1, preset="conversational")
        result = expand_preset(spec)
        assert result.preset is None
        assert result.version == 1
        assert result.chunker is not None
        assert result.chunker.strategy == "dialogue"
        assert result.dedup is not None
        assert result.dedup.threshold == 0.92

    def test_explicit_override_wins_over_preset(self) -> None:
        spec = PipelineSpec(
            version=1,
            preset="conversational",
            chunker=ChunkerSpec(max_size=400),  # override only max_size
        )
        result = expand_preset(spec)
        # Strategy from preset preserved
        assert result.chunker is not None
        assert result.chunker.strategy == "dialogue"
        # max_size from override wins
        assert result.chunker.max_size == 400

    def test_dedup_override_partial_merge(self) -> None:
        spec = PipelineSpec(
            preset="conversational",
            dedup=DedupSpec(threshold=0.99),  # override threshold, keep action
        )
        result = expand_preset(spec)
        assert result.dedup is not None
        assert result.dedup.threshold == 0.99
        assert result.dedup.action == "skip_chunk"  # from preset

    def test_rerank_override_partial_merge(self) -> None:
        spec = PipelineSpec(
            preset="conversational",
            rerank=RerankSpec(keyword_weight=0.20),
        )
        result = expand_preset(spec)
        assert result.rerank is not None
        assert result.rerank.keyword_weight == 0.20
        assert result.rerank.proper_noun_weight == 0.15  # from preset

    def test_reflect_override_partial_merge(self) -> None:
        spec = PipelineSpec(
            preset="conversational",
            reflect=ReflectSpec(prompt="evidence_strict"),
        )
        result = expand_preset(spec)
        assert result.reflect is not None
        assert result.reflect.prompt == "evidence_strict"
        # promote_metadata from preset preserved
        assert result.reflect.promote_metadata == ["speaker", "occurred_at"]

    def test_evidence_strict_chunker_stays_none_unless_overridden(self) -> None:
        spec = PipelineSpec(preset="evidence_strict")
        result = expand_preset(spec)
        assert result.chunker is None

    def test_evidence_strict_with_explicit_chunker(self) -> None:
        spec = PipelineSpec(
            preset="evidence_strict",
            chunker=ChunkerSpec(strategy="paragraph", max_size=900),
        )
        result = expand_preset(spec)
        assert result.chunker is not None
        assert result.chunker.strategy == "paragraph"

    def test_version_carries_through_expansion(self) -> None:
        spec = PipelineSpec(version=3, preset="document")
        result = expand_preset(spec)
        assert result.version == 3
