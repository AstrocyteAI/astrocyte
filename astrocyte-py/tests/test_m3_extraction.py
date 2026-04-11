"""M3 — extraction profile resolution and content-type → chunking routing (TDD)."""

from __future__ import annotations

from astrocyte.config import ExtractionProfileConfig, SourceConfig
from astrocyte.pipeline.extraction import (
    apply_metadata_mapping,
    apply_tag_rules,
    extraction_profile_for_source,
    merged_user_and_builtin_profiles,
    normalize_content,
    prepare_retain_input,
    resolve_retain_chunking,
    resolve_retain_fact_type,
)
from astrocyte.types import RetainRequest


class TestResolveRetainChunking:
    """Rules: profile overrides content-type defaults; chunk_size from profile when set."""

    def test_text_uses_orchestrator_default_strategy(self):
        strategy, max_sz = resolve_retain_chunking(
            "text",
            profile=None,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "sentence"
        assert max_sz == 512

    def test_empty_content_type_uses_default_strategy(self):
        strategy, _ = resolve_retain_chunking(
            "",
            profile=None,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "sentence"

    def test_conversation_routes_to_dialogue(self):
        strategy, max_sz = resolve_retain_chunking(
            "conversation",
            profile=None,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "dialogue"
        assert max_sz == 512

    def test_transcript_routes_to_dialogue(self):
        strategy, _ = resolve_retain_chunking(
            "transcript",
            profile=None,
            default_strategy="sentence",
            default_max_chunk_size=400,
        )
        assert strategy == "dialogue"

    def test_document_routes_to_paragraph(self):
        strategy, _ = resolve_retain_chunking(
            "document",
            profile=None,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "paragraph"

    def test_email_routes_to_paragraph(self):
        strategy, _ = resolve_retain_chunking(
            "email",
            profile=None,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "paragraph"

    def test_event_routes_to_sentence(self):
        strategy, _ = resolve_retain_chunking(
            "event",
            profile=None,
            default_strategy="paragraph",
            default_max_chunk_size=512,
        )
        assert strategy == "sentence"

    def test_unknown_content_type_falls_back_to_default_strategy(self):
        strategy, _ = resolve_retain_chunking(
            "weird_future_type",
            profile=None,
            default_strategy="paragraph",
            default_max_chunk_size=512,
        )
        assert strategy == "paragraph"

    def test_profile_overrides_content_type(self):
        profile = ExtractionProfileConfig(chunking_strategy="paragraph")
        strategy, _ = resolve_retain_chunking(
            "conversation",
            profile=profile,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "paragraph"

    def test_profile_chunk_size_overrides_default(self):
        profile = ExtractionProfileConfig(chunk_size=256)
        _, max_sz = resolve_retain_chunking(
            "text",
            profile=profile,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert max_sz == 256

    def test_profile_semantic_maps_to_sentence(self):
        profile = ExtractionProfileConfig(chunking_strategy="semantic")
        strategy, _ = resolve_retain_chunking(
            "document",
            profile=profile,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "sentence"

    def test_profile_fixed_strategy(self):
        profile = ExtractionProfileConfig(chunking_strategy="fixed", chunk_size=100)
        strategy, max_sz = resolve_retain_chunking(
            "text",
            profile=profile,
            default_strategy="sentence",
            default_max_chunk_size=512,
        )
        assert strategy == "fixed"
        assert max_sz == 100


class TestNormalizeContent:
    def test_email_strips_rfc_headers_and_signature(self):
        raw = "From: a@b.c\nTo: x@y.z\n\nHello body.\n\n-- \nSig"
        out = normalize_content(raw, "email")
        assert "From:" not in out
        assert "Hello body." in out
        assert "Sig" not in out

    def test_transcript_collapses_excessive_blank_lines(self):
        raw = "A\n\n\n\nB"
        out = normalize_content(raw, "transcript")
        assert "\n\n\n" not in out


class TestMetadataAndTags:
    def test_metadata_mapping_json_path(self):
        profile = ExtractionProfileConfig(
            metadata_mapping={"speaker": "$.participant_name", "static_key": "literal"},
        )
        content = '{"participant_name": "Ada"}'
        meta = apply_metadata_mapping(content, profile)
        assert meta is not None
        assert meta["speaker"] == "Ada"
        assert meta["static_key"] == "literal"

    def test_tag_rules_contains(self):
        profile = ExtractionProfileConfig(
            tag_rules=[{"contains": "urgent", "tags": ["priority"]}],
        )
        tags = apply_tag_rules("This is urgent", profile)
        assert tags == ["priority"]


class TestPrepareRetainInput:
    def test_request_metadata_wins_over_mapped(self):
        profile = ExtractionProfileConfig(metadata_mapping={"k": "$.x"})
        req = RetainRequest(
            content='{"x": "from_json"}',
            bank_id="b",
            metadata={"k": "from_request"},
        )
        prep = prepare_retain_input(req, profile, graph_store_configured=False)
        assert prep.metadata is not None
        assert prep.metadata["k"] == "from_request"


class TestResolveRetainFactType:
    def test_default_world(self):
        assert resolve_retain_fact_type(None) == "world"
        assert resolve_retain_fact_type(ExtractionProfileConfig()) == "world"

    def test_profile_overrides(self):
        assert resolve_retain_fact_type(ExtractionProfileConfig(fact_type="observation")) == "observation"


class TestPackagedYamlBuiltins:
    def test_builtin_names_present_in_merged_table(self):
        profiles = merged_user_and_builtin_profiles({})
        assert "builtin_text" in profiles
        assert "builtin_conversation" in profiles


class TestExtractionProfileForSource:
    def test_returns_profile_name(self):
        sources = {"tavus": SourceConfig(type="webhook", extraction_profile="conversation")}
        assert extraction_profile_for_source("tavus", sources) == "conversation"
        assert extraction_profile_for_source("missing", sources) is None
        assert extraction_profile_for_source("tavus", None) is None
