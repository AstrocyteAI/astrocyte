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
