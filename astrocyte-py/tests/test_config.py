"""Tests for astrocyte.config — YAML loading, profiles, env vars."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocyte.config import load_config
from astrocyte.errors import ConfigError


class TestConfigLoading:
    def test_load_minimal_config(self, sample_config_path: Path):
        config = load_config(sample_config_path)
        assert config.provider_tier == "engine"
        assert config.provider == "test"

    def test_load_with_profile(self, support_config_path: Path):
        config = load_config(support_config_path)
        assert config.homeostasis.recall_max_tokens == 4096
        assert config.barriers.pii.action == "redact"
        assert config.defaults.empathy == 5

    def test_user_config_overrides_profile(self, tmp_path: Path):
        config_file = tmp_path / "test.yaml"
        config_file.write_text(
            """
profile: support
provider_tier: engine
provider: test
homeostasis:
  recall_max_tokens: 8192
"""
        )
        config = load_config(config_file)
        assert config.homeostasis.recall_max_tokens == 8192  # Overridden
        assert config.defaults.empathy == 5  # From profile

    def test_env_var_substitution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TEST_API_KEY", "secret123")
        config_file = tmp_path / "test.yaml"
        config_file.write_text(
            """
provider_tier: engine
provider: test
provider_config:
  api_key: ${TEST_API_KEY}
"""
        )
        config = load_config(config_file)
        assert config.provider_config["api_key"] == "secret123"

    def test_missing_config_file(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path.yaml")

    def test_missing_profile(self, tmp_path: Path):
        config_file = tmp_path / "test.yaml"
        config_file.write_text("profile: nonexistent_profile\n")
        with pytest.raises(ConfigError, match="Profile not found"):
            load_config(config_file)

    def test_tier_detection(self, sample_config_path: Path):
        config = load_config(sample_config_path)
        assert config.provider_tier == "engine"

    def test_rate_limits_from_config(self, sample_config_path: Path):
        config = load_config(sample_config_path)
        assert config.homeostasis.rate_limits.retain_per_minute == 60
        assert config.homeostasis.rate_limits.recall_per_minute == 120

    def test_barrier_config(self, sample_config_path: Path):
        config = load_config(sample_config_path)
        assert config.barriers.pii.mode == "regex"
        assert config.barriers.pii.action == "redact"
        assert config.barriers.validation.reject_empty_content is True

    def test_empty_config(self, tmp_path: Path):
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")
        config = load_config(config_file)
        assert config.provider_tier == "engine"  # Default
