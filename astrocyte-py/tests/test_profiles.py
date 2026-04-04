"""Tests for use-case profile loading and runtime behavior.

Verifies that all shipped profiles (support, coding, personal, research, minimal)
load correctly, merge with user overrides, and affect Astrocyte runtime behavior.
"""

from pathlib import Path

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import load_config
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "astrocyte.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


class TestProfileLoading:
    def test_support_profile(self, tmp_path: Path):
        config = load_config(_write_config(tmp_path, "profile: support\nprovider: test\n"))
        assert config.homeostasis.recall_max_tokens == 4096
        assert config.homeostasis.reflect_max_tokens == 4096
        assert config.barriers.pii.mode == "regex"
        assert config.barriers.pii.action == "redact"
        assert config.defaults.empathy == 5
        assert config.defaults.skepticism == 2
        assert config.escalation.degraded_mode == "empty_recall"
        assert config.homeostasis.rate_limits.retain_per_minute == 30

    def test_coding_profile(self, tmp_path: Path):
        config = load_config(_write_config(tmp_path, "profile: coding\nprovider: test\n"))
        assert config.homeostasis.recall_max_tokens == 8192
        assert config.barriers.pii.mode == "regex"
        assert config.barriers.pii.action == "warn"
        assert config.defaults.literalism == 5
        assert config.defaults.empathy == 1
        assert config.escalation.degraded_mode == "error"
        assert config.homeostasis.rate_limits.retain_per_minute == 120

    def test_personal_profile(self, tmp_path: Path):
        config = load_config(_write_config(tmp_path, "profile: personal\nprovider: test\n"))
        assert config.homeostasis.recall_max_tokens == 4096
        assert config.homeostasis.reflect_max_tokens == 8192
        assert config.defaults.empathy == 4
        assert config.defaults.skepticism == 3
        assert config.escalation.degraded_mode == "empty_recall"

    def test_research_profile(self, tmp_path: Path):
        config = load_config(_write_config(tmp_path, "profile: research\nprovider: test\n"))
        assert config.homeostasis.recall_max_tokens == 16384
        assert config.barriers.pii.mode == "disabled"
        assert config.defaults.skepticism == 5
        assert config.defaults.empathy == 1
        assert config.escalation.degraded_mode == "error"

    def test_minimal_profile(self, tmp_path: Path):
        config = load_config(_write_config(tmp_path, "profile: minimal\nprovider: test\n"))
        assert config.barriers.pii.mode == "disabled"
        assert config.escalation.degraded_mode == "error"

    def test_no_profile(self, tmp_path: Path):
        config = load_config(_write_config(tmp_path, "provider: test\n"))
        # Should use defaults from AstrocyteConfig
        assert config.defaults.empathy == 3
        assert config.defaults.skepticism == 3


# ---------------------------------------------------------------------------
# Profile + user override merging
# ---------------------------------------------------------------------------


class TestProfileOverrides:
    def test_user_overrides_profile_value(self, tmp_path: Path):
        config = load_config(
            _write_config(
                tmp_path,
                """
profile: support
provider: test
homeostasis:
  recall_max_tokens: 9999
""",
            )
        )
        assert config.homeostasis.recall_max_tokens == 9999  # Overridden
        assert config.defaults.empathy == 5  # Kept from profile

    def test_user_overrides_pii_action(self, tmp_path: Path):
        config = load_config(
            _write_config(
                tmp_path,
                """
profile: support
provider: test
barriers:
  pii:
    mode: regex
    action: reject
""",
            )
        )
        assert config.barriers.pii.action == "reject"  # Overridden from redact

    def test_user_overrides_degraded_mode(self, tmp_path: Path):
        config = load_config(
            _write_config(
                tmp_path,
                """
profile: support
provider: test
escalation:
  degraded_mode: error
""",
            )
        )
        assert config.escalation.degraded_mode == "error"  # Overridden from empty_recall


# ---------------------------------------------------------------------------
# Profile runtime behavior
# ---------------------------------------------------------------------------


class TestProfileRuntimeBehavior:
    async def test_support_profile_redacts_pii(self, tmp_path: Path):
        brain = Astrocyte.from_config(_write_config(tmp_path, "profile: support\nprovider: test\n"))
        brain.set_engine_provider(InMemoryEngineProvider())

        result = await brain.retain("Contact user@example.com", bank_id="b1")
        assert result.stored
        mem = brain._engine_provider._memories["b1"][0]
        assert "user@example.com" not in mem.text
        assert "[EMAIL_REDACTED]" in mem.text

    async def test_coding_profile_warns_pii(self, tmp_path: Path):
        brain = Astrocyte.from_config(_write_config(tmp_path, "profile: coding\nprovider: test\n"))
        brain.set_engine_provider(InMemoryEngineProvider())

        result = await brain.retain("Contact user@example.com", bank_id="b1")
        assert result.stored
        # Coding profile uses warn — content is preserved
        mem = brain._engine_provider._memories["b1"][0]
        assert "user@example.com" in mem.text

    async def test_minimal_profile_no_pii_scan(self, tmp_path: Path):
        brain = Astrocyte.from_config(_write_config(tmp_path, "profile: minimal\nprovider: test\n"))
        brain.set_engine_provider(InMemoryEngineProvider())

        result = await brain.retain("SSN: 123-45-6789", bank_id="b1")
        assert result.stored
        mem = brain._engine_provider._memories["b1"][0]
        assert "123-45-6789" in mem.text

    async def test_support_profile_rate_limit(self, tmp_path: Path):
        brain = Astrocyte.from_config(_write_config(tmp_path, "profile: support\nprovider: test\n"))
        brain.set_engine_provider(InMemoryEngineProvider())

        assert "retain" in brain._rate_limiters
        assert brain._rate_limiters["retain"]._max_per_minute == 30

    async def test_research_profile_high_token_budget(self, tmp_path: Path):
        brain = Astrocyte.from_config(_write_config(tmp_path, "profile: research\nprovider: test\n"))
        brain.set_engine_provider(InMemoryEngineProvider())

        assert brain._config.homeostasis.recall_max_tokens == 16384
        assert brain._config.homeostasis.reflect_max_tokens == 16384
