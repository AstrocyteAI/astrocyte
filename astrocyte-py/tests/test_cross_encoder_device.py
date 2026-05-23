"""Tests for cross-encoder device-selection behavior.

Focus: ``SentenceTransformersCrossEncoder._resolve_device`` — auto-MPS
on Apple Silicon, explicit override via ``device=...``, force_cpu
flag. The actual model load is not exercised here (would require
sentence-transformers + torch installs); we test the device-resolution
contract.
"""

from __future__ import annotations

from unittest.mock import patch

from astrocyte.pipeline.cross_encoder_rerank import (
    APACHE2_MODEL_PRESETS,
    SentenceTransformersCrossEncoder,
    is_apple_silicon,
    is_mps_available,
)


class TestPresets:
    def test_minilm_default_preset(self) -> None:
        assert APACHE2_MODEL_PRESETS["minilm"] == "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def test_mxbai_preset_available(self) -> None:
        # mxbai-rerank-base-v2 is the recommended Apache-2.0 default
        assert APACHE2_MODEL_PRESETS["mxbai-base"] == "mixedbread-ai/mxbai-rerank-base-v2"

    def test_all_presets_are_apache2(self) -> None:
        # Smoke: every entry maps to a publicly-known Apache-2.0 model path
        assert "mxbai-base" in APACHE2_MODEL_PRESETS
        assert "mxbai-large" in APACHE2_MODEL_PRESETS
        assert "bge-base" in APACHE2_MODEL_PRESETS
        assert "bge-large" in APACHE2_MODEL_PRESETS


class TestPlatformDetection:
    def test_is_apple_silicon_returns_bool(self) -> None:
        assert isinstance(is_apple_silicon(), bool)

    def test_is_mps_available_returns_bool(self) -> None:
        # Just confirms it doesn't raise; actual value depends on platform
        assert isinstance(is_mps_available(), bool)

    def test_mps_unavailable_without_torch(self) -> None:
        # Even on Apple Silicon, if torch isn't importable, MPS isn't usable
        with patch.dict("sys.modules", {"torch": None}):
            # Patching sys.modules to None makes import torch raise ImportError
            assert is_mps_available() is False


class TestDeviceResolution:
    def test_explicit_device_wins(self) -> None:
        e = SentenceTransformersCrossEncoder(device="cuda")
        assert e._resolve_device() == "cuda"

    def test_explicit_cpu(self) -> None:
        e = SentenceTransformersCrossEncoder(device="cpu")
        assert e._resolve_device() == "cpu"

    def test_force_cpu_flag(self) -> None:
        e = SentenceTransformersCrossEncoder(force_cpu=True)
        assert e._resolve_device() == "cpu"

    def test_force_cpu_loses_to_explicit_device(self) -> None:
        # If both set, explicit device wins (more specific override)
        e = SentenceTransformersCrossEncoder(device="cuda", force_cpu=True)
        assert e._resolve_device() == "cuda"

    def test_auto_mps_when_apple_silicon(self) -> None:
        with patch(
            "astrocyte.pipeline.cross_encoder_rerank.is_mps_available",
            return_value=True,
        ):
            e = SentenceTransformersCrossEncoder()
            assert e._resolve_device() == "mps"

    def test_default_when_no_mps(self) -> None:
        with patch(
            "astrocyte.pipeline.cross_encoder_rerank.is_mps_available",
            return_value=False,
        ):
            e = SentenceTransformersCrossEncoder()
            # None = let sentence-transformers default (CUDA/CPU)
            assert e._resolve_device() is None

    def test_force_cpu_overrides_mps_auto(self) -> None:
        with patch(
            "astrocyte.pipeline.cross_encoder_rerank.is_mps_available",
            return_value=True,
        ):
            e = SentenceTransformersCrossEncoder(force_cpu=True)
            assert e._resolve_device() == "cpu"

    def test_construction_with_mxbai_preset(self) -> None:
        # Smoke: encoder constructs cleanly with the recommended preset
        e = SentenceTransformersCrossEncoder(
            model_name=APACHE2_MODEL_PRESETS["mxbai-base"],
        )
        assert e.model_name == "mixedbread-ai/mxbai-rerank-base-v2"
        assert e._model is None  # not loaded yet


class TestEnvVarOverride:
    """M33-1a — ``ASTROCYTE_CROSS_ENCODER_MODEL`` env var override."""

    def test_env_unset_returns_default(self, monkeypatch) -> None:
        from astrocyte.pipeline.cross_encoder_rerank import _resolve_default_model

        monkeypatch.delenv("ASTROCYTE_CROSS_ENCODER_MODEL", raising=False)
        assert _resolve_default_model() == "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def test_env_empty_returns_default(self, monkeypatch) -> None:
        from astrocyte.pipeline.cross_encoder_rerank import _resolve_default_model

        monkeypatch.setenv("ASTROCYTE_CROSS_ENCODER_MODEL", "   ")
        assert _resolve_default_model() == "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def test_env_preset_alias_resolves(self, monkeypatch) -> None:
        from astrocyte.pipeline.cross_encoder_rerank import _resolve_default_model

        monkeypatch.setenv("ASTROCYTE_CROSS_ENCODER_MODEL", "bge-large")
        assert _resolve_default_model() == "BAAI/bge-reranker-large"

    def test_env_full_hf_path_passes_through(self, monkeypatch) -> None:
        from astrocyte.pipeline.cross_encoder_rerank import _resolve_default_model

        monkeypatch.setenv(
            "ASTROCYTE_CROSS_ENCODER_MODEL",
            "mixedbread-ai/mxbai-rerank-large-v2",
        )
        assert _resolve_default_model() == "mixedbread-ai/mxbai-rerank-large-v2"

    def test_get_default_cross_encoder_picks_env_var(self, monkeypatch) -> None:
        """``get_default_cross_encoder()`` with no args honors the env var."""
        from astrocyte.pipeline.cross_encoder_rerank import (
            get_default_cross_encoder,
            reset_default_cross_encoder_cache,
        )

        reset_default_cross_encoder_cache()
        monkeypatch.setenv("ASTROCYTE_CROSS_ENCODER_MODEL", "bge-large")
        # We don't actually want to load the 1GB model in CI — assert the
        # encoder was constructed with the resolved name but stays lazy.
        enc = get_default_cross_encoder()
        assert enc.model_name == "BAAI/bge-reranker-large"  # type: ignore[attr-defined]
        reset_default_cross_encoder_cache()

    def test_explicit_model_name_wins_over_env(self, monkeypatch) -> None:
        """Explicit ``model_name=...`` arg overrides the env var."""
        from astrocyte.pipeline.cross_encoder_rerank import (
            get_default_cross_encoder,
            reset_default_cross_encoder_cache,
        )

        reset_default_cross_encoder_cache()
        monkeypatch.setenv("ASTROCYTE_CROSS_ENCODER_MODEL", "bge-large")
        enc = get_default_cross_encoder(
            model_name="cross-encoder/ms-marco-MiniLM-L-6-v2",
        )
        assert enc.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"  # type: ignore[attr-defined]
        reset_default_cross_encoder_cache()
